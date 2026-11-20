"""Submit one shot to local ComfyUI and archive its inputs and final MP4."""
import sys
# Studio deploys a frozen episode manifest. The original interactive CLI remains available.
if len(sys.argv)==3 and sys.argv[1]=="--production":
    from pathlib import Path
    from vast_worker import execute
    execute(Path(sys.argv[2]))
    raise SystemExit(0)

import argparse
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

base = Path(__file__).resolve().parent
comfy = Path(os.getenv("COMFY_ROOT", "/workspace/ComfyUI"))
url = os.getenv("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
parser = argparse.ArgumentParser()
parser.add_argument("--retry", type=uuid.UUID)
args = parser.parse_args()

def request(path, data=None):
    call = Request(url + path, data=json.dumps(data).encode() if data else None,
                   headers={"Content-Type": "application/json"})
    try:
        with urlopen(call, timeout=60) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(exc.read().decode(errors="replace")) from exc

job = base / "jobs" / str(args.retry or uuid.uuid4())
if not args.retry:
    source = Path(input("Reference image path: ").strip()).expanduser()
    prompt = input("Shot prompt (one line): ").strip()
    if not source.is_file() or not prompt:
        raise SystemExit("Supply an existing image and a prompt.")
    job.mkdir(parents=True)
    asset = job / ("reference" + source.suffix.lower())
    shutil.copyfile(source, asset)
    graph = json.loads((base / "workflow.json").read_text())
    graph["84"]["inputs"]["value"] = prompt
    graph["169"]["inputs"]["image"] = job.name + asset.suffix
    (job / "graph.json").write_text(json.dumps(graph, ensure_ascii=False))
graph = json.loads((job / "graph.json").read_text())
asset = next(job.glob("reference.*"))
shutil.copyfile(asset, comfy / "input" / graph["169"]["inputs"]["image"])
attempt = job / uuid.uuid4().hex
attempt.mkdir()
graph["141"]["inputs"]["filename_prefix"] = f"AXDAR/{job.name}/{attempt.name}"
print("Shot ID:", job.name, flush=True)
try:
    nodes = request("/object_info")
    missing = sorted({v["class_type"] for v in graph.values()} - set(nodes))
    if missing:
        raise RuntimeError("Missing node types: " + ", ".join(missing))
    ticket = request("/prompt", {"prompt": graph})["prompt_id"]
    (attempt / "prompt_id.txt").write_text(ticket)
    print("Queued:", ticket, flush=True)
    while True:
        result = request("/history/" + ticket).get(ticket)
        if result:
            break
        time.sleep(4)
    (attempt / "history.json").write_text(json.dumps(result))
    if result.get("status", {}).get("status_str") == "error":
        raise RuntimeError(str(result["status"]["messages"][-1]))
    files = list((comfy / "output" / "AXDAR" / job.name).glob(attempt.name + "*.mp4"))
    if not files:
        raise RuntimeError("No MP4 found. See history.json.")
    for file in files:
        shutil.copy2(file, attempt / file.name)
    print("Video saved:", attempt)
except Exception as exc:
    (attempt / "error.txt").write_text(str(exc))
    raise SystemExit(f"{exc}\nRetry after fixing: python worker.py --retry {job.name}")
