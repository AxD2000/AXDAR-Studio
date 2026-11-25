"""Durable shot execution for direct ComfyUI production."""
import hashlib
import json
import os
import struct
import time
import uuid
from http.client import HTTPException
from urllib.error import HTTPError, URLError


def read(path, default=None):
    return json.loads(path.read_text("utf-8")) if path.exists() else default


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else hash_stream(stream)


def hash_stream(stream):
    h = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        h.update(chunk)
    return h.hexdigest()


def atomic(path, obj):
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), "utf-8")
    for attempt in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:
            # Windows may deny the rename while a status poll holds the old JSON open.
            if getattr(exc, "winerror", None) not in (5, 32) or attempt == 5:
                raise
            time.sleep(0.02 * 2**attempt)


def inspect_video(path, seconds, require_audio=False):
    """Check ISO BMFF box boundaries, video track and movie duration without decoding."""
    def boxes(f, start, end):
        while start < end:
            f.seek(start)
            if end - start < 8:
                raise ValueError("Truncated MP4 header")
            size, kind = struct.unpack(">I4s", f.read(8))
            head = 8
            if size == 1:
                if end - start < 16:
                    raise ValueError("Truncated extended MP4 box")
                size, head = int.from_bytes(f.read(8), "big"), 16
            if size == 0:
                size = end - start
            if size < head or start + size > end:
                raise ValueError("Truncated MP4 box")
            yield kind, start + head, start + size
            start += size

    duration, video, audio, media, brand = 0, False, False, False, False
    with path.open("rb") as f:
        for kind, a, b in boxes(f, 0, path.stat().st_size):
            if kind == b"ftyp":
                brand = True
            if kind == b"mdat" and b > a:
                media = True
            if kind != b"moov":
                continue
            for k, x, y in boxes(f, a, b):
                if k == b"mvhd":
                    f.seek(x)
                    v = f.read(1)
                    pos = x + (20 if v == b"\1" else 12)
                    width = 8 if v == b"\1" else 4
                    if pos + 4 + width > y:
                        raise ValueError("Truncated movie timing")
                    f.seek(pos)
                    scale = int.from_bytes(f.read(4), "big")
                    duration = int.from_bytes(f.read(width), "big") / max(scale, 1)
                if k == b"trak":
                    for t, l, r in boxes(f, x, y):
                        if t == b"mdia":
                            for v, i, j in boxes(f, l, r):
                                if v == b"hdlr" and j-i >= 12:
                                    f.seek(i+8)
                                    handler=f.read(4)
                                    video |= handler == b"vide"
                                    audio |= handler == b"soun"
    # Allow for differences in MP4 track and movie header timing.
    if require_audio and not audio:
        raise ValueError("Expected audio track is missing from the MP4")
    if not (brand and media and video and duration > 0 and abs(duration-seconds) <= 1):
        raise ValueError(f"Invalid MP4 or unexpected duration {duration:.2f}s (expected {seconds}s)")
    return duration


def inspect_take(path, job):
    seconds = job.get("playback_seconds", job["settings"]["seconds"])
    return inspect_video(path, seconds, job.get("audioRequired",False))


def lookup(api, url, marker):
    queue = api(url, "/queue")
    entries = queue["queue_running"] + queue["queue_pending"]
    def match(row):
        return len(row) >= 4 and row[3].get("axdar_attempt") == marker
    for row in entries:
        if match(row):
            return row[1]
    for ticket, entry in api(url, "/history").items():
        if match(entry.get("prompt", [])):
            return ticket
    return None


class Uncertain(Exception):
    pass


class ExecutionFailed(ValueError):
    pass


def pending_prompt(job, folder):
    if job.get("status") in ("queued", "waiting", "submitting", "uncertain"):
        return True
    # Older Studio versions could store an error after accepting a prompt without checking its history.
    return (job.get("status") == "error" and bool(job.get("ticket"))
            and not (folder / "history.json").is_file())


