"""Workflow installation orchestration on the existing registered Vast connection."""
import json
import re
import subprocess
import threading
import time
import uuid
from urllib.error import HTTPError
from urllib.request import Request, build_opener

import production
from provision import ModelRedirect
import vast_deploy
from vast_connection import hf_token
from workflow_install_remote import PHASES, redact
from workflow_profiles import _parse, _upstream, public_profile


INSTALL_ROOT = vast_deploy.STORE + "/installations"
TERMINAL = {"ready","needs attention","failed","cancelled","timed out"}
FILES = ("workflow_install_remote.py","production.py","provision.py","model-lock.json")


def identity(target):
    return str(target.get("id") or (str(target["host"])+":"+str(target["port"])))


class WorkflowManager:
    def __init__(self, ctx):
        self.ctx=ctx
        self.connection=ctx["controller"].connection
        self.path=ctx["DATA"]/"production"/"workflow-installation.json"
        self.profiles=ctx["DATA"]/"production"/"workflow-installation-profiles"
        self.lock=threading.RLock()
        self.thread=None
        self.record=production.read(self.path,{})
        self.telemetry={}
        self.telemetry_lock=threading.Lock()

    def save(self, **changes):
        with self.lock:
            self.record.update(changes)
            production.atomic(self.path,self.record)

    def busy(self):
        with self.lock:
            return bool(self.record and self.record.get("status") not in TERMINAL)

    def profile_path(self, version):
        if not isinstance(version,str) or not re.fullmatch(r"[0-9a-f]{64}",version):
            raise ValueError("Unknown workflow version.")
        return self.profiles/(version+".json")

    def profile(self, version):
        return production.read(self.profile_path(version),{})

    def save_profile(self, version, **changes):
        with self.lock:
            data=self.profile(version)
            data.update(changes)
            self.profiles.mkdir(parents=True,exist_ok=True)
            production.atomic(self.profile_path(version),data)

    def action(self, request):
        mode=request.get("action")
        if mode=="token":
            token=request.get("token","").strip()
            if not re.fullmatch(r"hf_[A-Za-z0-9]{8,256}",token):
                raise ValueError("Enter a Hugging Face access token.")
            try:
                with build_opener(ModelRedirect()).open(Request("https://huggingface.co/api/whoami-v2",
                                     headers={"Authorization":"Bearer "+token}),timeout=12) as response:
                    if response.status!=200:
                        raise ValueError("Hugging Face did not accept the token.")
            except HTTPError as exc:
                raise ValueError(f"Hugging Face token check returned HTTP {exc.code}.") from None
            except OSError:
                raise ValueError("Could not reach Hugging Face to verify the token.") from None
            hf_token(token)
            return {"valid":True}
        if mode=="prepare":
            return self.prepare(request.get("version"))
        if mode=="status":
            return self.status(request.get("version"),request.get("monitor") is True)
        if mode=="cancel":
            with self.lock:
                if not self.busy():
                    raise ValueError("No active installation to cancel.")
                self.save(cancel=True,message="Cancellation requested")
                self.watch()
            return {"ok":True}
        if mode not in ("scan","install"):
            raise ValueError("Unknown Workflow Manager action.")
        version=request.get("version")
        wf=self.ctx["actions"].store.get(version)
        if not wf["executable"]:
            raise ValueError("Prepare the original workflow on the connected ComfyUI instance first.")
        if not wf["output"] or wf["output"] not in wf["graph"]:
            raise ValueError("Map the final MP4 output node first.")
        script=request.get("script","") if mode=="install" else ""
        if not isinstance(script,str) or len(script)>256000:
            raise ValueError("The optional Bash script must be under 256 KB.")
        if redact(script)!=script:
            raise ValueError("Use the HF_TOKEN environment variable in Bash instead of inserting a token.")
        if mode=="install" and request.get("confirmed") is not True:
            raise ValueError("Preview and confirm execution on the selected Vast instance.")
        timeout=int(request.get("timeout",30))
        if not 1<=timeout<=180:
            raise ValueError("Choose an installation timeout of 1–180 minutes.")
        with self.ctx["LOCK"], self.lock:
            if self.ctx["running"] or self.ctx["production_busy"]() or self.busy():
                raise ValueError("Finish the active production or installation first.")
            target=self.connection.target()
            if mode=="install" and request.get("instance") is not None and str(target.get("id"))!=str(request["instance"]):
                raise ValueError("The selected instance changed. Review the installation again.")
            plan={k:wf["graph"][k] for k in _upstream(wf["graph"],[wf["output"]])}
            if mode=="install":
                self.save_profile(version,script=script)
            self.record=dict(id=uuid.uuid4().hex,version=version,instance=identity(target),
                             rate=target.get("rate"),status="starting",started=time.time(),mode=mode,
                             timeout=timeout*60,stages={},report={},log="")
            self.save()
            spec=dict(graph=plan,mode=mode,timeout=timeout*60,restart=request.get("restart") is True)
            threading.Thread(target=self.launch,args=(target,spec,script),daemon=True).start()
        return {"ok":True}

    def prepare(self, version):
        store=self.ctx["actions"].store
        wf=store.get(version)
        starting_active=(self.ctx["active_workflow"] or {}).get("version")
        capture=self.profile(version).get("capture")
        if capture:
            wf=store.get(capture)
        target=self.connection.target()
        url=self.connection.snapshot().get("url")
        if not url:
            raise ValueError("Connect to ComfyUI to prepare the original workflow.")
        if not wf["executable"]:
            raw=store.original(version)
            ui=json.loads(raw.decode("utf-8-sig"))
            ui=ui.get("workflow",ui.get("graph",ui))
            try:
                capture=self.ctx["json_api"](url,"/workflow/convert",ui)
                graph=_parse(capture)
            except (ValueError,OSError):
                raise ValueError("ComfyUI could not convert this workflow. Check its Workflow to API Converter and use the original JSON again.") from None
            with self.ctx["LOCK"]:
                if identity(self.connection.target())!=identity(target):
                    raise ValueError("The instance changed during conversion. Prepare the workflow on the new connection.")
                wf=store.attach(version,json.dumps(graph,ensure_ascii=False).encode("utf-8"))
                self.save_profile(version,capture=wf["version"])
        with self.ctx["LOCK"]:
            current_active=(self.ctx["active_workflow"] or {}).get("version")
            if (current_active==starting_active and not wf["issues"] and not self.ctx["running"]
                    and not self.ctx["production_busy"]()):
                self.ctx["actions"].workflow("select",{"version":wf["version"]})
            active=self.ctx["active_workflow"]
        message=None
        previous=self.profile(wf["version"]).get("instances",{}).get(identity(target))
        if not previous and wf["output"] and not wf["issues"]:
            try:
                self.action({"action":"scan","version":wf["version"]})
            except ValueError as exc:
                message=str(exc)
        return dict(profile=public_profile(wf),active=active["version"] if active else None,message=message)

    def folder(self):
        return INSTALL_ROOT+"/"+self.record["id"]

    def launch(self, target, spec, script):
        folder=self.folder()
        self.save(message="Uploading the reviewed installation plan")
        try:
            spec["port"]=self.connection._remote_port(target)
            vast_deploy.ssh(target,f"umask 077; mkdir -p {folder}")
            base=self.ctx["BASE"]
            vast_deploy.deploy_files(target,{folder+"/"+name:base/name for name in FILES})
            vast_deploy.put_json(target,folder+"/spec.json",spec)
            if script.strip():
                vast_deploy.ssh(target,f"umask 077; cat > {folder}/install.sh",
                                data=script.replace("\r\n","\n").encode())
            if self.record.get("cancel"):
                self.save(status="cancelled",finished=time.time(),message="Cancelled before launch")
                return
            token=(hf_token() or "") if spec["mode"]=="install" else ""
            self.save(dispatched=True,message="Waiting for remote installer")
            command=("IFS= read -r HF_TOKEN; export HF_TOKEN; "
                     "HUGGING_FACE_HUB_TOKEN=$HF_TOKEN; export HUGGING_FACE_HUB_TOKEN; "
                     f"umask 077; nohup python3 {folder}/workflow_install_remote.py {folder}"
                     " </dev/null >/dev/null 2>&1 &")
            vast_deploy.ssh(target,command,data=(token+"\n").encode(),timeout=15)
        except Exception:
            if not self.record.get("dispatched"):
                self.save(status="failed",finished=time.time(),message="Remote setup could not be uploaded over SSH.")
                return
            self.save(status="reconnecting",message="Launch acknowledgement lost. Checking the original remote operation.")
        self.watch()

    def watch(self):
        with self.lock:
            if not self.busy() or not self.record.get("dispatched") or self.thread and self.thread.is_alive():
                return
            self.thread=threading.Thread(target=self.reconcile,args=(self.record["id"],),daemon=True)
            self.thread.start()

    def reconcile(self, operation):
        folder=INSTALL_ROOT+"/"+operation
        while self.record.get("id")==operation and self.busy():
            try:
                target=self.connection.ssh_target()
                if identity(target)!=self.record["instance"]:
                    raise ValueError("Select the original instance to reconcile installation.")
                if self.record.get("cancel"):
                    vast_deploy.ssh(target,f"umask 077; touch {folder}/cancel",timeout=10)
                data=vast_deploy.ssh(target,f"python3 {folder}/workflow_install_remote.py {folder} snapshot",timeout=10)
                state=json.loads(data)
                if not state.get("started"):
                    self.save(status="reconnecting",message="Remote process is not yet confirmed. It will not be restarted automatically.")
                    if time.time()>self.record["started"]+self.record["timeout"]+60:
                        self.save(status="failed",finished=time.time(),message="Remote launch did not start.")
                else:
                    state.pop("started")
                    state.setdefault("status","running")
                    with self.lock:
                        self.save(message="",**state)
                        if state["status"] in ("ready","needs attention") and state.get("report"):
                            version=self.record["version"]
                            profiles=self.profile(version).get("instances",{})
                            profiles[self.record["instance"]]={k:self.record.get(k) for k in ("status","report","finished")}
                            self.save_profile(version,instances=profiles)
            except (ValueError,OSError,TimeoutError,subprocess.SubprocessError) as exc:
                message=("Reconnect the original Vast instance to check this installation."
                         if isinstance(exc,ValueError) else "SSH interrupted. Checking the same remote installation.")
                self.save(status="reconnecting",message=message)
            time.sleep(2)

    def status(self, version=None, monitor=False):
        if self.busy() and not self.record.get("dispatched") and time.time()-self.record["started"]>300:
            self.save(status="failed",finished=time.time(),message="AXDAR closed before it launched the remote operation.")
        self.watch()
        if monitor and self.telemetry_lock.acquire(blocking=False):
            threading.Thread(target=self.refresh_telemetry,daemon=True).start()
        with self.lock:
            view={key:self.record.get(key) for key in ("version","instance","status","rate","started","finished","mode","stages",
                                                       "report","log","message","exitCode","bashExitCode")}
            if self.record:
                view["elapsed"]=(self.record.get("finished") or time.time())-self.record["started"]
            version=version if isinstance(version,str) and re.fullmatch(r"[0-9a-f]{64}",version) else None
            previous=self.profile(version) if version else {}
            try:
                target=identity(self.connection.target())
            except ValueError:
                target=None
            return dict(installation=view,phases=PHASES,selectedVersion=version,connectedInstance=target,
                        savedScript=previous.get("script",""),
                        verification=previous.get("instances",{}).get(target),telemetry=self.telemetry)

    def refresh_telemetry(self):
        try:
            url=self.connection.snapshot().get("url")
            if not url:
                self.telemetry={}
                return
            api=self.ctx["json_api"]
            queue=api(url,"/queue")
            stats=api(url,"/system_stats")
            def ids(rows):
                return [row[1] for row in rows if isinstance(row,list) and len(row)>=2]
            gpus=[{key:d.get(key) for key in ("name","vram_total","vram_free","utilization")} for d in stats.get("devices",[])]
            self.telemetry=dict(running=ids(queue["queue_running"]),queued=ids(queue["queue_pending"]),gpus=gpus)
        except (OSError,ValueError,KeyError):
            self.telemetry={}
        finally:
            self.telemetry_lock.release()
