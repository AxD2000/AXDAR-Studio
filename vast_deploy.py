"""Vast instance discovery and deployment over the user's verified OpenSSH connection."""
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import threading
import time
import uuid
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.parse import urlencode

import production as prod

STORE="/workspace/axdar-studio"
SOURCES=("worker.py","production.py","vast_worker.py","vast_deploy.py","studio.pyw","studio_actions.py","story_engine.py","workflow_profiles.py","provision.py","model-lock.json","vast_connection.py")
TERMINAL={"finished","needs attention","stopped","paused","waiting","limit reached"}


def instance_target(row):
    host,port=row.get("ssh_host"),row.get("ssh_port")
    mappings=(row.get("ports") or {}).get("22/tcp",[])
    if row.get("public_ipaddr") and mappings:
        import ipaddress
        try:
            direct_host=str(ipaddress.ip_address(row["public_ipaddr"]))
            direct_port=int(mappings[0]["HostPort"])
            if ":" not in direct_host and 1<=direct_port<=65535:
                host,port=direct_host,direct_port
        except (ValueError,KeyError,TypeError,IndexError):
            pass
    return dict(id=row["id"],status=row.get("actual_status"),host=host,port=port,
                rate=row.get("dph_total"),gpu=row.get("gpu_name","GPU"))


def instances():
    from vast_connection import api_key
    key=api_key()
    if not key:
        raise ValueError("Save your Vast API key in the Vast connection panel first.")
    params={"limit":25}
    result=[]
    while True:
        req=Request("https://console.vast.ai/api/v1/instances/?"+urlencode(params),headers={"Authorization":"Bearer "+key})
        with urlopen(req,timeout=15) as stream:
            data=json.load(stream)
        for row in data["instances"]:
            result.append(instance_target(row))
        if not data.get("next_token"):return result
        params["after_token"]=data["next_token"]


