"""Durable production events and the fail-closed lifecycle countdown."""
import json
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from drive_archive import NoRedirect
from production import atomic, read


def webhook_url(value):
    parts=urlsplit(value)
    if parts.scheme!="https" or not parts.hostname or parts.username or parts.password or parts.fragment or len(value)>2048:
        raise ValueError("Enter the HTTPS URL of your authorized notification webhook.")
    return value


def event(run, spec, kind, detail=""):
    path=run/"events.json"
    rows=read(path,{})
    key=f"{spec.get('dispatch') or spec['id']}:{kind}"
    if key not in rows:
        rows[key]=dict(id=key,kind=kind,detail=detail,time=time.time(),episode=spec["episode"],delivery="pending")
        atomic(path,rows)
    return key


def deliver(run, endpoint):
    path=run/"events.json"
    rows=read(path,{})
    if endpoint:
        for row in rows.values():
            if row["delivery"]=="delivered":
                continue
            try:
                message={k:row[k] for k in ("id","kind","detail","time","episode")}
                request=Request(webhook_url(endpoint),data=json.dumps(message).encode(),
                                headers={"Content-Type":"application/json","Idempotency-Key":row["id"]},method="POST")
                with build_opener(NoRedirect()).open(request,timeout=8) as response:
                    if not 200<=response.status<300:
                        raise ValueError("Non-successful webhook response.")
                row.update(delivery="delivered",delivered=time.time())
                row.pop("error",None)
            except (OSError,ValueError):
                row["error"]="Notification delivery failed. It remains pending."
            atomic(path,rows)
    return rows


def active_jobs(ctx,url):
    queue=ctx["json_api"](url,"/queue")
    return bool(queue["queue_running"] or queue["queue_pending"])


def eligible(run,spec,state,ctx):
    if state.get("status") not in ("finished","needs attention"):
        return "Production has not finished."
    shots=state.get("shots",{})
    expected={row[1]["id"] for row in spec["jobs"]}
    if set(shots)!=expected or any(status not in ("done","error") for status in shots.values()):
        return "Some shots are unfinished or uncertain."
    if state.get("inflightRetry") or any(status=="error" and
            state.get("shotRetries",{}).get(sid,0)<spec.get("shotRetries",0) for sid,status in shots.items()):
        return "The final retry pass has not finished."
    for file in (run/"episodes").glob("*/*/*/job.json"):
        if read(file).get("status") not in ("done","error"):
            return "Outstanding ComfyUI submissions require reconciliation."
    if state.get("backup",{}).get("state")!="verified":
        return "The Drive archive has not been verified."
    try:
        if active_jobs(ctx,spec["url"]):
            return "There are active ComfyUI jobs on this instance."
    except (OSError,ValueError,KeyError):
        return "ComfyUI queue status could not be verified."
    return ""


def countdown(run,spec,state,ctx,endpoint):
    path=run/"lifecycle.json"
    previous=read(path,{})
    if cancel:=read(run/"lifecycle-cancel.json",{}):
        return dict(status="cancelled",**cancel)
    if previous.get("status") in ("cancelled","rehearsed"):
        return previous
    reason=eligible(run,spec,state,ctx)
    if reason:
        outcome=dict(status="blocked",reason=reason)
    else:
        key=event(run,spec,"termination warning","Ten-minute dry-run countdown. Cancellation is available in AXDAR.")
        events=deliver(run,endpoint)
        if events[key]["delivery"]!="delivered":
            outcome=dict(status="blocked",reason="The termination warning has not been delivered.")
        else:
            deadline=previous.get("deadline") or time.time()+600
            outcome=dict(status="countdown",deadline=deadline,mode="rehearsal")
    if cancel:=read(run/"lifecycle-cancel.json",{}):
        return dict(status="cancelled",**cancel)
    atomic(path,outcome)
    return outcome



def watchdog(run):
    """Check the dry run independently of both the Windows app and rendering worker."""
    import fcntl
    from urllib.request import urlopen

    path=run/"lifecycle.json"
    remaining=True
    while remaining:
        if read(run/"lifecycle-cancel.json",{}):
            return
        plan=read(path,{})
        if plan.get("status")!="countdown":
            return
        remaining=plan["deadline"]-time.time()
        if remaining>0:
            time.sleep(min(5,remaining))
        else:
            break
    spec=read(run/"spec.json")
    state=read(run/"status.json")
    spec["url"]=state.get("url","")
    def api(url,route):
        parts=urlsplit(url)
        if parts.scheme!="http" or parts.hostname!="127.0.0.1" or not parts.port:
            raise ValueError("The worker's loopback ComfyUI address is unavailable.")
        with urlopen(url+route,timeout=5) as response:
            return json.load(response)
    with (run.parent.parent/"production.lock").open("a") as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            reason="A production process still holds the instance lock."
        else:
            reason=eligible(run,spec,state,{"json_api":api})
        if read(run/"lifecycle-cancel.json",{}):
            return
        if reason:
            atomic(path,dict(status="blocked",reason=reason))
        else:
            atomic(path,dict(plan,status="rehearsed",reason="Dry run completed. The instance was preserved."))
        private=read(run/"services-private.json",{})
        event(run,spec,"termination rehearsal ended")
        deliver(run,private.get("webhook"))


if __name__=="__main__":
    import sys
    from pathlib import Path
    watchdog(Path(sys.argv[1]).resolve())
