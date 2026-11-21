"""Detached production entry point and interactive single-shot ComfyUI worker."""
import argparse
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def request(url, path, data=None):
    call = Request(url + path, data=json.dumps(data).encode() if data is not None else None,
                   headers={"Content-Type":"application/json"})
    try:
        with urlopen(call, timeout=60) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(exc.read(2000).decode(errors="replace")) from exc


def run_interactive(retry):
    import workflow_profiles as profiles
    base = Path(__file__).resolve().parent
    comfy = Path(os.getenv("COMFY_ROOT", "/workspace/ComfyUI"))
    url = os.getenv("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
    job = base / "jobs" / str(retry or uuid.uuid4())
    attempt = job / uuid.uuid4().hex
    if not retry:
        store = profiles.WorkflowStore(base / "workflow-profiles")
        wf = store.active()
        if wf is None:
            source = next((p for p in (base/"selected-workflow.json",base/"workflow.json") if p.exists()), None)
            if not source:
                raise ValueError("Import and select a compatible workflow in Studio first.")
            wf = profiles.inspect_workflow(source.name, source.read_bytes())
        if wf["issues"]:
            raise ValueError("Review the workflow mapping in Studio: " + " ".join(wf["issues"]))
        source = input("Reference image path (blank for none): ").strip()
        prompt = input("Shot prompt (one line): ").strip()
        seconds = float(input("Shot duration in seconds: ").strip())
        names = []
        if source:
            original = Path(source).expanduser()
            if not original.is_file():
                raise ValueError("Image file not found.")
            names.append("reference" + original.suffix.lower())
        # Validate the selected contract before creating a job or copying assets.
        graph = profiles.runtime_graph(wf, {"seconds":seconds}, prompt, names, [], lambda n:"axdar_"+job.name+Path(n).suffix,
                                       "AXDAR/" + job.name)
        job.mkdir(parents=True)
        if names:
            shutil.copy2(original, job/names[0])
        (job/"graph.json").write_text(json.dumps(graph,ensure_ascii=False),encoding="utf-8")
        (job/"output.json").write_text(json.dumps(wf["output"]),encoding="utf-8")
    else:
        graph = json.loads((job/"graph.json").read_bytes())
    if (job/"output.json").exists():
        output = json.loads((job/"output.json").read_bytes())
    else:
        # Older CLI archives retain their original graph, including staged filenames.
        wf = profiles.inspect_workflow("archived graph",graph)
        output = wf.get("output")
        if not output:
            raise ValueError("Could not identify the archived workflow's video output.")
    reference = next(job.glob("reference.*"),None)
    if reference:
        if (job/"output.json").exists():
            target = "axdar_" + job.name + reference.suffix
        else:
            target = job.name + reference.suffix
        shutil.copy2(reference, comfy/"input"/target)
    attempt.mkdir()
    if "filename_prefix" in graph[output]["inputs"]:
        graph[output]["inputs"]["filename_prefix"] = f"AXDAR/{job.name}/{attempt.name}"
    (attempt/"graph.json").write_text(json.dumps(graph,ensure_ascii=False),encoding="utf-8")
    print("Shot ID:",job.name,flush=True)
    try:
        nodes = request(url,"/object_info")
        missing = {node["class_type"] for node in graph.values()} - set(nodes)
        if missing:
            raise RuntimeError("Missing node types: " + ", ".join(sorted(missing)))
        ticket = request(url,"/prompt",{"prompt":graph})["prompt_id"]
        (attempt/"prompt_id.txt").write_text(ticket)
        print("Queued:",ticket,flush=True)
        while not (result:=request(url,"/history/"+ticket).get(ticket)):
            time.sleep(4)
        (attempt/"history.json").write_text(json.dumps(result),encoding="utf-8")
        if result.get("status",{}).get("status_str") == "error":
            raise RuntimeError(str(result["status"].get("messages",[])[-1:]))
        outputs = result.get("outputs",{}).get(output,{})
        videos = [v for group in outputs.values() if isinstance(group,list) for v in group
                  if isinstance(v,dict) and str(v.get("filename","")).lower().endswith(".mp4")]
        if not videos:
            raise ValueError("The selected output returned no MP4. See history.json.")
        for i,video in enumerate(videos,1):
            query = urlencode({k:video[k] for k in ("filename","subfolder","type") if k in video})
            with urlopen(url+"/view?"+query,timeout=60) as stream, (attempt/f"video-{i}.mp4").open("wb") as out:
                shutil.copyfileobj(stream,out)
        print("Video saved:",attempt)
    except Exception as exc:
        (attempt/"error.txt").write_text(str(exc),encoding="utf-8")
        raise RuntimeError(f"{exc}\nRetry after fixing: python worker.py --retry {job.name}") from exc


if __name__ == "__main__":
    if len(sys.argv)==3 and sys.argv[1]=="--production":
        from vast_worker import execute
        execute(Path(sys.argv[2]))
    else:
        parser=argparse.ArgumentParser()
        parser.add_argument("--retry", type=uuid.UUID)
        try:
            run_interactive(parser.parse_args().retry)
        except (ValueError, RuntimeError, OSError) as exc:
            raise SystemExit(str(exc))
