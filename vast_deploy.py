"""Vast instance discovery and deployment over the user's verified OpenSSH connection."""
import hashlib
from datetime import datetime, timezone
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
import drive_archive
from production_services import webhook_url
import take_quality

STORE="/workspace/axdar-studio"
SOURCES=("worker.py","production.py","vast_worker.py","vast_deploy.py","studio.pyw","studio_actions.py","story_engine.py","workflow_profiles.py","provision.py","model-lock.json","vast_connection.py","vast_ssh_setup.py","drive_archive.py","production_services.py","take_quality.py","editor_media.py","episode_editor.py","freecut_bridge.py","workflow_manager.py","workflow_install_remote.py","direct_session.py")
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
                rate=row.get("dph_total"),gpu=row.get("gpu_name","GPU"),uptime=row.get("uptime_mins"))


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


def month_key():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def billing_charges(instance):
    from vast_connection import api_key
    key=api_key()
    if not key:
        raise ValueError("Save the Vast API key before reading billing.")
    now=datetime.now(timezone.utc)
    filters={"day":{"gte":int(now.replace(day=1,hour=0,minute=0,second=0,microsecond=0).timestamp()),"lte":int(now.timestamp())}}
    params={"select_filters":json.dumps(filters),"limit":100}
    account=selected=0
    seen=set()
    while True:
        req=Request("https://console.vast.ai/api/v0/charges?"+urlencode(params),headers={"Authorization":"Bearer "+key})
        with urlopen(req,timeout=15) as stream:
            data=json.load(stream)
        if not data.get("success"):
            raise ValueError("Vast did not return confirmed charges.")
        for row in data["results"]:
            amount=float(row["amount"])
            if not math.isfinite(amount):
                raise ValueError("Vast returned an invalid charge.")
            account+=amount
            if instance and row.get("source")==f"instance-{int(instance)}":
                selected+=amount
        next_token=data.get("next_token")
        if not next_token:
            return {"month":month_key(),"confirmedAccount":round(account,4),"confirmedInstance":round(selected,4) if instance else None}
        if next_token in seen:
            raise ValueError("Vast returned a repeated billing page.")
        seen.add(next_token)
        params["after_token"]=next_token


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
    from vast_connection import identity_options
    host=str(target["host"])
    port=int(target["port"])
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}",host) or not 1<=port<=65535:
        raise ValueError("Invalid SSH host or port")
    args=["ssh","-T","-o","BatchMode=yes","-o","StrictHostKeyChecking=yes","-o","ConnectTimeout=8",
          "-o","ServerAliveInterval=15","-o","ServerAliveCountMax=2",*identity_options(target),"-p",str(port),"root@"+host,command]
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


def deploy_files(target, planned):
    # One remote check for the whole manifest; transmit only missing or damaged files.
    manifest="".join(f"{prod.digest(src)}  {dst}\n" for dst,src in planned.items())
    check=ssh(target,"while read -r hash path; do echo \"$hash  $path\" | sha256sum -c --status 2>/dev/null || echo \"$path\"; done",data=manifest.encode())
    for dst in check.decode().splitlines():
        if dst not in planned:raise ValueError("Unexpected checksum response from SSH host")
        if dst.startswith(STORE+"/blobs/") and prod.digest(planned[dst])!=dst.rsplit("/",1)[1]:
            raise ValueError("An asset changed during deployment. Start a new snapshot.")
        put(target,dst,planned[dst])


def prepare_direct(ctx, target, jobs, workflow, timeout, repair=True):
    from vast_worker import runtime_requirements
    base = ctx["BASE"]
    requirements = runtime_requirements(ctx, {"jobs":jobs}, workflow)
    revision = hashlib.sha256("".join(prod.digest(base/n) for n in SOURCES).encode()).hexdigest()
    release = STORE + "/releases/" + revision
    ssh(target, f"umask 077; mkdir -p {release}")
    deploy_files(target, {release+"/"+name:base/name for name in SOURCES})
    code = ("import json, sys\nfrom vast_worker import ready\n"
            "from urllib.request import urlopen\n"
            "def api(url, path):\n"
            " with urlopen(url + path, timeout=15) as response: return json.load(response)\n"
            f"try: ready({{'json_api':api}}, json.load(sys.stdin), repair={repair})\n"
            "except Exception as exc: sys.exit(str(exc))")
    try:
        ssh(target, f"cd {release} && python3 -c {shlex.quote(code)}",
            data=json.dumps(requirements).encode(), timeout=max(15, timeout()))
    except subprocess.TimeoutExpired:
        raise ValueError("ComfyUI dependency setup ran out of session time.") from None
    except OSError as exc:
        raise ValueError(str(exc).splitlines()[-1]) from None


