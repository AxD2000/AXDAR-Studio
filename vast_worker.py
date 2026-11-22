"""Runs on the user's rented ComfyUI Linux instance. All state lives in /workspace."""
import json
import os
import runpy
import shutil
import subprocess
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import production as prod
import provision
from drive_archive import archive_run
from production_services import countdown, deliver, event
import take_quality


def comfy_root():
    candidates=[Path(os.environ["COMFY_ROOT"])] if os.environ.get("COMFY_ROOT") else []
    candidates += [Path("/workspace/ComfyUI"), Path("/opt/ComfyUI"), Path.home()/"ComfyUI"]
    return next((p for p in candidates if (p/"main.py").is_file()),None)


def runtime_requirements(ctx,spec,wf):
    from workflow_profiles import runtime_graph
    required={}
    for eid,shot,rules in spec["jobs"]:
        prompt=shot.get("frozenPrompt") or ctx["prompt"](shot,rules)
        runtime=runtime_graph(wf,shot,prompt,ctx["generation_images"](shot),ctx["generation_voices"](shot),str,"AXDAR/check")
        for node in runtime.values():
            key=json.dumps(node,sort_keys=True)
            if key not in required:
                required[key]=node
    return {"graph":required}


def ready(ctx, wf, repair=True):
    configured=os.environ.get("COMFY_PORT","")
    ports=(int(configured),18188,8188) if configured.isdecimal() and 1024<=int(configured)<=65535 else (18188,8188)
    for port in dict.fromkeys(ports):
        url=f"http://127.0.0.1:{port}"
        try:
            if "system" in ctx["json_api"](url,"/system_stats"):
                break
        except (OSError,ValueError):
            continue
    else:
        raise RuntimeError("ComfyUI API is unavailable on the configured loopback port(s): "+", ".join(map(str,ports)))
    nodes=ctx["json_api"](url,"/object_info")
    missing=[]
    models=set()
    for node in wf["graph"].values():
        kind=node["class_type"]
        if kind not in nodes:
            missing.append(kind)
            # A freshly installed custom node cannot enumerate its model choices yet.
            models.update(value for value in node["inputs"].values()
                          if isinstance(value, str) and value.lower().endswith(provision.MODEL_SUFFIXES))
            continue
        schema=nodes[kind].get("input",{})
        for key,value in node["inputs"].items():
            if not isinstance(value,str) or not value.lower().endswith(provision.MODEL_SUFFIXES):
                continue
            field=schema.get("required",{}).get(key) or schema.get("optional",{}).get(key)
            if field:
                choices=field[0] if isinstance(field[0],list) else (field[1].get("options") if len(field)>1 else None)
                if isinstance(choices,list) and provision.model_choice(value, choices) is None:
                    models.add(value)
    sage_required = any(node["class_type"] == "MiniMaxH3MemoryEfficientSageAttentionPatch"
                        for node in wf["graph"].values())
    sage_missing = sage_required and not provision.sage_ready()
    if missing or models or sage_missing:
        if not repair:
            raise ValueError("Still unavailable after preparation: "+", ".join(
                sorted(set(missing)|models|({"SageAttention"} if sage_missing else set()))))
        queue=ctx["json_api"](url,"/queue")
        if queue["queue_running"] or queue["queue_pending"]:
            raise ValueError("ComfyUI has another job. Resume setup after its queue is empty.")
        root=comfy_root()
        if not root:
            raise ValueError("Select a ComfyUI template or configure COMFY_ROOT.")
        try:
            fixed=provision.install_nodes(root,set(missing))
        except (OSError,ValueError,subprocess.SubprocessError) as exc:
            raise ValueError("Node installation failed. Check Jupyter Terminal and install: " + ", ".join(sorted(set(missing)))) from exc
        unknown=set(missing)-fixed
        if unknown:raise ValueError("Unsupported missing nodes: "+", ".join(sorted(unknown)))
        try:
            provision.install_models(root,models)
        except (OSError,ValueError,subprocess.SubprocessError) as exc:
            raise ValueError("Model installation failed: " + ", ".join(sorted(models)) + ". Repair it in Jupyter Terminal, then Resume.") from exc
        if sage_missing:
            try:
                provision.install_sage(root)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                raise ValueError("SageAttention setup failed on this CUDA/PyTorch environment.") from exc
        subprocess.run(["supervisorctl","restart","comfyui"],check=True,timeout=90)
        for attempt in range(30):
            time.sleep(3)
            try:return ready(ctx,wf,False)
            except RuntimeError:
                if attempt==29:raise
    return url


