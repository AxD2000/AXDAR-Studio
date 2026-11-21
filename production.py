"""Durable shot execution shared by the desktop and the detached Vast worker."""
import hashlib
import json
import os
import struct
import time
import uuid
from http.client import HTTPException
from pathlib import Path
from urllib.error import URLError


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
    os.replace(tmp, path)


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


def render(ctx, url, deadline, eid, shot, rules, wf, retries, notify, retry_failed=False):
    root = ctx["DATA"]/eid/shot["id"]
    root.mkdir(parents=True, exist_ok=True)
    versions = sorted(root.glob("*/job.json"))
    for path in reversed(versions):
        j = read(path)
        if j["revision"] != shot["revision"]:
            continue
        folder = path.parent
        if j["status"] == "done":
            inspect_take(folder/"video.mp4", j)
            return "done"
        if j["status"] in ("queued","waiting","submitting","uncertain") or j.get("ticket") and j["status"]!="error":
            if j.get("url") != url:
                raise Uncertain("Prior submission belongs to another server; reconcile it before generating again.")
            break
        if j["status"] == "error" and not retry_failed:
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
                 prompt=shot.get("frozenPrompt") or ctx["prompt"](shot,rules), settings=shot,rules=rules, workflow=wf["name"],
                 output_node=wf["output"], marker=uuid.uuid4().hex)
        if wf.get("version"):
            from workflow_profiles import timing
            j.update(workflow_version=wf["version"], workflow_profile=wf, scene_id=shot.get("scene_id"),
                     playback_seconds=timing(wf,shot["seconds"])["playback_seconds"])
        atomic(path,j)
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
                    j["status"]="submitting"
                    atomic(path,j)
                    try:
                        ticket=ctx["json_api"](url,"/prompt",{"prompt":graph,"extra_data":{"axdar_attempt":j["marker"]}})["prompt_id"]
                    except (json.JSONDecodeError,KeyError) as exc:
                        raise Uncertain("Invalid ComfyUI acknowledgement; reconcile before retrying.") from exc
                j.update(ticket=ticket,status="queued")
                atomic(path,j)
            missing_checks=0
            while time.time()<deadline:
                notify()
                h=ctx["json_api"](url,"/history/"+j["ticket"]).get(j["ticket"])
                if h:
                    atomic(folder/"history.json",h)
                    if h.get("status",{}).get("status_str")=="error":
                        details=next((m[1] for m in h["status"].get("messages",[]) if m[0]=="execution_error"),{})
                        raise ValueError(str(details.get("node_type","Render"))+": "+str(details.get("exception_message","ComfyUI execution failed"))[:800])
                    ctx["download_output"](url,folder,h,j)
                    if "audioRequired" not in j:
                        graph = read(folder/"graph.json", wf.get("graph",{}))
                        j["audioRequired"] = "audio" in graph.get(str(j["output_node"]),{}).get("inputs",{})
                    duration=inspect_take(folder/"video.part",j)
                    (folder/"video.part").replace(folder/"video.mp4")
                    j.update(status="done", duration=duration, sha256=digest(folder/"video.mp4"))
                    j.pop("error",None)
                    atomic(path,j)
                    return "done"
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
            j.update(status="waiting" if j.get("ticket") else ("uncertain" if j["status"]=="submitting" else "error"),error=str(exc)[:500])
        except Uncertain as exc:
            j.update(status="uncertain",error=str(exc))
        except Exception as exc:
            j.update(status="error",error=str(exc)[:900])
        atomic(path,j)
        return j["status"]


def drive(ctx, spec, jobs, wf, state_path, control_path, retry_id=None):
    state=read(state_path, {}) or {}
    if state.get("id")!=spec["id"] or "shots" not in state:
        state=dict(id=spec["id"], started=spec["started"], shots={}, total=len(jobs), seconds=[])
    def publish():
        state["updated"]=time.time()
        elapsed=max(0,state["updated"]-state["started"])
        state["elapsed"]=elapsed
        state["estimate"]=elapsed*spec["rate"]/3600
        state["projectEstimate"]=spec.get("priorEstimate",0)+state["estimate"]
        good=state["seconds"]
        state["meanSeconds"]=sum(good)/len(good) if good else None
        atomic(state_path,state)
    try:
        state.update(status="running",current=None,error="",url=spec["url"],dispatch=spec.get("dispatch"))
        publish()
        for eid,s,rules in jobs:
            sid=s["id"]
            if retry_id and retry_id!=sid: continue
            if state["shots"].get(sid) in ("done","error") and retry_id!=sid: continue
            action=read(control_path,{}).get("action")
            if action in ("pause","stop"):
                state["status"]="paused" if action=="pause" else "stopped"
                break
            publish()
            expected=state["meanSeconds"] or spec["expectedSeconds"]
            if time.time()>=spec["end"]-60 or (spec["budget"] and state["projectEstimate"]+expected*spec["rate"]/3600>spec["budget"]):
                state["status"]="limit reached"
                break
            state.update(current=sid,shotStarted=time.time())
            publish()
            try:
                result=render(ctx,spec["url"],spec["end"],eid,s,rules,wf,spec["retries"],publish,retry_id==sid)
            except Exception as exc:
                result="uncertain" if isinstance(exc,Uncertain) else "error"
                state["error"]=f"{sid}: {exc}"
            state["shots"][sid]=result
            if result=="done":
                state["seconds"].append(time.time()-state["shotStarted"])
            publish()
            if result=="waiting":
                state["status"]="waiting"
                break
        else:
            values=state["shots"].values()
            state["status"]="needs attention" if any(x!="done" for x in values) else ("finished" if len(state["shots"])==len(jobs) else "paused")
    finally:
        state["current"]=None
        publish()

