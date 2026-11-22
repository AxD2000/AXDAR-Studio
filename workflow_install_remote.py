"""Remote installer, supervised independently of the SSH connection."""
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import production
import provision

PHASES = ("Vast connection","Workflow analysis","Custom node scan","Custom node installation",
          "Model verification","ComfyUI restart","Final validation")
WEIGHTS = (".safetensors",".gguf",".ckpt",".pt",".pth",".bin",".onnx",".sft")


def redact(text, token=""):
    text=str(text or "")
    if token:
        text = text.replace(token,"[REDACTED]")
    return re.sub(r"\bhf_[A-Za-z0-9]{8,}\b", "[REDACTED]", text)


def scan(graph, info, exists=None):
    missing = sorted({n["class_type"] for n in graph.values()} - info.keys())
    models = {}
    verified = provision.model_sources()
    for filename,source in list(verified.items()):
        folder = source["folder"].split("/")
        verified["/".join([*folder[1:],filename])] = source
    for node in graph.values():
        kind = node["class_type"]
        schema = info.get(kind,{}).get("input",{})
        for field, value in node["inputs"].items():
            if not isinstance(value,str) or not value.lower().endswith(WEIGHTS):
                continue
            name = value.replace("\\","/")
            source = verified.get(name)
            spec = schema.get("required",{}).get(field) or schema.get("optional",{}).get(field)
            options = spec[0] if spec and isinstance(spec[0],list) else (
                spec[1].get("options") if spec and len(spec)>1 and isinstance(spec[1],dict) else None)
            listed = isinstance(options,list) and name in {str(item).replace("\\","/") for item in options}
            on_disk = exists(name) if exists else False
            if listed or on_disk and options is None and kind in info:
                status = "Installed"
            elif on_disk and isinstance(options,list):
                status = "Incompatible"
            else:
                status = "Missing" if source or isinstance(options,list) else "Requires manual action"
            previous = models.get(name)
            if previous and previous["status"] == "Incompatible":
                continue
            models[name] = dict(name=name,status=status,verified=bool(source),
                                source={k:source[k] for k in ("repo","revision","file","sha256","bytes")} if source else None)
    return {"nodes":missing,"models":[models[name] for name in sorted(models)]}


def root_path():
    configured=os.environ.get("COMFY_ROOT")
    candidates=[Path(configured)] if configured else []
    candidates.extend((Path("/workspace/ComfyUI"),Path("/opt/ComfyUI"),Path.home()/"ComfyUI"))
    for root in candidates:
        if (root/"main.py").is_file():
            return root.resolve()
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            if b"main.py" not in (proc/"cmdline").read_bytes():
                continue
            cwd = (proc/"cwd").resolve(strict=True)
            if (cwd/"main.py").is_file() and (cwd/"comfy").is_dir():
                return cwd
        except OSError:
            continue
    raise ValueError("Could not discover the active ComfyUI installation.")


def execute(folder, config):
    from urllib.error import URLError
    port = config["port"]
    base = f"http://127.0.0.1:{port}"
    def api(path):
        with urlopen(base+path,timeout=10) as response:
            return json.load(response)
    def idle():
        q = api("/queue")
        if q["queue_running"] or q["queue_pending"]:
            raise ValueError("ComfyUI has an active or queued generation. Finish it before installation.")
    state = {"stages":{}, "report":{}}
    def stage(name, status, message="", count=None, total=None):
        old = state["stages"].get(name,{})
        state["stages"][name] = dict(status=status,message=redact(message),
                    started=old.get("started",time.time()),updated=time.time(),count=count,total=total)
        production.atomic(folder/"stage.json",state)
    def report(result):
        state["report"] = result
        production.atomic(folder/"stage.json",state)

    stage(PHASES[0],"Running")
    root = root_path()
    info=api("/object_info")
    stage(PHASES[0],"Completed",str(root))
    stage(PHASES[1],"Completed",str(len(config["graph"]))+" required nodes")
    models_root = root/"models"
    def exists(name):
        path=Path(name)
        if path.is_absolute() or ".." in path.parts:
            return False
        return any(p.is_file() for p in models_root.glob("*/"+str(path))) if not any(c in name for c in "*?[]") else False
    graph=config["graph"]
    stage(PHASES[2],"Running")
    result=scan(graph,info,exists)
    report(result)
    stage(PHASES[2],"Completed",f"{len(result['nodes'])} missing")
    if config["mode"]=="scan":
        stage(PHASES[4],"Completed","Checked remote model choices and files")
    else:
        idle()
        script=folder/"install.sh"
        if script.is_file():
            stage(PHASES[3],"Running","Checking Bash syntax before execution")
            check=subprocess.run(["bash","-n",str(script)],cwd=root)
            state["bashExitCode"]=check.returncode
            if check.returncode:
                stage(PHASES[3],"Failed","Bash syntax check failed. No script commands ran.")
                return 1
            stage(PHASES[3],"Running","Running the approved Bash script")
            execution=subprocess.run(["bash",str(script)],cwd=root)
            state["bashExitCode"]=execution.returncode
            if execution.returncode:
                stage(PHASES[3],"Failed","The Bash script returned a nonzero exit code.")
                return 1
            info=api("/object_info")
            result=scan(graph,info,exists)
            report(result)
        missing=set(result["nodes"])
        known=set().union(*(kinds for kinds,*_ in provision.NODE_SOURCES))
        planned=sorted(missing & known)
        stage(PHASES[3],"Running","Installing reviewed custom nodes",0,len(planned))
        installed=provision.install_nodes(root,set(planned)) if planned else set()
        stage(PHASES[3],"Completed","Unknown classes require Bash or manual installation",len(installed),len(planned))
        absent=[m for m in result["models"] if m["status"]=="Missing" and m["verified"]]
        for index, model in enumerate(absent):
            stage(PHASES[4],"Running",model["name"],index,len(absent))
            model["status"]="Downloading"
            report(result)
            try:
                provision.install_models(root,[model["name"]])
            except (OSError,ValueError) as exc:
                model["status"]="Failed"
                model["error"]=redact(str(exc),os.environ.get("HF_TOKEN",""))
                report(result)
                raise
        stage(PHASES[4],"Completed","Verified model downloads",len(absent),len(absent))
        if installed or config.get("restart"):
            # ComfyUI reads many model lists dynamically. Restart for newly installed nodes
            # or when the approved Bash script explicitly requests a restart.
            info=api("/object_info")
            still=scan(graph,info,exists)
            if config.get("restart") or installed & set(still["nodes"]):
                idle()
                stage(PHASES[5],"Running","Restarting the ComfyUI supervisor service")
                subprocess.run(["supervisorctl","restart","comfyui"],check=True,timeout=90)
                until=time.monotonic()+120
                while True:
                    try:
                        info=api("/object_info")
                        break
                    except (URLError, OSError, ValueError):
                        if time.monotonic()>=until:
                            raise
                        time.sleep(2)
                stage(PHASES[5],"Completed")
        if PHASES[5] not in state["stages"]:
            stage(PHASES[5],"Skipped","ComfyUI restart was unnecessary")
    stage(PHASES[6],"Running")
    result=scan(graph,api("/object_info"),exists)
    report(result)
    available=not result["nodes"] and all(m["status"]=="Installed" for m in result["models"])
    stage(PHASES[6],"Completed" if available else "Failed",
          "Required classes registered and model choices verified" if available else "Review unresolved dependencies")
    return 0 if available else 2