def execute(directory, maintenance=False):
    import fcntl
    directory=directory.resolve()
    store=directory.parent.parent
    with (store/"production.lock").open("a") as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another production worker is active on this instance")
        spec=prod.read(directory/"spec.json")
        ctx=runpy.run_path(str(Path(__file__).with_name("studio.pyw")))
        ctx=ctx["prepare_shot"].__globals__
        ctx["DATA"]=directory/"episodes"
        ctx["files"]={name:store/"blobs"/sha for name,sha in spec["assets"].items()}
        wf=spec["workflow"]
        if "mapping" not in wf:
            wf=ctx["prepare_workflow"](wf["name"],wf["graph"])
        status=directory/"status.json"
        private=prod.read(directory/"services-private.json",{})
        endpoint=private.get("webhook")
        if maintenance:
            existing=prod.read(status,{})
            spec["url"]=existing.get("url")
            if not existing or not spec["url"]:
                raise ValueError("Run dependency checks before production maintenance.")
            finalize(directory,spec,existing,ctx,private)
            return
        backups=None
        try:
            progress=prod.read(status,{})
            progress.update(id=spec["id"],dispatch=spec.get("dispatch"),status="checking ComfyUI and requirements",updated=time.time())
            prod.atomic(status,progress)
            requirements=runtime_requirements(ctx,spec,wf) if spec.get("package2") else wf
            try:
                url=ready(ctx,requirements)
            except RuntimeError:
                subprocess.run(["supervisorctl","start","comfyui"],capture_output=True,timeout=35)
                for i in range(12):
                    time.sleep(3)
                    try:
                        url=ready(ctx,requirements)
                        break
                    except RuntimeError:
                        if i==11: raise
            spec["url"]=url
            # A detached worker reuses the local ComfyUI input directory when available.
            root=comfy_root()
            inputs=root/"input" if root else None
            if inputs and inputs.is_dir():
                def stage(_url,asset,name):
                    target=inputs/("axdar_"+prod.digest(asset)+asset.suffix.lower())
                    if not target.exists():
                        tmp=target.with_suffix(".part")
                        shutil.copyfile(asset,tmp)
                        tmp.replace(target)
                    return target.name
                ctx["upload"]=stage
            if spec.get("package2"):
                ctx["technical_qc"]=lambda path,job:take_quality.review(path,job,private.get("visual"))
                if private.get("drive"):
                    backups=ThreadPoolExecutor(max_workers=1)
                    ctx["record_take"]=lambda folder,job:backups.submit(archive_run,directory,spec,private["drive"],folder)
            control=directory/"control.json"
            for attempt in range(4 if spec.get("package2") else 1):
                command=prod.read(control,{})
                prod.drive(ctx,spec,spec["jobs"],wf,status,control,command.get("retry"))
                state=prod.read(status)
                uncertain=any(value=="uncertain" for value in state.get("shots",{}).values())
                if state["status"]!="waiting" and not uncertain:
                    break
                if attempt<3 and spec.get("package2") and time.time()+30<spec["end"]:
                    time.sleep(10*(attempt+1))
                else:
                    break
            if backups:
                backups.shutdown()
                backups=None
            if spec.get("package2"):
                finalize(directory,spec,state,ctx,private)
        except Exception as exc:
            s=prod.read(status,{})
            s.update(id=spec["id"],dispatch=spec.get("dispatch"), status="needs attention", error=str(exc)[:1200],updated=time.time(),current=None)
            s.pop("maintenance",None)
            prod.atomic(status,s)
            if spec.get("package2"):
                event(directory,spec,"worker failure")
                if isinstance(exc,ValueError):
                    event(directory,spec,"readiness or production error","Check the readiness diagnostics in Production.")
                deliver(directory,endpoint)
        finally:
            if backups:
                backups.shutdown(wait=True)


def finalize(run,spec,state,ctx,private):
    endpoint=private.get("webhook")
    status=state.get("status")
    if status=="finished":
        event(run,spec,"production completed")
    elif status=="limit reached":
        event(run,spec,"budget or time threshold")
    elif status=="needs attention":
        if any(x=="uncertain" for x in state.get("shots",{}).values()):
            event(run,spec,"uncertain submission","Review the original ComfyUI queue before retrying.")
        else:
            event(run,spec,"retry exhaustion")
    state["maintenance"]="working"
    state["backup"]={"state":"checking"}
    prod.atomic(run/"status.json",state)
    try:
        backup=archive_run(run,spec,private.get("drive"))
    except Exception as exc:
        backup={"state":"failed","error":str(exc)[:180] if type(exc).__name__=="DriveError" else
                "Drive backup could not verify. Check authorization and retry maintenance."}
        event(run,spec,"cloud backup failed")
    state["backup"]=backup
    state.pop("maintenance",None)
    prod.atomic(run/"status.json",state)
    deliver(run,endpoint)
    if spec.get("lifecycle")=="rehearse":
        plan=countdown(run,spec,state,ctx,endpoint)
        if plan["status"]=="countdown":
            log=run/"worker.log"
            with log.open("ab") as stream:
                subprocess.Popen([sys.executable,str(Path(__file__).with_name("production_services.py")),str(run)],
                                 stdin=subprocess.DEVNULL,stdout=stream,stderr=stream,start_new_session=True)


if __name__=="__main__":
    execute(Path(sys.argv[1]),len(sys.argv)>2 and sys.argv[2]=="--maintenance")