def deploy(base,target,spec,assets):
    release,run=paths(spec)
    ssh(target,f"umask 077; mkdir -p {release} {run} {STORE}/blobs")
    planned={release+"/"+name:base/name for name in SOURCES}
    planned.update({STORE+"/blobs/"+sha:assets[name] for name,sha in spec["assets"].items()})
    deploy_files(target, planned)
    put_json(target,run+"/spec.json",spec)


def launch_maintenance(target,spec):
    release,run=paths(spec)
    cmd=f"python3 {release}/vast_worker.py {run} --maintenance"
    ssh(target,f"nohup sh -c {shlex.quote(cmd)} >> {run}/worker.log 2>&1 </dev/null &")


def launch(target,spec):
    release,run=paths(spec)
    # flock inside the worker serializes runs; the watchdog retries an interrupted interpreter.
    cmd=f"for delay in 1 4 12; do python3 {release}/worker.py --production {run} && exit 0; sleep $delay; done"
    ssh(target,f"nohup sh -c {shlex.quote(cmd)} >> {run}/worker.log 2>&1 </dev/null &")


def snapshot(target,spec):
    _,run=paths(spec)
    code=f"""
import json
from pathlib import Path
p=Path({run!r})
def read(f,default):
    try: return json.loads(f.read_text())
    except FileNotFoundError: return default
state=read(p/'status.json',{{}})
lifecycle=read(p/'lifecycle.json',{{}})
cancel=read(p/'lifecycle-cancel.json',{{}})
if cancel: lifecycle=dict(status='cancelled',**cancel)
state.update(lifecycle=lifecycle, notifications=list(read(p/'events.json',{{}}).values()))
jobs=[]
for f in (p/'episodes').glob('*/*/*/job.json'):
    job=read(f,{{}})
    review=read(f.parent/'review.json',{{}})
    if review: job['review']=review['decision']
    jobs.append((str(f.parent.relative_to(p/'episodes')),job))
print(json.dumps([state,jobs]))
"""
    return json.loads(ssh(target,"python3 -c "+shlex.quote(code)))


def take_path(data_dir,episode,shot,version):
    for value in (episode,shot):
        if not isinstance(value,str) or str(uuid.UUID(value))!=value:
            raise ValueError("Invalid episode or shot ID.")
    if not isinstance(version,str) or not re.fullmatch(r"[0-9]{1,22}",version):
        raise ValueError("Invalid take version.")
    return data_dir/episode/shot/version


def fetch_take(target,spec,data_dir,rel,j,sources=None,preview=False):
    eid,shot,version=rel.split("/")
    if eid!=spec["episode"] or j.get("shot")!=shot:
        raise ValueError("Unexpected remote result path.")
    dest=take_path(data_dir,eid,shot,version)
    dest.mkdir(parents=True,exist_ok=True)
    j={**j,"remote_run":spec["id"]}
    if j["status"]!="done" or j.get("review") in ("required","rejected") and not preview:
        prod.atomic(dest/"job.json",j)
        return
    video=dest/("preview.mp4" if preview else "video.mp4")
    if video.is_file() and prod.digest(video)==j.get("sha256"):
        prod.atomic(dest/"job.json",j)
        return
    _,run=paths(spec)
    tmp=dest/"transfer.part"
    required={"video.mp4","history.json","graph.json"}
    for name in set(filter(None, [j["settings"]["image"], *j["settings"]["refs"]])):
        if Path(name).name!=name or name not in spec["assets"]:
            raise ValueError("Unexpected take asset.")
        source=(sources or {}).get(name)
        if source and source.is_file() and prod.digest(source)==spec["assets"][name]:
            shutil.copy2(source,dest/name)
        else:
            required.add(name)
    members=" ".join(shlex.quote(name) for name in sorted(required))
    try:
        with tmp.open("wb") as out:
            ssh(target,f"tar -C {run}/episodes/{rel} -cf - -- {members}",out=out,timeout=600)
        received=set()
        with tarfile.open(tmp,"r:") as arch:
            for m in arch:
                name=m.name.removeprefix("./")
                if name not in required or not m.isfile():
                    continue
                target_file=video if name=="video.mp4" else dest/name
                part=target_file.with_name(target_file.name+".part")
                with arch.extractfile(m) as src,part.open("wb") as out:
                    shutil.copyfileobj(src,out)
                if name=="video.mp4":
                    prod.inspect_take(part,j)
                    if prod.digest(part)!=j["sha256"]:
                        raise ValueError("Video checksum mismatch; retry sync.")
                if name in spec["assets"] and prod.digest(part)!=spec["assets"][name]:
                    raise ValueError("Archive asset checksum mismatch.")
                part.replace(target_file)
                received.add(name)
        if received!=required:
            raise ValueError("Remote take transfer was incomplete.")
        prod.atomic(dest/"job.json",j)
    finally:
        tmp.unlink(missing_ok=True)