def render(ctx, url, deadline, eid, shot, wf, retries, notify, retry_failed=False, force_new=False):
    root = ctx["DATA"]/eid/shot["id"]
    root.mkdir(parents=True, exist_ok=True)
    versions = sorted(root.glob("*/job.json"))
    for path in reversed(versions):
        j = read(path)
        if j["revision"] != shot["revision"]:
            continue
        folder = path.parent
        if j["status"] == "preparing":
            break
        if j["status"] == "done":
            if force_new:
                versions = []
                break
            inspect_take(folder/"video.mp4", j)
            return "done"
        if pending_prompt(j, folder) or j.get("ticket") and j["status"] not in ("error", "cancelled"):
            same_target = str(j["instance"]) == str(ctx.get("instance")) if j.get("instance") else j.get("url") == url
            if not same_target:
                raise Uncertain("Prior submission belongs to another server; reconcile it before generating again.")
            break
        if j["status"] == "error" and not (retry_failed or force_new):
            return "error"
        versions = []
        break
    else:
        versions = []
    if not versions:
        folder = root/str(time.time_ns())
        folder.mkdir()
        path = folder/"job.json"
        j = dict(shot=shot["id"], revision=shot["revision"], url=url, status="preparing",
                 prompt=shot.get("frozenPrompt") or ctx["prompt"](shot), settings=shot, workflow=wf["name"],
                 output_node=wf["output"], marker=uuid.uuid4().hex,
                 take_id=str(uuid.uuid4()), started=time.time(), resolution=shot.get("resolution"))
        if wf.get("version"):
            from workflow_profiles import timing
            j.update(workflow_version=wf["version"], workflow_profile=wf, scene_id=shot.get("scene_id"),
                     playback_seconds=timing(wf,shot["seconds"])["playback_seconds"])
        atomic(path,j)
    def finish(status):
        j.update(status=status, completed=time.time(), renderSeconds=max(0,time.time()-j.get("started",time.time())))
        atomic(path,j)
        return status
    for attempt in range(retries+1):
        try:
            if not j.get("ticket"):
                if j["status"] in ("submitting","uncertain","waiting"):
                    ticket = lookup(ctx["json_api"], url, j.get("marker") or "legacy-no-marker")
                    if not ticket:
                        raise Uncertain("Submission acknowledgement missing. Inspect the original ComfyUI queue/history before retrying.")
                else:
                    graph = ctx["prepare_shot"](url, folder, eid, shot, wf, j)
                    atomic(folder/"graph.json",graph)
                    j["audioRequired"] = "audio" in graph.get(str(j["output_node"]),{}).get("inputs",{})
                    j.update(status="submitting",url=url)
                    if ctx.get("instance"):
                        j["instance"] = ctx["instance"]
                    atomic(path,j)
                    try:
                        ticket=ctx["json_api"](url,"/prompt",{"prompt":graph,"extra_data":{"axdar_attempt":j["marker"]}})["prompt_id"]
                        if not isinstance(ticket,str) or not ticket:
                            raise TypeError("Invalid prompt ID.")
                    except (json.JSONDecodeError,KeyError,TypeError) as exc:
                        raise Uncertain("Invalid ComfyUI acknowledgement; reconcile before retrying.") from exc
                j.update(ticket=ticket,status="queued")
                atomic(path,j)
            missing_checks=0
            while time.time()<deadline:
                notify()
                if not j.get("artifact_pending") and ctx.get("cancel_prompt") and ctx["cancel_prompt"](j["ticket"]):
                    j.pop("error",None)
                    return finish("cancelled")
                try:
                    h=read(folder/"history.json") if j.get("artifact_pending") else None
                    if not h:
                        h=ctx["json_api"](url,"/history/"+j["ticket"]).get(j["ticket"])
                except (ValueError,AttributeError) as exc:
                    raise Uncertain("The recorded prompt ID could not be checked. Reconcile before retrying.") from exc
                if h:
                    atomic(folder/"history.json",h)
                    if h.get("status",{}).get("status_str")=="error":
                        details=next((m[1] for m in h["status"].get("messages",[]) if m[0]=="execution_error"),{})
                        raise ExecutionFailed(str(details.get("node_type","Render"))+": "+str(details.get("exception_message","ComfyUI execution failed"))[:800])
                    if h.get("status",{}).get("completed") is False:
                        raise Uncertain("The original ComfyUI prompt has no completed execution history.")
                    if not j.get("artifact_pending"):
                        j.update(status="waiting",artifact_pending=True)
                        atomic(path,j)
                    ctx["download_output"](url,folder,h,j)
                    if "audioRequired" not in j:
                        graph = read(folder/"graph.json", wf.get("graph",{}))
                        j["audioRequired"] = "audio" in graph.get(str(j["output_node"]),{}).get("inputs",{})
                    part=folder/"video.part"
                    try:
                        duration=inspect_take(part,j)
                    except ValueError as exc:
                        j["quality"]={"technical":"failed","reason":str(exc)}
                        raise
                    if reviewer:=ctx.get("technical_qc"):
                        j["quality"]=reviewer(part,j)
                        if j["quality"]["technical"]=="failed":
                            raise ValueError(j["quality"]["reason"])
                        j["quality"].pop("visual",None)
                    part.replace(folder/"video.mp4")
                    (folder/"video.resume.json").unlink(missing_ok=True)
                    j.update(duration=duration,sha256=digest(folder/"video.mp4"))
                    j.pop("artifact_pending",None)
                    j.pop("error",None)
                    return finish("done")
                missing_checks+=1
                if missing_checks%10==0:
                    queue=ctx["json_api"](url,"/queue")
                    if not any(row[1]==j["ticket"] for row in queue["queue_running"]+queue["queue_pending"]):
                        if not ctx["json_api"](url,"/history/"+j["ticket"]).get(j["ticket"]):
                            raise Uncertain("ComfyUI no longer lists this ticket in its queue or history; verify before resubmitting.")
                time.sleep(3)
            j["status"]="waiting"
            atomic(path,j)
            return "waiting"
        except (URLError,TimeoutError,ConnectionError,HTTPException) as exc:
            if attempt < retries:
                time.sleep(min(2**attempt,15))
                continue
            message="ComfyUI finished rendering. Resume to download its video from the original instance." if j.get("artifact_pending") else str(exc)[:500]
            j.update(status="waiting" if j.get("ticket") else ("uncertain" if j["status"]=="submitting" else "error"),error=message)
        except Uncertain as exc:
            j.update(status="uncertain",error=str(exc))
        except Exception as exc:
            if j["status"] == "done":
                raise
            confirmed_failure = isinstance(exc, ExecutionFailed) or (j.get("quality") or {}).get("technical") == "failed"
            response = exc if isinstance(exc, HTTPError) else exc.__cause__
            rejected = isinstance(response, HTTPError) and 400 <= response.code < 500
            status = "error"
            if j.get("ticket") and not confirmed_failure:
                status = "waiting"
            elif j["status"] == "submitting" and not rejected:
                status = "uncertain"
            j.update(status=status,error=str(exc)[:900])
        if j["status"]=="error":
            if (folder/"video.part").is_file():
                (folder/"video.part").replace(folder/"video.failed.mp4")
            j.pop("artifact_pending",None)
            return finish("error")
        atomic(path,j)
        return j["status"]
