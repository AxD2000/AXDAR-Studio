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
from vast_connection import SSH_CHECK_SECONDS, hf_token, identity
from workflow_install_remote import PHASES, redact
from workflow_profiles import _parse, _upstream, public_profile


INSTALL_ROOT = vast_deploy.STORE + "/installations"
TERMINAL = {"ready","needs attention","failed","cancelled","timed out"}
FILES = ("workflow_install_remote.py","production.py","provision.py","model-lock.json")
HF_VALUE = r"hf_[A-Za-z0-9]{8,256}"
HF_ASSIGNMENT = re.compile(
    rf"^(?P<indent>[ \t]*)(?P<export>export[ \t]+)?"
    rf"(?P<name>HF_TOKEN|HUGGING_FACE_HUB_TOKEN|HUGGINGFACEHUB_API_TOKEN|HUGGINGFACE_TOKEN|HF_API_TOKEN|HF_ACCESS_TOKEN)="
    rf"(?P<quote>['\"]?)(?P<token>{HF_VALUE})(?P=quote)(?P<tail>[ \t]*(?:#[^\r\n]*)?)(?=\r?$)",
    re.M,
)


def prepare_install_script(script):
    if not isinstance(script,str) or len(script)>256000:
        raise ValueError("The optional Bash script must be under 256 KB.")
    tokens=set()
    heredoc=script.find("<<")
    def replace(match):
        if heredoc!=-1 and match.start()>heredoc:
            return match[0]
        tokens.add(match["token"])
        export=match["export"] or ""
        return f'{match["indent"]}{export}{match["name"]}="$HF_TOKEN"{match["tail"]}'
    cleaned=HF_ASSIGNMENT.sub(replace,script)
    if redact(cleaned)!=cleaned or len(tokens)>1:
        raise ValueError("Save your Hugging Face token in Workflow Setup and use $HF_TOKEN wherever the script needs it.")
    return cleaned,next(iter(tokens),None)


def saved_hf_token():
    try:
        return bool(hf_token())
    except Exception:
        return None