def run(folder):
    # An exclusive remote claim prevents a duplicate launch after uncertain SSH acknowledgement.
    try:
        with (folder/"started").open("x") as lock:
            lock.write(str(os.getpid()))
    except FileExistsError:
        return
    if (folder/"cancel").exists():
        production.atomic(folder/"result.json",dict(status="cancelled",exitCode=None,finished=time.time()))
        return
    config=production.read(folder/"spec.json")
    deadline=time.monotonic()+config["timeout"]
    env=dict(os.environ, PYTHONUNBUFFERED="1")
    token=env.get("HF_TOKEN","")
    status="failed"
    child=subprocess.Popen([sys.executable,__file__,str(folder),"execute"],stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT,start_new_session=True,env=env)
    with selectors.DefaultSelector() as monitor, (folder/"terminal.log").open("a",encoding="utf-8",buffering=1) as log:
        monitor.register(child.stdout,selectors.EVENT_READ)
        pending=b""
        try:
            while monitor.get_map():
                if (folder/"cancel").exists() or time.monotonic()>=deadline:
                    status="cancelled" if (folder/"cancel").exists() else "timed out"
                    try:
                        os.killpg(child.pid,signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        child.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        pass
                    try:
                        os.killpg(child.pid,signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    break
                for key,_ in monitor.select(0.4):
                    data=os.read(key.fd,65536)
                    if not data:
                        monitor.unregister(key.fileobj)
                        continue
                    pending+=data
                    last=pending.rfind(b"\n")
                    if last!=-1:
                        log.write(redact(pending[:last+1].decode("utf-8",errors="replace"),token))
                        pending=pending[last+1:]
                    if len(pending)>1024*1024:
                        pending=b""
                        log.write("[Overlong terminal line omitted]\n")
            if pending:
                log.write(redact(pending.decode("utf-8",errors="replace"),token))
            code=child.wait(timeout=8)
            if status not in ("cancelled","timed out"):
                status="ready" if code==0 else "needs attention" if code==2 else "failed"
        except Exception:
            code=-1
            if child.poll() is None:
                os.killpg(child.pid,signal.SIGKILL)
                child.wait()
    production.atomic(folder/"result.json",dict(status=status,exitCode=code,finished=time.time()))


def snapshot(folder):
    state=production.read(folder/"stage.json",{})
    state.update(production.read(folder/"result.json",{}) or {})
    claim=folder/"started"
    state["started"]=claim.is_file()
    if state["started"] and "status" not in state and time.time()-claim.stat().st_mtime>10:
        try:
            pid=int(claim.read_text("ascii"))
            command=(Path("/proc")/str(pid)/"cmdline").read_bytes()
        except (OSError,ValueError):
            command=b""
        if os.fsencode(str(folder)) not in command:
            state.update(status="failed",finished=time.time(),exitCode=None)
    if (folder/"terminal.log").is_file():
        with (folder/"terminal.log").open("rb") as log:
            log.seek(max(0,log.seek(0,2)-60000))
            state["log"]=log.read().decode("utf-8",errors="replace")
    return state


if __name__=="__main__":
    work=Path(sys.argv[1])
    if len(sys.argv)>2:
        if sys.argv[2]=="snapshot":
            print(json.dumps(snapshot(work)))
        else:
            try:
                sys.exit(execute(work,production.read(work/"spec.json")))
            except Exception as exc:
                print(redact(str(exc),os.environ.get("HF_TOKEN","")),file=sys.stderr)
                sys.exit(1)
    else:
        run(work)
