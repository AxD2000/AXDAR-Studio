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


def render(ctx, url, deadline, eid, shot, rules, wf, retries, notify, retry_failed=False, retry_key=None, admit=True, budget_check=None):
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
            if not (ctx.get("direct") and j.get("remote_run") and not (folder/"video.mp4").is_file()):
                inspect_take(folder/"video.mp4", j)
            return "done"
        if j["status"] in ("queued","waiting","submitting","uncertain") or j.get("ticket") and j["status"]!="error":
            same_target = str(j["instance"]) == str(ctx.get("instance")) if j.get("instance") else j.get("url") == url
            if not same_target:
                raise Uncertain("Prior submission belongs to another server; reconcile it before generating again.")
            break
        if j["status"] == "error" and (not retry_failed or retry_key and j.get("retry_key")==retry_key):
            return "error"
        versions = []
        break
    else:
        versions = []
    if not versions:
        if not admit:
            return "limit reached"
        folder = root/str(time.time_ns())
        folder.mkdir()
        path = folder/"job.json"
        j = dict(shot=shot["id"], revision=shot["revision"], url=url, status="preparing",
                 prompt=shot.get("frozenPrompt") or ctx["prompt"](shot,rules), settings=shot,rules=rules, workflow=wf["name"],
                 output_node=wf["output"], marker=uuid.uuid4().hex,
                 retry_key=retry_key, take_id=str(uuid.uuid4()), started=time.time(), resolution=shot.get("resolution"))
        if wf.get("version"):
            from workflow_profiles import timing
            j.update(workflow_version=wf["version"], workflow_profile=wf, scene_id=shot.get("scene_id"),
                     playback_seconds=timing(wf,shot["seconds"])["playback_seconds"])
        atomic(path,j)
    def finish(status):
        j.update(status=status, completed=time.time(), renderSeconds=max(0,time.time()-j.get("started",time.time())))
        atomic(path,j)
        if ctx.get("record_take"):
            ctx["record_take"](folder, j)
        return status
    for attempt in range(retries+1):
        try:
            if not j.get("ticket"):
                if j["status"] in ("submitting","uncertain","waiting"):
                    ticket = lookup(ctx["json_api"], url, j.get("marker") or "legacy-no-marker")
                    if not ticket:
                        raise Uncertain("Submission acknowledgement missing. Inspect the original ComfyUI queue/history before retrying.")
                else:
                    if not admit:
                        return "limit reached"
                    graph = ctx["prepare_shot"](url, folder, eid, shot, wf, j)
                    atomic(folder/"graph.json",graph)
                    if budget_check and not budget_check():
                        return "limit reached"
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
            poll_deadline=max(deadline,time.time()+8) if not admit else deadline
            while time.time()<poll_deadline:
                notify()
                if ctx.get("cancel_prompt") and ctx["cancel_prompt"](j["ticket"]):
                    j.pop("error",None)
                    return finish("cancelled")
                try:
                    h=ctx["json_api"](url,"/history/"+j["ticket"]).get(j["ticket"])
                except (ValueError,AttributeError) as exc:
                    raise Uncertain("The recorded prompt ID could not be checked. Reconcile before retrying.") from exc
                if h:
                    atomic(folder/"history.json",h)
                    if h.get("status",{}).get("status_str")=="error":
                        details=next((m[1] for m in h["status"].get("messages",[]) if m[0]=="execution_error"),{})
                        raise ValueError(str(details.get("node_type","Render"))+": "+str(details.get("exception_message","ComfyUI execution failed"))[:800])
                    if h.get("status",{}).get("completed") is False:
                        raise Uncertain("The original ComfyUI prompt has no completed execution history.")
                    ctx["download_output"](url,folder,h,j)
                    if "audioRequired" not in j:
                        graph = read(folder/"graph.json", wf.get("graph",{}))
                        j["audioRequired"] = "audio" in graph.get(str(j["output_node"]),{}).get("inputs",{})
                    part=folder/"video.part"
                    try:
                        duration=inspect_take(part,j)
                    except ValueError as exc:
                        j["quality"]={"technical":"failed","visual":"review required","reason":str(exc)}
                        raise
                    if reviewer:=ctx.get("technical_qc"):
                        j["quality"]=reviewer(part,j)
                        if j["quality"]["technical"]=="failed":
                            raise ValueError(j["quality"]["reason"])
                        if ctx.get("direct"):
                            j["quality"].pop("visual",None)
                        else:
                            j["review"]="required"
                    part.replace(folder/"video.mp4")
                    j.update(duration=duration,sha256=digest(folder/"video.mp4"))
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
            j.update(status="waiting" if j.get("ticket") else ("uncertain" if j["status"]=="submitting" else "error"),error=str(exc)[:500])
        except Uncertain as exc:
            j.update(status="uncertain",error=str(exc))
        except Exception as exc:
            j.update(status="error",error=str(exc)[:900])
        if j["status"]=="error":
            if (folder/"video.part").is_file():
                (folder/"video.part").replace(folder/"video.failed.mp4")
            return finish("error")
        atomic(path,j)
        return j["status"]