class WorkflowManager:
    def __init__(self, ctx):
        self.ctx=ctx
        self.connection=ctx["controller"].connection
        directory=ctx["DATA"]/"production"
        self.path=directory/"workflow-installations.json"
        self.profiles=ctx["DATA"]/"production"/"workflow-installation-profiles"
        self.lock=threading.RLock()
        self.watchers={}
        legacy=production.read(directory/"workflow-installation.json",{})
        self.records=production.read(self.path,{str(legacy["instance"]):legacy} if legacy.get("instance") else {})
        self.telemetry={}
        self.telemetry_lock=threading.Lock()

    def selected_instance(self):
        try:
            return identity(self.connection.target())
        except ValueError:
            settings=self.connection.snapshot()
            if settings.get("instance"):
                return str(settings["instance"])
            if settings.get("ssh"):
                return identity(settings["ssh"])
            return None

    def save(self, record, **changes):
        with self.lock:
            if self.records.get(record["instance"]) is not record:
                return
            record.update(changes)
            production.atomic(self.path,self.records)

    def busy(self, instance=None):
        with self.lock:
            record=self.records.get(instance or self.selected_instance())
            return bool(record and record.get("status") not in TERMINAL)

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
        if mode in ("token","save-token"):
            supplied=request.get("token","")
            if not isinstance(supplied,str):
                raise ValueError("Enter a Hugging Face access token.")
            supplied=supplied.strip()
            token=supplied or (hf_token() if mode=="token" else "")
            if not token or not re.fullmatch(HF_VALUE,token):
                raise ValueError("Enter a Hugging Face access token.")
            if mode=="save-token":
                hf_token(token)
                return {"saved":True}
            try:
                with build_opener(ModelRedirect()).open(Request("https://huggingface.co/api/whoami-v2",
                                     headers={"Authorization":"Bearer "+token}),timeout=12) as response:
                    if response.status!=200:
                        raise ValueError("Hugging Face did not accept the token.")
            except HTTPError as exc:
                raise ValueError(f"Hugging Face token check returned HTTP {exc.code}.") from None
            except OSError:
                raise ValueError("Could not reach Hugging Face to verify the token.") from None
            if supplied:
                hf_token(token)
            return {"valid":True,"saved":True}
        if mode=="install-preview":
            script,token=prepare_install_script(request.get("script",""))
            saved=saved_hf_token()
            return {"script":script,"hfTokenSaved":saved,"willSaveHfToken":bool(token and not saved)}
        if mode=="prepare":
            return self.prepare(request.get("version"))
        if mode=="status":
            return self.status(request.get("version"),request.get("monitor") is True)
        if mode=="cancel":
            with self.lock:
                instance=self.selected_instance()
                if request.get("instance") is not None and str(request["instance"])!=instance:
                    raise ValueError("The selected instance changed. Open its installation monitor to cancel.")
                if not instance or not self.busy(instance):
                    raise ValueError("No active installation to cancel.")
                self.save(self.records[instance],cancel=True,message="Cancellation requested")
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
        if mode=="install" and request.get("confirmed") is not True:
            raise ValueError("Preview and confirm execution on the selected Vast instance.")
        script,embedded_token=prepare_install_script(request.get("script","")) if mode=="install" else ("",None)
        timeout=int(request.get("timeout",30))
        if not 1<=timeout<=180:
            raise ValueError("Choose an installation timeout of 1–180 minutes.")
        with self.ctx["LOCK"], self.lock:
            target=self.connection.target()
            instance=identity(target)
            if self.ctx["running"] or self.ctx["production_busy"]():
                raise ValueError("Finish or pause the active production first.")
            if self.busy(instance):
                raise ValueError("A workflow scan or installation is already active on this Vast instance.")
            if mode=="install" and request.get("instance") is not None and str(target.get("id"))!=str(request["instance"]):
                raise ValueError("The selected instance changed. Review the installation again.")
            plan={k:wf["graph"][k] for k in _upstream(wf["graph"],[wf["output"]])}
            if mode=="install":
                if embedded_token and not saved_hf_token():
                    hf_token(embedded_token)
                self.save_profile(version,script=script)
            record=dict(id=uuid.uuid4().hex,version=version,instance=instance,
                             rate=target.get("rate"),status="starting",started=time.time(),mode=mode,
                             timeout=timeout*60,stages={},report={},log="")
            self.records[instance]=record
            self.save(record)
            spec=dict(graph=plan,mode=mode,timeout=timeout*60,restart=request.get("restart") is True)
            threading.Thread(target=self.launch,args=(record,target,spec,script),daemon=True).start()
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

    def launch(self, record, target, spec, script):
        folder=INSTALL_ROOT+"/"+record["id"]
        self.save(record,message="Uploading the reviewed installation plan")
        try:
            spec["port"]=target["comfy_port"]
            vast_deploy.ssh(target,f"umask 077; mkdir -p {folder}")
            base=self.ctx["BASE"]
            vast_deploy.deploy_files(target,{folder+"/"+name:base/name for name in FILES})
            vast_deploy.put_json(target,folder+"/spec.json",spec)
            if script.strip():
                vast_deploy.ssh(target,f"umask 077; cat > {folder}/install.sh",
                                data=script.replace("\r\n","\n").encode())
            if record.get("cancel"):
                self.save(record,status="cancelled",finished=time.time(),message="Cancelled before launch")
                return
            token=(hf_token() or "") if spec["mode"]=="install" else ""
            self.save(record,dispatched=True,message="Waiting for remote installer")
            command=("IFS= read -r HF_TOKEN; export HF_TOKEN; "
                     "HUGGING_FACE_HUB_TOKEN=$HF_TOKEN; export HUGGING_FACE_HUB_TOKEN; "
                     f"umask 077; nohup python3 {folder}/workflow_install_remote.py {folder}"
                     " </dev/null >/dev/null 2>&1 &")
            vast_deploy.ssh(target,command,data=(token+"\n").encode(),timeout=SSH_CHECK_SECONDS)
        except Exception:
            if not record.get("dispatched"):
                self.save(record,status="failed",finished=time.time(),message="Remote setup could not be uploaded over SSH.")
                return
            self.save(record,status="reconnecting",message="Launch acknowledgement lost. Checking the original remote operation.")
        self.watch()

    def watch(self):
        with self.lock:
            instance=self.selected_instance()
            record=self.records.get(instance,{})
            previous=self.watchers.get(instance)
            if not instance or not self.busy(instance) or not record.get("dispatched") or previous and previous.is_alive():
                return
            self.watchers[instance]=threading.Thread(target=self.reconcile,args=(record,),daemon=True)
            self.watchers[instance].start()

    def reconcile(self, record):
        instance=record["instance"]
        folder=INSTALL_ROOT+"/"+record["id"]
        while self.selected_instance()==instance and self.records.get(instance) is record and self.busy(instance):
            try:
                target=self.connection.ssh_target()
                if identity(target)!=instance:
                    raise ValueError("Select the original instance to reconcile installation.")
                if record.get("cancel"):
                    vast_deploy.ssh(target,f"umask 077; touch {folder}/cancel",timeout=SSH_CHECK_SECONDS)
                data=vast_deploy.ssh(target,f"python3 {folder}/workflow_install_remote.py {folder} snapshot",timeout=SSH_CHECK_SECONDS)
                state=json.loads(data)
                if not state.get("started"):
                    self.save(record,status="reconnecting",message="Remote process is not yet confirmed. It will not be restarted automatically.")
                    if time.time()>record["started"]+record["timeout"]+60:
                        self.save(record,status="failed",finished=time.time(),message="Remote launch did not start.")
                else:
                    state.pop("started")
                    state.setdefault("status","running")
                    with self.lock:
                        message="Remote installer stopped before recording a result." if state["status"]=="failed" and state.get("exitCode") is None else ""
                        self.save(record,message=message,**state)
                        if state["status"] in ("ready","needs attention") and state.get("report"):
                            version=record["version"]
                            profiles=self.profile(version).get("instances",{})
                            profiles[instance]={k:record.get(k) for k in ("status","report","finished")}
                            self.save_profile(version,instances=profiles)
            except (ValueError,OSError,TimeoutError,subprocess.SubprocessError) as exc:
                message=("Reconnect the original Vast instance to check this installation."
                         if isinstance(exc,ValueError) else "SSH interrupted. Checking the same remote installation.")
                self.save(record,status="reconnecting",message=message)
                self.connection.retry_saved(instance)
            time.sleep(2)

    def status(self, version=None, monitor=False):
        self.watch()
        if monitor and self.telemetry_lock.acquire(blocking=False):
            threading.Thread(target=self.refresh_telemetry,daemon=True).start()
        with self.lock:
            target=self.selected_instance()
            record=self.records.get(target,{})
            if target and self.busy(target) and not record.get("dispatched") and time.time()-record["started"]>300:
                self.save(record,status="failed",finished=time.time(),message="AXDAR closed before it launched the remote operation.")
            view={key:record.get(key) for key in ("version","instance","status","rate","started","finished","mode","stages",
                                                       "report","log","message","exitCode","bashExitCode")}
            if record:
                view["elapsed"]=(record.get("finished") or time.time())-record["started"]
            version=version if isinstance(version,str) and re.fullmatch(r"[0-9a-f]{64}",version) else None
            previous=self.profile(version) if version else {}
            return dict(installation=view,phases=PHASES,selectedVersion=version,connectedInstance=target,
                        otherInstallations=sum(key!=target and record.get("status") not in TERMINAL
                                               for key,record in self.records.items()),
                        savedScript=previous.get("script",""),
                        hfTokenSaved=saved_hf_token(),
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