def request_instance_stop(target):
    from vast_connection import api_key
    key=api_key()
    if not key:raise ValueError("VAST_API_KEY is required to stop the selected instance.")
    instance_id=int(target["id"])
    req=Request(f"https://console.vast.ai/api/v0/instances/{instance_id}/",method="PUT",
                data=b'{"state":"stopped"}',headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"})
    with urlopen(req,timeout=20) as response:
        if not json.load(response).get("success"):
            raise ValueError("Vast did not confirm the stop request")


def ssh(target,command,*,data=None,stream=None,out=None,timeout=70):
    host=str(target["host"])
    port=int(target["port"])
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}",host) or not 1<=port<=65535:
        raise ValueError("Invalid SSH host or port")
    args=["ssh","-T","-o","BatchMode=yes","-o","StrictHostKeyChecking=yes","-o","ConnectTimeout=8",
          "-o","ServerAliveInterval=15","-o","ServerAliveCountMax=2","-p",str(port),"root@"+host,command]
    p=subprocess.run(args,input=data,stdin=stream,stdout=out or subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    if p.returncode:
        raise OSError(p.stderr.decode(errors="replace").strip()[-550:] or "Remote command failed")
    return p.stdout


def put(target,path,local):
    # The remote paths contain only fixed names and validated hexadecimal identifiers.
    sha=path.rsplit("/",1)[1] if path.startswith(STORE+"/blobs/") else prod.digest(local)
    tmp=path+"."+uuid.uuid4().hex+".part"
    with local.open("rb") as src:
        ssh(target,f"umask 077; cat > {tmp} && echo '{sha}  {tmp}' | sha256sum -c --status && mv {tmp} {path}",stream=src,timeout=240)


def put_json(target,path,data):
    tmp=path+"."+uuid.uuid4().hex+".tmp"
    ssh(target,f"umask 077; cat > {tmp} && mv {tmp} {path}",data=json.dumps(data).encode())


def paths(spec):
    sid=uuid.UUID(spec["id"]).hex
    release=spec["release"]
    if not re.fullmatch("[0-9a-f]{64}",release):raise ValueError("Invalid release")
    return STORE+"/releases/"+release,STORE+"/runs/"+sid


def deploy(base,target,spec,assets):
    release,run=paths(spec)
    ssh(target,f"umask 077; mkdir -p {release} {run} {STORE}/blobs")
    planned={release+"/"+name:base/name for name in SOURCES}
    planned.update({STORE+"/blobs/"+sha:assets[name] for name,sha in spec["assets"].items()})
    # One remote check for the whole manifest; transmit only missing or damaged files.
    manifest="".join(f"{prod.digest(src)}  {dst}\n" for dst,src in planned.items())
    check=ssh(target,"while read -r hash path; do echo \"$hash  $path\" | sha256sum -c --status 2>/dev/null || echo \"$path\"; done",data=manifest.encode())
    for dst in check.decode().splitlines():
        if dst not in planned:raise ValueError("Unexpected checksum response from SSH host")
        if dst.startswith(STORE+"/blobs/") and prod.digest(planned[dst])!=dst.rsplit("/",1)[1]:
            raise ValueError("An asset changed during deployment. Start a new snapshot.")
        put(target,dst,planned[dst])
    put_json(target,run+"/spec.json",spec)


def launch(target,spec):
    release,run=paths(spec)
    # flock inside the worker serializes runs; the watchdog retries an interrupted interpreter.
    cmd=f"for delay in 1 4 12; do python3 {release}/worker.py --production {run} && exit 0; sleep $delay; done"
    ssh(target,f"nohup sh -c {shlex.quote(cmd)} >> {run}/worker.log 2>&1 </dev/null &")


def snapshot(target,spec):
    _,run=paths(spec)
    code="import pathlib,json; p=pathlib.Path("+repr(run)+"); print(json.dumps([json.loads((p/'status.json').read_text()) if (p/'status.json').exists() else {},[(str(f.parent.relative_to(p/'episodes')),json.loads(f.read_text())) for f in (p/'episodes').glob('*/*/*/job.json')]]))"
    return json.loads(ssh(target,"python3 -c "+shlex.quote(code)))


def fetch_take(target,spec,data_dir,rel,j,sources=None):
    eid,shot,version=rel.split("/")
    if eid!=spec["episode"] or not re.fullmatch("[a-f0-9-]{36}",shot) or not re.fullmatch("[0-9]{1,22}",version):
        raise ValueError("Unexpected remote result path")
    dest=data_dir/eid/shot/version
    dest.mkdir(parents=True,exist_ok=True)
    if j["status"]!="done":
        prod.atomic(dest/"job.json",j)
        return
    if (dest/"video.mp4").is_file() and prod.read(dest/"job.json",{}).get("sha256")==j.get("sha256") and prod.digest(dest/"video.mp4")==j.get("sha256"):return
    _,run=paths(spec)
    tmp=dest/"transfer.part"
    required={"video.mp4","history.json","graph.json"}
    for name in set(filter(None, [j["settings"]["image"], *j["settings"]["refs"]])):
        if Path(name).name!=name or name not in spec["assets"]:raise ValueError("Unexpected take asset")
        source=(sources or {}).get(name)
        if source and source.is_file() and prod.digest(source)==spec["assets"][name]:
            shutil.copy2(source,dest/name)
        else:required.add(name)
    members=" ".join(shlex.quote(name) for name in sorted(required))
    with tmp.open("wb") as out:
        ssh(target,f"tar -C {run}/episodes/{rel} -cf - -- {members}",out=out,timeout=600)
    allowed=required
    with tarfile.open(tmp,"r:") as arch:
        for m in arch:
            name=m.name.removeprefix("./")
            if name not in allowed or Path(name).name!=name or not m.isfile():continue
            part=dest/(name+".part")
            with arch.extractfile(m) as src,part.open("wb") as out:
                shutil.copyfileobj(src,out)
            if name=="video.mp4":
                prod.inspect_take(part,j)
                if prod.digest(part)!=j["sha256"]:raise ValueError("Video checksum mismatch; retry sync")
            if name in spec["assets"] and prod.digest(part)!=spec["assets"][name]:raise ValueError("Archive asset checksum mismatch")
            part.replace(dest/name)
    if any(not (dest/name).is_file() for name in required):raise ValueError("Remote take is incomplete")
    if not (dest/"video.mp4").exists():raise ValueError("Remote output missing")
    prod.atomic(dest/"job.json",j)
    tmp.unlink(missing_ok=True)


class Controller:
    def __init__(self,ctx):
        self.ctx=ctx
        self.base=ctx["BASE"]
        self.dir=ctx["DATA"]/"production"
        self.dir.mkdir(exist_ok=True)
        self.ptr=self.dir/"active.json"
        self.lock=threading.RLock()
        self.thread=None
        self.monitor_stop=threading.Event()
        prior=prod.read(self.ptr,{})
        self.view=dict(status="stopped" if prior.get("abandoned") else "waiting for connection",healthy=False,episode=prior.get("episode")) if prior else {}
        self.instances={}
        self.synced=set()
        from vast_connection import Connection
        self.connection=Connection(self.dir/"connection.json",instances,ssh)

    def list_instances(self):
        rows=instances()
        self.instances={str(r["id"]):r for r in rows}
        return rows

    def files(self,spec):
        if spec.get("archive"):
            folder=self.ctx["DATA"].joinpath(*spec["archive"])
            return {name:folder/name for name in spec["assets"]}
        return {name:self.ctx["files"][name] for name in spec["assets"] if name in self.ctx["files"]}

    def current(self):
        with self.lock:
            return dict(self.view)

    def action(self,obj):
        act=obj["action"]
        if act=="instances":return self.list_instances()
        with self.lock:
            old=prod.read(self.ptr,{})
            if act=="archive":
                if not old:raise ValueError("No production session")
                if self.view.get("healthy") is not False:raise ValueError("Only disconnected tracking can be archived")
                self.monitor_stop.set()
                old["abandoned"]=True
                prod.atomic(self.ptr,old)
                self.thread=None
                self.view=dict(status="stopped",healthy=False,episode=old.get("episode"),warning="Old session tracking archived locally. Remote jobs were not stopped.")
                return {"ok":True}
            if act=="sync":
                if old.get("abandoned"):raise ValueError("This session was archived; start a new production")
                if not old:raise ValueError("No production session")
                self.watch(old,False)
                return {"ok":True}
            if act=="start":
                if self.thread and self.thread.is_alive() or old and self.view.get("status") not in ("finished","stopped","needs attention","deployment failed"):
                    raise ValueError("Finish or stop the current production first")
                target=self.connection.target() if obj.get("instance") else {"host":obj.get("host",""),"port":int(obj.get("port",22))}
                if obj.get("instance") and (not target or str(target["id"])!=str(obj["instance"])):raise ValueError("Connect the selected instance first")
                if not target:raise ValueError("Connect the selected instance first")
                db=self.ctx["project"]()
                ep=next(e for e in db["episodes"] if e["id"]==obj["episode"])
                jobs=[]; assets={}; ids=set()
                skipped={}
                sources=self.ctx["files"]
                wf=self.ctx["active_workflow"]
                rules=db["rules"]
                archive=obj.get("archive")
                if archive:
                    sid,version=archive
                    if str(uuid.UUID(sid))!=sid or not re.fullmatch(r"[0-9]{1,22}",version):
                        raise ValueError("Invalid archived take")
                    folder=self.ctx["DATA"]/ep["id"]/sid/version
                    old_job=prod.read(folder/"job.json")
                    if not old_job or old_job["status"]!="done":raise ValueError("Choose a completed take")
                    original=dict(old_job["settings"],approved=True,revision=str(uuid.uuid4()))
                    original["frozenPrompt"]=old_job["prompt"]
                    shots=[original]
                    rules=old_job["rules"]
                    sources={name:folder/name for name in filter(None, [original["image"],*original["refs"]])}
                    wf=old_job.get("workflow_profile") or self.ctx["prepare_workflow"](old_job["workflow"],prod.read(folder/"graph.json"))
                else:
                    prepared,skipped=self.ctx["actions"].approved_jobs(db,ep)
                    shots=[row[1] for row in prepared]
                    sources=self.ctx["files"]
                for s in shots:
                    if not s["approved"]:continue
                    for value in [ep["id"],s["id"]]:
                        if str(uuid.UUID(value))!=value:raise ValueError("Invalid project or shot identifier")
                    if s["id"] in ids:raise ValueError("Duplicate shot ID")
                    ids.add(s["id"])
                    previous=[(p,prod.read(p)) for p in sorted((self.ctx["DATA"]/ep["id"]/s["id"]).glob("*/job.json"),reverse=True)]
                    matching=next(((p,j) for p,j in previous if j.get("revision")==s["revision"]),None)
                    if matching:
                        p,j=matching
                        if j["status"]=="done":
                            prod.inspect_take(p.parent/"video.mp4",j)
                            continue
                        if j["status"] in ("queued","waiting","submitting","uncertain"):
                            raise ValueError("Resume the previous session for shot "+s["id"]+" before starting another.")
                    for name in filter(None,[s["image"],*s["refs"]]):
                        if Path(name).name!=name or name not in sources or not sources[name].is_file():raise ValueError("Missing asset: "+name)
                        if name not in assets: assets[name]=prod.digest(sources[name])
                    jobs.append([ep["id"],s,rules])
                if not jobs:raise ValueError(next((reason for reasons in skipped.values() for reason in reasons), "No pending approved shots"))
                rate=float(target.get("rate") or obj.get("rate",0));budget=float(obj.get("budget",0))
                minutes=int(obj["minutes"]);retries=int(obj["retries"])
                if not all(math.isfinite(v) and v>=0 for v in (rate,budget)) or not 2<=minutes<=1440 or not 0<=retries<=5 or budget and not rate:raise ValueError("Check rate, budget, minutes and retries")
                stop_when_limited=obj.get("policy")=="stop-instance"
                if stop_when_limited and not target.get("id"):raise ValueError("Select a discovered Vast instance for automatic stopping")
                spec=dict(id=uuid.uuid4().hex,episode=ep["id"],target=target, jobs=jobs,assets=assets,workflow=wf,blocked=skipped,rate=rate,budget=budget,expectedSeconds=360,retries=retries,started=time.time(),end=time.time()+60*minutes,
                          dispatch=uuid.uuid4().hex,stopOnLimit=stop_when_limited,release=hashlib.sha256("".join(prod.digest(self.base/n) for n in SOURCES).encode()).hexdigest())
                ledger=prod.read(self.dir/"costs.json",{}).get(ep["id"],{})
                spec["priorEstimate"]=sum(ledger.values())
                if budget and spec["priorEstimate"]>=budget:raise ValueError("Project budget already reached. Review previous estimated production spend.")
                if archive:spec["archive"]=[ep["id"],*archive]
                prod.atomic(self.ptr,spec)
                self.view=dict(status="deploying",current=None,total=len(jobs),episode=ep["id"],rate=rate,blocked=skipped)
                self.watch(spec,True)
                return {"ok":True,"submitted":len(jobs),"blocked":skipped}
            if not old:raise ValueError("No production session")
            if act=="resume" and self.view.get("status")=="deployment failed":
                self.view.update(status="deploying")
                self.watch(old,True)
                return {"ok":True}
            if act not in ("pause","stop","resume","retry"):raise ValueError("Unknown control")
            _,r=paths(old)
            if act=="retry":
                sid=obj.get("shot")
                if self.view.get("shots",{}).get(sid)!="error":raise ValueError("Select a failed shot ID; uncertain submissions require reconciliation")
            if act in ("resume","retry") and self.view.get("status") in ("running","starting worker","checking ComfyUI and requirements","deploying"):raise ValueError("Worker is already running")
            if self.view.get("status")=="deployment failed" and act=="stop":
                self.view.update(status="stopped")
                old["abandoned"]=True;prod.atomic(self.ptr,old)
                return {"ok":True}
            put_json(old["target"],r+"/control.json",{"action":act,"retry":obj.get("shot") if act=="retry" else None})
            if act in ("resume","retry"):
                if time.time()>=old["end"]-60:
                    minutes=int(obj.get("minutes",60))
                    if not 2<=minutes<=1440:raise ValueError("Invalid session length")
                    old["end"]=time.time()+minutes*60
                    prod.atomic(self.ptr,old)
                if "budget" in obj:
                    budget=float(obj["budget"])
                    if not math.isfinite(budget) or budget<0 or budget>0 and not old["rate"]:raise ValueError("Invalid budget for this production")
                    old["budget"]=budget
                old["dispatch"]=uuid.uuid4().hex
                prod.atomic(self.ptr,old)
                put_json(old["target"],r+"/spec.json",old)
                launch(old["target"],old)
                self.view.update(status="reconnecting")
            if act=="stop" and self.view.get("status") in TERMINAL:
                old["abandoned"]=True;prod.atomic(self.ptr,old);self.view.update(status="stopped")
                return {"ok":True}
            self.watch(old,False)
            return {"ok":True}

    def watch(self,spec,prepare):
        if self.thread and self.thread.is_alive():return
        stop=threading.Event()
        self.monitor_stop=stop
        def monitor():
            target=spec["target"]
            recoveries=0
            try:
                if prepare:
                    for attempt in range(spec["retries"]+1):
                        try:
                            deploy(self.base,target,spec,self.files(spec))
                            launch(target,spec)
                            break
                        except (OSError,subprocess.TimeoutExpired):
                            if attempt>=spec["retries"]:raise
                            time.sleep(2**attempt)
            except Exception as e:
                self.view.update(status="deployment failed",error=str(e),healthy=False)
                return
            failures=0
            while not stop.is_set():
                try:
                    if target.get("id"):
                        current=self.connection.target()
                        if str(current["id"])!=str(target["id"]): raise ValueError("Connect the production instance first")
                        target=current
                    state,jobs=snapshot(target,spec)
                    if stop.is_set():return
                    for rel,j in jobs:
                        key=(spec["id"],rel,j["status"],j.get("sha256"))
                        if key not in self.synced:
                            fetch_take(target,spec,self.ctx["DATA"],rel,j,self.files(spec))
                            self.synced.add(key)
                    self.view=dict(state,healthy=True,episode=spec["episode"],total=len(spec["jobs"]),rate=spec["rate"],budget=spec["budget"],blocked=spec.get("blocked",{}))
                    if state and "estimate" in state:
                        cost_file=self.dir/"costs.json"
                        costs=prod.read(cost_file,{})
                        values=costs.setdefault(spec["episode"],{})
                        if values.get(spec["id"])!=state["estimate"]:
                            values[spec["id"]]=state["estimate"]
                            prod.atomic(cost_file,costs)
                    if not state:
                        self.view.update(status="starting worker")
                        if time.time()-spec["started"]>240:
                            self.view.update(status="needs attention",error="Worker did not report readiness. Check worker.log and whether another AXDAR run holds the instance lock.")
                            return
                    elif state.get("status") in TERMINAL and state.get("dispatch")==spec.get("dispatch"):
                        if state["status"]=="limit reached" and spec.get("stopOnLimit") and not spec.get("stopRequested") and not any(j.get("status") in ("uncertain","waiting","submitting","queued") for _,j in jobs):
                            # Check the entire instance queue so a manual or unrelated job is preserved.
                            _,run=paths(spec)
                            check="import json,urllib.request as u; q=json.load(u.urlopen("+repr(state.get("url","")+"/queue")+",timeout=5));print(int(not(q['queue_running'] or q['queue_pending'])))"
                            if ssh(target,"python3 -c "+shlex.quote(check)).strip()==b"1":
                                request_instance_stop(target)
                                spec["stopRequested"]=True
                                prod.atomic(self.ptr,spec)
                                self.view["warning"]="Vast accepted the stop request."
                            else:self.view["warning"]="Instance left running because ComfyUI has other jobs."
                        return
                    elif state.get("status")=="running" and time.time()-state.get("updated",time.time())>180:
                        self.view["warning"]="Worker heartbeat delayed. Checking the instance lock."
                        # A live worker owns production.lock even during a long API call.
                        if recoveries<spec["retries"] and ssh(target,"flock -n "+STORE+"/production.lock true && echo free || echo busy").strip()==b"free":
                            launch(target,spec)
                            recoveries+=1
                    elif state.get("current") and time.time()-state.get("shotStarted",time.time())>max(900,3*(state.get("meanSeconds") or spec["expectedSeconds"])):self.view["warning"]="Shot is taking longer than expected; inspect ComfyUI progress."
                    failures=0
                except Exception as e:
                    if stop.is_set():return
                    failures+=1
                    self.view.update(healthy=False,error=str(e)[:600],status="reconnecting")
                    if failures>=6:
                        self.view.update(status="connection lost")
                        return
                stop.wait(min(30,4*max(failures,1)))
        self.thread=threading.Thread(target=monitor,daemon=True)
        self.thread.start()

    def reconnect(self):
        spec=prod.read(self.ptr)
        if spec and spec.get("stopRequested"):
            self.view=dict(status="stopped",warning="Vast accepted the stop request.")
        elif spec and not spec.get("abandoned"):
            self.view=dict(status="reconnecting",episode=spec["episode"])
            self.watch(spec,False)