class Controller:
    def __init__(self,ctx):
        self.ctx=ctx
        self.base=ctx["BASE"]
        self.dir=ctx["DATA"]/"production"
        self.dir.mkdir(exist_ok=True)
        self.ptr=self.dir/"active.json"
        self.runs=self.dir/"runs"
        self.runs.mkdir(exist_ok=True)
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

    def resolve_target(self,spec):
        target=spec["target"]
        if target.get("id"):
            try:
                current=self.connection.ssh_target()
                if str(current["id"])==str(target["id"]):
                    return current
            except ValueError:
                pass
            return self.connection.resolve(target["id"])
        return target

    def private_config(self,spec):
        config={}
        if spec.get("backupDrive"):
            config["drive"]=drive_archive.account()
        for field in ("webhook","visual"):
            if spec.get(field):
                saved=drive_archive.vault().get_password(drive_archive.SERVICE,field)
                if saved:
                    config[field]=saved
        return config

    def save_run(self,spec):
        prod.atomic(self.ptr,spec)
        prod.atomic(self.runs/(spec["id"]+".json"),spec)

    def import_review(self,obj):
        shot,version=obj.get("shot"),obj.get("version")
        ep=obj["episode"]
        folder=take_path(self.ctx["DATA"],ep,shot,version)
        j=prod.read(folder/"job.json")
        if not j or j["status"]!="done":
            raise ValueError("Select a completed take for review.")
        decision=obj.get("decision")
        if decision not in (None,"accepted","rejected"):
            raise ValueError("Unknown review decision.")
        if decision=="accepted" and j.get("quality",{}).get("technical")!="passed":
            raise ValueError("Recheck the take after installing FFmpeg before approval.")
        if j.get("remote_run"):
            rid=uuid.UUID(j["remote_run"]).hex
            spec=prod.read(self.runs/(rid+".json"),{})
            if not spec:
                raise ValueError("This take's Vast production session is unavailable.")
            target=self.resolve_target(spec)
            release,run=paths(spec)
            rel=f"{ep}/{shot}/{version}"
            preview=folder/"preview.mp4"
            if obj.get("recheck"):
                command=f"python3 {release}/take_quality.py {run}/episodes/{rel}"
                j["quality"]=json.loads(ssh(target,command,timeout=420))
            if not obj.get("recheck") and not (preview.exists() or (folder/"video.mp4").exists()):
                fetch_take(target,spec,self.ctx["DATA"],rel,j,self.files(spec),preview=True)
            if decision or obj.get("recheck"):
                put_json(target,run+"/lifecycle-cancel.json",dict(reason="Take review changed. Verify the updated archive."))
            if decision:
                if decision=="accepted" and preview.exists() and prod.digest(preview)!=j["sha256"]:
                    raise ValueError("The preview file no longer matches the archived take.")
                put_json(target,run+f"/episodes/{rel}/review.json",dict(decision=decision,time=time.time(),note=str(obj.get("note",""))[:600]))
                if decision=="accepted" and preview.exists():
                    preview.replace(folder/"video.mp4")
        elif j.get("review") is None:
            raise ValueError("No Package 2 review data for this legacy take.")
        elif obj.get("recheck"):
            j["quality"]=take_quality.check(folder/"video.mp4",j)
        if decision:
            j["review"]=decision
        if decision or obj.get("recheck"):
            prod.atomic(folder/"job.json",j)
        return {"ok":True}

    def action(self,obj):
        act=obj["action"]
        if act=="instances":return self.list_instances()
        if act=="charges":return billing_charges(obj.get("instance"))
        if act=="review":return self.import_review(obj)
        if act=="service-settings":
            vault=drive_archive.vault()
            for field in ("webhook","visual"):
                if obj.get(field):
                    vault.set_password(drive_archive.SERVICE,field,webhook_url(obj[field]))
            return {"ok":True}
        with self.lock:
            old=prod.read(self.ptr,{})
            if act in ("maintenance","cancel-countdown"):
                if not old.get("package2"):
                    raise ValueError("Choose a Package 2 production session.")
                target=self.resolve_target(old)
                _,run=paths(old)
                if act=="cancel-countdown":
                    put_json(target,run+"/lifecycle-cancel.json",dict(reason="Cancelled by user in AXDAR."))
                else:
                    if self.view.get("status") not in TERMINAL or self.view.get("maintenance"):
                        raise ValueError("Wait for rendering and archive processing to finish.")
                    if obj.get("backupDrive") is True:
                        if not drive_archive.account():
                            raise ValueError("Authorize Drive before enabling backup.")
                        old["backupDrive"]=True
                    for field in ("webhook","visual"):
                        if obj.get(field) is True:
                            old[field]=True
                    self.save_run(old)
                    put_json(target,run+"/spec.json",old)
                    put_json(target,run+"/services-private.json",self.private_config(old))
                    launch_maintenance(target,old)
                self.watch(old,False)
                return {"ok":True}
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
                if obj.get("instance") or obj.get("useConnection"):
                    target=self.connection.ssh_target()
                else:
                    target={"host":obj.get("host",""),"port":int(obj.get("port",22))}
                if obj.get("instance") and (not target or str(target.get("id"))!=str(obj["instance"])):raise ValueError("Connect the selected instance first")
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
                        if j["status"]=="done" and j.get("review")!="rejected":
                            if (p.parent/"video.mp4").exists():
                                prod.inspect_take(p.parent/"video.mp4",j)
                            continue
                        if j["status"] in ("queued","waiting","submitting","uncertain"):
                            raise ValueError("Resume the previous session for shot "+s["id"]+" before starting another.")
                    for name in filter(None,[s["image"],*s["refs"]]):
                        if Path(name).name!=name or name not in sources or not sources[name].is_file():raise ValueError("Missing asset: "+name)
                        if name not in assets: assets[name]=prod.digest(sources[name])
                    jobs.append([ep["id"],s,rules])
                if not jobs:raise ValueError(next((reason for reasons in skipped.values() for reason in reasons), "No pending approved shots"))
                rate=float(target.get("rate") or obj.get("rate",0))
                caps={key:float(obj.get(key,0)) for key in ("budget","episodeBudget","monthBudget")}
                minutes=int(obj["minutes"])
                retries=int(obj["retries"])
                shot_retries=int(obj.get("shotRetries",1))
                width,height=int(obj.get("expectedWidth") or 0),int(obj.get("expectedHeight") or 0)
                if (width or height) and (not 16<=width<=16384 or not 16<=height<=16384):
                    raise ValueError("Set both expected dimensions between 16 and 16,384 pixels, or leave both blank.")
                if width and height:
                    for row in jobs:
                        row[1]["resolution"]=[width,height]
                if (not all(math.isfinite(v) and v>=0 for v in (rate,*caps.values())) or
                        not 2<=minutes<=1440 or not 0<=retries<=5 or not 0<=shot_retries<=5 or any(caps.values()) and not rate):
                    raise ValueError("Check the rate, budgets, time limit and retries.")
                stop_when_limited=obj.get("policy")=="stop-instance"
                if stop_when_limited and not target.get("id"):
                    raise ValueError("Select a discovered Vast instance for automatic stopping.")
                now=time.time()
                spec=dict(id=uuid.uuid4().hex,episode=ep["id"],project=db["id"],target=target,jobs=jobs,assets=assets,workflow=wf,
                          blocked=skipped,rate=rate,expectedSeconds=360,retries=retries,shotRetries=shot_retries,
                          started=now,end=now+60*minutes,package2=True,month=month_key(),budget=caps["budget"],
                          episodeBudget=caps["episodeBudget"],monthBudget=caps["monthBudget"],
                          backupDrive=obj.get("backupDrive") is True,webhook=obj.get("webhook") is True,
                          visual=obj.get("visual") is True,lifecycle="rehearse" if obj.get("lifecycle")=="rehearse" else "manual",
                          dispatch=uuid.uuid4().hex,stopOnLimit=stop_when_limited,
                          release=hashlib.sha256("".join(prod.digest(self.base/n) for n in SOURCES).encode()).hexdigest())
                ledger=prod.read(self.dir/"costs.json",{}).get(ep["id"],{})
                spec["priorEstimate"]=sum(ledger.values())
                months=prod.read(self.dir/"monthly-costs.json",{}).get(spec["month"],{})
                spec["priorMonthEstimate"]=sum(months.values())
                if caps["monthBudget"]:
                    try:
                        billed=billing_charges(target.get("id"))
                        spec["priorMonthEstimate"]=max(spec["priorMonthEstimate"],billed["confirmedAccount"])
                    except (ValueError,OSError,KeyError):
                        spec["monthlyBudgetSource"]="AXDAR estimate only. Vast billing was not available."
                    else:
                        spec["monthlyBudgetSource"]="Greater of recorded AXDAR estimate and confirmed Vast account charges."
                if caps["episodeBudget"] and spec["priorEstimate"]>=caps["episodeBudget"]:
                    raise ValueError("The estimated episode budget is exhausted.")
                if caps["monthBudget"] and spec["priorMonthEstimate"]>=caps["monthBudget"]:
                    raise ValueError("The estimated monthly budget is exhausted.")
                if spec["backupDrive"] and not drive_archive.account():
                    raise ValueError("Authorize Google Drive first or leave Drive backup disabled.")
                for field in ("webhook","visual"):
                    if spec.get(field) and not drive_archive.vault().get_password(drive_archive.SERVICE,field):
                        raise ValueError(f"Save the {field} endpoint first or disable it for this run.")
                if archive:
                    spec["archive"]=[ep["id"],*archive]
                self.save_run(spec)
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
            if act in ("resume","retry") and (self.view.get("maintenance") or self.view.get("status") in
                                              ("running","starting worker","checking ComfyUI and requirements","deploying")):
                raise ValueError("Wait for the worker or archive processing to finish.")
            if self.view.get("status")=="deployment failed" and act=="stop":
                self.view.update(status="stopped")
                old["abandoned"]=True;prod.atomic(self.ptr,old)
                return {"ok":True}
            target=self.resolve_target(old)
            put_json(target,r+"/control.json",{"action":act,"retry":obj.get("shot") if act=="retry" else None})
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
                put_json(target,r+"/spec.json",old)
                launch(target,old)
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
                            target=self.resolve_target(spec)
                            deploy(self.base,target,spec,self.files(spec))
                            if spec.get("package2"):
                                _,run=paths(spec)
                                put_json(target,run+"/services-private.json",self.private_config(spec))
                            launch(target,spec)
                            break
                        except (OSError,subprocess.TimeoutExpired):
                            if attempt>=spec["retries"]:raise
                            time.sleep(2**attempt)
            except Exception as e:
                self.view.update(status="deployment failed",error=str(e),healthy=False)
                return
            failures=0
            state_checked=False
            while not stop.is_set():
                try:
                    if failures or target.get("id") and not state_checked:
                        target=self.resolve_target(spec)
                    state,jobs=snapshot(target,spec)
                    state_checked=True
                    if stop.is_set():return
                    for rel,j in jobs:
                        key=(spec["id"],rel,j["status"],j.get("sha256"),j.get("review"),json.dumps(j.get("quality"),sort_keys=True))
                        if key not in self.synced:
                            fetch_take(target,spec,self.ctx["DATA"],rel,j,self.files(spec))
                            self.synced.add(key)
                    self.view=dict(state,healthy=True,episode=spec["episode"],total=len(spec["jobs"]),rate=spec["rate"],budget=spec["budget"],blocked=spec.get("blocked",{}))
                    accepted={j["shot"]:j for _,j in sorted(jobs) if j["status"]=="done" and j.get("review") in ("accepted",None)}
                    accepted_seconds=sum(j.get("duration",0) for j in accepted.values())
                    self.view.update(acceptedTakes=len(accepted),acceptedSeconds=accepted_seconds,
                                     acceptedSecondsPerDollar=accepted_seconds/state["estimate"] if state.get("estimate") else None,
                                     instanceUptimeMinutes=target.get("uptime"),monthlyBudgetSource=spec.get("monthlyBudgetSource"))
                    if state and "estimate" in state:
                        cost_file=self.dir/"costs.json"
                        costs=prod.read(cost_file,{})
                        values=costs.setdefault(spec["episode"],{})
                        if values.get(spec["id"])!=state["estimate"]:
                            values[spec["id"]]=state["estimate"]
                            prod.atomic(cost_file,costs)
                        if spec.get("month"):
                            monthly_path=self.dir/"monthly-costs.json"
                            months=prod.read(monthly_path,{})
                            runs=months.setdefault(spec["month"],{})
                            if runs.get(spec["id"])!=state["estimate"]:
                                runs[spec["id"]]=state["estimate"]
                                prod.atomic(monthly_path,months)
                    if not state:
                        self.view.update(status="starting worker")
                        if time.time()-spec["started"]>240:
                            self.view.update(status="needs attention",error="Worker did not report readiness. Check worker.log and whether another AXDAR run holds the instance lock.")
                            return
                    elif (state.get("status") in TERMINAL and state.get("dispatch")==spec.get("dispatch")
                          and not state.get("maintenance") and state.get("lifecycle",{}).get("status")!="countdown"):
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

