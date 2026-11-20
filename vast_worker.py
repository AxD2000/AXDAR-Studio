"""Runs on the user's rented ComfyUI Linux instance. All state lives in /workspace."""
import json
import os
import runpy
import shutil
import subprocess
import sys
import time
from pathlib import Path

import production as prod
import provision


def ready(ctx, wf, repair=True):
    for port in (18188,8188):
        url=f"http://127.0.0.1:{port}"
        try:
            if "system" in ctx["json_api"](url,"/system_stats"):
                break
        except (OSError,ValueError):
            continue
    else:
        raise RuntimeError("ComfyUI API is unavailable on 18188 and 8188.")
    nodes=ctx["json_api"](url,"/object_info")
    missing=[]
    models=set()
    for node in wf["graph"].values():
        kind=node["class_type"]
        if kind not in nodes:
            missing.append(kind)
            # A freshly installed custom node cannot enumerate its model choices yet.
            models.update(value for value in node["inputs"].values()
                          if isinstance(value, str) and value.lower().endswith(".safetensors"))
            continue
        schema=nodes[kind]["input"]
        for key,value in node["inputs"].items():
            if not isinstance(value,str) or not value.lower().endswith(".safetensors"):
                continue
            field=schema.get("required",{}).get(key) or schema.get("optional",{}).get(key)
            if field:
                choices=field[0] if isinstance(field[0],list) else (field[1].get("options") if len(field)>1 else None)
                if isinstance(choices,list) and value.replace("\\","/") not in [str(c).replace("\\","/") for c in choices]:
                    models.add(value)
    if missing or models:
        if not repair:
            raise ValueError("Still unavailable after preparation: "+", ".join(sorted(set(missing)|models)))
        queue=ctx["json_api"](url,"/queue")
        if queue["queue_running"] or queue["queue_pending"]:
            raise ValueError("ComfyUI has another job. Resume setup after its queue is empty.")
        root=Path(os.getenv("COMFY_ROOT","/workspace/ComfyUI"))
        if not (root/"main.py").is_file():
            raise ValueError("Select a ComfyUI template or configure COMFY_ROOT.")
        fixed=provision.install_nodes(root,set(missing))
        unknown=set(missing)-fixed
        if unknown:raise ValueError("Unsupported missing nodes: "+", ".join(sorted(unknown)))
        provision.install_models(root,models)
        subprocess.run(["supervisorctl","restart","comfyui"],check=True,timeout=90)
        for attempt in range(30):
            time.sleep(3)
            try:return ready(ctx,wf,False)
            except RuntimeError:
                if attempt==29:raise
    return url


def execute(directory):
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
        wf=ctx["prepare_workflow"](spec["workflow"]["name"],spec["workflow"]["graph"])
        status=directory/"status.json"
        try:
            progress=prod.read(status,{})
            progress.update(id=spec["id"],dispatch=spec.get("dispatch"),status="checking ComfyUI and requirements",updated=time.time())
            prod.atomic(status,progress)
            try:
                url=ready(ctx,wf)
            except RuntimeError:
                subprocess.run(["supervisorctl","start","comfyui"],capture_output=True,timeout=35)
                for i in range(12):
                    time.sleep(3)
                    try:
                        url=ready(ctx,wf)
                        break
                    except RuntimeError:
                        if i==11: raise
            spec["url"]=url
            # A detached worker reuses the local ComfyUI input directory when available.
            inputs=Path(os.getenv("COMFY_ROOT","/workspace/ComfyUI"))/"input"
            if inputs.is_dir():
                def stage(_url,asset,name):
                    target=inputs/("axdar_"+prod.digest(asset)+asset.suffix.lower())
                    if not target.exists():
                        tmp=target.with_suffix(".part")
                        shutil.copyfile(asset,tmp)
                        tmp.replace(target)
                    return target.name
                ctx["upload"]=stage
            prod.drive(ctx,spec,spec["jobs"],wf,status,directory/"control.json",prod.read(directory/"control.json",{}).get("retry"))
        except Exception as exc:
            s=prod.read(status,{})
            s.update(id=spec["id"],dispatch=spec.get("dispatch"), status="needs attention", error=str(exc)[:1200],updated=time.time(),current=None)
            prod.atomic(status,s)


if __name__=="__main__":
    execute(Path(sys.argv[1]))