def drive(ctx, spec, jobs, wf, state_path, control_path, retry_id=None):
    state=read(state_path, {}) or {}
    if state.get("id")!=spec["id"] or "shots" not in state:
        state=dict(id=spec["id"], started=spec["started"], shots={}, total=len(jobs), seconds=[])
    state.setdefault("shotRetries", {})
    state.setdefault("inflightRetry", {})
    state.setdefault("renderSeconds", 0)
    state.setdefault("retrySeconds", 0)
    state.setdefault("usableSeconds", 0)

    def publish():
        state["updated"]=time.time()
        elapsed=max(0,state["updated"]-state["started"])
        state["elapsed"]=elapsed
        state["estimate"]=elapsed*spec["rate"]/3600
        state["projectEstimate"]=spec.get("priorEstimate",0)+state["estimate"]
        state["monthlyEstimate"]=spec.get("priorMonthEstimate",0)+state["estimate"]
        state["overheadSeconds"]=max(0,elapsed-state["renderSeconds"])
        good=state["seconds"]
        state["meanSeconds"]=sum(good)/len(good) if good else None
        atomic(state_path,state)

    def may_submit():
        publish()
        projected=(state["meanSeconds"] or spec["expectedSeconds"])*spec["rate"]/3600
        budgets=(("budget","estimate"),("episodeBudget","projectEstimate"),("monthBudget","monthlyEstimate"))
        return time.time()<spec["end"]-60 and all(
            not spec.get(limit) or state[cost]+projected<=spec[limit] for limit,cost in budgets)

    def run_job(row, retry=False, manual=False):
        eid,shot,rules=row
        sid=shot["id"]
        action=read(control_path,{}).get("action")
        if action in ("pause","stop"):
            state["status"]="paused" if action=="pause" else "stopped"
            return False
        admit=may_submit()
        previous=state["shots"].get(sid)
        if not admit and previous not in ("waiting","uncertain") and state.get("current")!=sid:
            state["status"]="limit reached"
            return False
        key = state["inflightRetry"].get(sid)
        if retry and admit and not key:
            key=spec.get("dispatch",spec["id"])+":"+uuid.uuid4().hex
            state["inflightRetry"][sid]=key
            if not manual:
                state["shotRetries"][sid]=state["shotRetries"].get(sid,0)+1
        started=time.time()
        state.update(current=sid,shotStarted=started)
        publish()
        try:
            result=render(ctx,spec["url"],spec["end"],eid,shot,rules,wf,spec["retries"],
                          publish,retry_failed=bool(key),retry_key=key,admit=admit,budget_check=may_submit)
        except Exception as exc:
            result="uncertain" if isinstance(exc,Uncertain) else "error"
            state["error"]=f"{sid}: {exc}"
        elapsed=time.time()-started
        state["renderSeconds"]+=elapsed
        if key:
            state["retrySeconds"]+=elapsed
        # Replaying an interrupted retry reuses its attempt key until the durable result is reconciled.
        if result=="limit reached":
            state["status"]=result
            return False
        state["shots"][sid]=result
        if result=="done":
            state["seconds"].append(elapsed)
            state["usableSeconds"]+=shot["seconds"]
        if result in ("done","error"):
            state["inflightRetry"].pop(sid,None)
        state["current"]=None
        publish()
        if result in ("waiting","uncertain"):
            state["status"]="waiting" if result=="waiting" else "needs attention"
            return False
        return True

    try:
        state.update(status="running",error="",url=spec["url"],dispatch=spec.get("dispatch"))
        if retry_id:
            pending=next((row for row in jobs if row[1]["id"]==retry_id),None)
            if pending and not run_job(pending,retry=True,manual=True):
                return
        else:
            for row in jobs:
                sid=row[1]["id"]
                if state["shots"].get(sid) in ("done","error") and sid not in state["inflightRetry"]:
                    continue
                if not run_job(row):
                    return
            # A full pass always gets the first attempt before any definite failure is retried.
            for pass_number in range(spec.get("shotRetries",0)):
                for row in jobs:
                    sid=row[1]["id"]
                    if state["shots"].get(sid)!="error" or state["shotRetries"].get(sid,0)>pass_number:
                        continue
                    if not run_job(row,retry=True):
                        return
        values=state["shots"].values()
        state["status"]="needs attention" if any(x!="done" for x in values) else ("finished" if len(state["shots"])==len(jobs) else "paused")
    finally:
        state["current"]=None
        if spec.get("package2"):
            state["maintenance"]="pending"
        publish()
