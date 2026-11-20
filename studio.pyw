from pathlib import Path
import copy, json, mimetypes, os, re, secrets, shutil, threading, time, uuid, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode, urlsplit, unquote
from urllib.request import Request, urlopen
from urllib.error import HTTPError

BASE = Path(__file__).resolve().parent
ASSETS = Path(os.environ.get("AXDAR_ASSETS", str(BASE / "assets")))
DATA = BASE / "episodes"
DATA.mkdir(exist_ok=True)
DB = DATA / "project.json"
LOCK = threading.RLock()
TOKEN = secrets.token_hex(24)
PORT = 18753
running = False
session = {}
files = {p.name:p for p in ASSETS.rglob("*") if p.is_file()}
LIBRARY = BASE / "library"
LIBRARY.mkdir(exist_ok=True)
files.update({p.name:p for p in LIBRARY.iterdir() if p.is_file() and p.suffix != ".part"})
PHOTOS = {".jpg", ".jpeg", ".png", ".webp", ".avif"}
VOICES = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}

def import_asset(filename, blob):
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename).strip(" .")
    stem, ext = Path(filename).stem[:80].strip(" ."), Path(filename).suffix.lower()
    if not stem or ext not in PHOTOS | VOICES:
        raise ValueError("Choose a supported photo or voice file.")
    if re.match(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)", stem, re.I):
        stem = "_" + stem
    existing = {name.casefold() for name in files}
    number, name = 1, stem + ext
    while name.casefold() in existing or (LIBRARY/name).exists():
        number += 1
        name = f"{stem} ({number}){ext}"
    target = LIBRARY/name
    temp = LIBRARY/(uuid.uuid4().hex + ".part")
    try:
        temp.write_bytes(blob)
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)
    files[name] = target
    return name

def active_assets(db):
    refreshed = {p.name:p for p in ASSETS.rglob("*") if p.is_file() and p.suffix.lower() in PHOTOS | VOICES}
    refreshed.update({p.name:p for p in LIBRARY.iterdir() if p.is_file() and p.suffix.lower() in PHOTOS | VOICES})
    global files
    files = refreshed
    hidden = set(db.get("removedAssets", []))
    return [name for name in files if name not in hidden]

def clean_assets(db):
    hidden = set(db.get("removedAssets", []))
    db["rules"] = [rule for rule in db["rules"] if rule["scope"] not in hidden]
    for episode in db["episodes"]:
        for shot in episode["shots"]:
            if shot["image"] in hidden or hidden.intersection(shot["refs"]):
                if shot["image"] in hidden:
                    shot["image"] = ""
                shot["refs"] = [name for name in shot["refs"] if name not in hidden]
                shot.update(approved=False, revision=str(uuid.uuid4()))

WORKFLOW_STATE = BASE / "selected-workflow.json"

def prepare_workflow(name, graph):
    if not isinstance(graph, dict) or "nodes" in graph:
        raise ValueError("Export this workflow from ComfyUI in API format (Save / Export API).")
    graph = graph.get("prompt", graph)
    if isinstance(graph,dict):
        missing=[str(k) for k,n in graph.items() if isinstance(n,dict) and not n.get("class_type")]
        if missing: raise ValueError("API export is missing class_type for "+str(len(missing))+" nodes: "+", ".join(missing)+". Load the original in ComfyUI with all custom nodes available, then export in API format.")
    if not isinstance(graph, dict) or not graph or not all(
        str(key).isdigit() and isinstance(node, dict) and isinstance(node.get("inputs"), dict)
        and isinstance(node.get("class_type"), str) for key, node in graph.items()
    ):
        raise ValueError("Expected a ComfyUI API graph with numeric node IDs.")
    def single(ids, role):
        ids = list(ids)
        if len(ids) != 1:
            raise ValueError(f"Expected one {role}, found {len(ids)}.")
        return ids[0]

    def upstream(key):
        seen, todo = set(), [key]
        while todo:
            current = todo.pop()
            if current in seen:
                continue
            seen.add(current)
            for value in graph[current]["inputs"].values():
                if isinstance(value, list) and len(value)==2 and isinstance(value[0],str) and isinstance(value[1],int):
                    if value[0] not in graph:
                        raise ValueError(f"Node {current} links to missing node {value[0]}.")
                    todo.append(value[0])
        return seen

    candidates = [k for k,n in graph.items() if n["class_type"]=="VHS_VideoCombine"
                  and n["inputs"].get("save_output",True) and "mp4" in str(n["inputs"].get("format","")).lower()]
    final = [k for k in candidates if "final" in graph[k].get("_meta",{}).get("title","").lower()]
    output = single(final or candidates, "final MP4 output (VHS_VideoCombine)")
    branch = upstream(output)
    reference = single((k for k in branch if graph[k]["class_type"]=="MiniMaxH3ReferenceToVideo"), "MiniMax H3 reference node")
    inputs = graph[reference]["inputs"]
    def source(slot, types):
        link = inputs.get(slot)
        if not isinstance(link,list) or len(link)!=2 or graph.get(link[0],{}).get("class_type") not in types:
            raise ValueError(f"Unsupported connection for {slot} on node {reference}.")
        return link[0]
    prompt_id = source("prompt", {"PrimitiveStringMultiline","PrimitiveString"})
    # Determine the first existing reference image slot (sorted numerically)
    ref_slots = [s for s in inputs if s.startswith("ref_images.ref_image_") and re.fullmatch(r"ref_images\.ref_image_(\d+)", s)]
    if not ref_slots:
        raise ValueError("No reference image slots found on MiniMax H3 reference node")
    # Sort slots by numeric suffix
    ref_slots.sort(key=lambda s: int(s.split("_")[-1]))
    loader = source(ref_slots[0], {"LoadImage"})
    duration_branch = upstream(source("length", {node["class_type"] for node in graph.values()}))
    numbers = [k for k in duration_branch if graph[k]["class_type"] in ("PrimitiveFloat","PrimitiveInt")
               and isinstance(graph[k]["inputs"].get("value"),(float,int))]
    duration = single([k for k in numbers if "duration" in graph[k].get("_meta",{}).get("title","").lower()] or numbers, "shot duration control")
    return dict(name=name, graph=graph, reference=reference, loader=loader, prompt=prompt_id, duration=duration, output=output)

def read_workflow():
    if WORKFLOW_STATE.exists():
        data=json.loads(WORKFLOW_STATE.read_text("utf-8"))
        return prepare_workflow(data["name"],data["graph"])
    if not (BASE/"workflow.json").exists():
        raise ValueError("Upload a compatible ComfyUI API workflow in Studio to begin.")
    return prepare_workflow("workflow.json",json.loads((BASE/"workflow.json").read_text("utf-8")))

try:
    active_workflow = read_workflow()
    workflow_error = ""
except ValueError as exc:
    active_workflow = None
    workflow_error = str(exc)


def save(path, obj):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), "utf-8")
    temp.replace(path)

def project():
    return json.loads(DB.read_text("utf-8")) if DB.exists() else {"episodes":[], "rules":[
        {"scope":"all","text":"حافظ على شكل الشخصية وملابسها حسب الصورة الأساسية المختارة."},
        {"scope":"Cozy Sunlit Traditional Living Room.png","text":"حافظ على ترتيب أثاث الغرفة واتجاه الإضاءة."},
        {"scope":"dialog","text":"الكاميرا ثابتة أثناء الحوار."}]}

def generation_voices(shot):
    return list(dict.fromkeys(name for name in shot["refs"] if Path(name).suffix.lower() in VOICES))

def prompt(s, rules):
    selected = [s["image"], *s["refs"]]
    lines = [r["text"] for r in rules if r["scope"] == "all" or r["scope"] in selected or (r["scope"]=="dialog" and s["dialog"])]
    return "\n".join(x for x in ["r34l1sm", s["prompt"], *[f"<Audio {i}> is the voice reference for {Path(name).stem}." for i, name in enumerate(generation_voices(s), 1)], *lines, s["change"]] if x)

def remote(url, path, body=None, headers=None):
    try:
        return urlopen(Request(url + path, data=body, headers=headers or {}), timeout=45)
    except HTTPError as e:
        if e.code in (408,429,500,502,503,504): raise
        raise ValueError(f"ComfyUI {e.code}: {e.read(1800).decode(errors='replace')}") from e

def json_api(url, path, obj=None):
    with remote(url,path,json.dumps(obj).encode() if obj is not None else None, {"Content-Type":"application/json"}) as f:
        return json.load(f)

def upload(url, asset, name):
    boundary = uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
            + asset.read_bytes() + f'\r\n--{boundary}--\r\n'.encode())
    with remote(url, "/upload/image", body, {"Content-Type":"multipart/form-data; boundary="+boundary}) as f:
        info=json.load(f)
    return "/".join(filter(None,[info.get("subfolder"),info["name"]]))

def generation_images(shot):
    return list(dict.fromkeys(name for name in [shot["image"], *shot["refs"]]
                              if name and Path(name).suffix.lower() in PHOTOS))

def connect_images(graph_copy, names, upload_image, wf):
    if not 1 <= len(names) <= 9:
        raise ValueError("Choose between 1 and 9 character images for this shot.")
    next_id = max(map(int, graph_copy)) + 1
    inputs = graph_copy[wf["reference"]]["inputs"]
    for slot in list(inputs):
        if slot.startswith("ref_images.ref_image_"): del inputs[slot]
    for index, name in enumerate(names):
        node_id = wf["loader"] if index == 0 else str(next_id + index - 1)
        loader = copy.deepcopy(graph_copy[wf["loader"]])
        loader["inputs"]["image"] = upload_image(name)
        graph_copy[node_id] = loader
        inputs[f"ref_images.ref_image_{index}"] = [node_id, 0]

import production
import vast_deploy

def prepare_shot(url, folder, eid, s, wf, record):
    for name in set(filter(None,[s["image"],*s["refs"]])):
        shutil.copy2(files[name],folder/name)
    g=copy.deepcopy(wf["graph"])
    connect_images(g, generation_images(s), lambda name: upload(url,folder/name,uuid.uuid4().hex+Path(name).suffix), wf)
    inputs = g[wf["reference"]]["inputs"]
    for slot in list(inputs):
        if slot.startswith("ref_audios.ref_audio_"):
            del inputs[slot]
    for index, name in enumerate(generation_voices(s)):
        node_id = str(max(map(int, g)) + 1)
        uploaded = upload(url, folder/name, uuid.uuid4().hex + Path(name).suffix)
        g[node_id] = {"class_type": "LoadAudio", "inputs": {"audio": uploaded}}
        inputs[f"ref_audios.ref_audio_{index}"] = [node_id, 0]
    g[wf["prompt"]]["inputs"]["value"]=record["prompt"]
    g[wf["duration"]]["inputs"]["value"]=s["seconds"]
    g[wf["output"]]["inputs"]["filename_prefix"]="AXDAR/"+eid+"/"+s["id"]+"/"+folder.name
    return g

def download_output(url,folder,h,record):
    outputs=h.get("outputs",{}).get(record.get("output_node","141"),{})
    video=next((v for group in outputs.values() if isinstance(group,list) for v in group if isinstance(v,dict) and v.get("filename","").lower().endswith(".mp4")),None)
    if not video: raise ValueError("No MP4 returned from the final output node.")
    query=urlencode({k:video[k] for k in ("filename","subfolder","type") if k in video})
    with remote(url,"/view?"+query) as src, (folder/"video.part").open("wb") as out: shutil.copyfileobj(src,out)

def run(url, deadline, jobs, wf):
    global running
    try:
        for eid, shot, rules in jobs:
            if time.time() >= deadline-60: break
            try:
                result=production.render(globals(),url,deadline,eid,shot,rules,wf,2,lambda:None,True)
                if result=="waiting": break
            except Exception as exc:
                session["error"]=f"{shot['id']}: {exc}"
    finally:
        with LOCK: running=False

controller=vast_deploy.Controller(globals())

class Handler(BaseHTTPRequestHandler):
    def send(self, data, mime="application/json", status=200):
        if not isinstance(data,bytes): data=json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type",mime)
        self.send_header("Cache-Control","no-store")
        self.send_header("Content-Length",str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        from urllib.parse import unquote
        route=unquote(urlsplit(self.path).path)
        if self.headers.get("Host") != f"127.0.0.1:{PORT}": return self.send({},status=403)
        if route in ("/workspaces.js", "/workspaces.css", "/production-ui.js", "/plugin-manager.js", "/connection-ui.js"):
            mime = "text/javascript" if route.endswith(".js") else "text/css"
            return self.send((BASE/route[1:]).read_bytes(), mime + "; charset=utf-8")
        if route=="/":
            return self.send((BASE/"studio.html").read_text("utf-8").replace("__TOKEN__",TOKEN).encode(),"text/html; charset=utf-8")
        if route=="/api":
            with LOCK:
                db = project()
                return self.send(dict(project=db,assets=active_assets(db)))
        if route=="/status":
            result=[]
            for p in sorted(DATA.glob("*/*/*/job.json")):
                j=json.loads(p.read_text("utf-8"))
                result.append(dict(episode=p.parents[2].name,id=j["shot"],version=p.parent.name,status=j["status"],error=j.get("error",""),revision=j["revision"],video="/video/"+p.parent.relative_to(DATA).as_posix() if (p.parent/"video.mp4").exists() else ""))
            return self.send(dict(production=controller.current(),running=running,session=session,results=result,workflow=active_workflow["name"] if active_workflow else "Workflow blocked",workflowError=workflow_error,connection=controller.connection.snapshot()))
        p=files.get(route[7:]) if route.startswith("/asset/") else None
        if re.fullmatch(r"/video/[a-f0-9-]+/[a-f0-9-]+/\d+",route): p=DATA/route[7:]/"video.mp4"
        if p and p.is_file():
            self.send_response(200)
            self.send_header("Content-Type",mimetypes.guess_type(p.name)[0] or "application/octet-stream")
            self.send_header("Content-Length",str(p.stat().st_size))
            self.end_headers()
            with p.open("rb") as src: shutil.copyfileobj(src,self.wfile)
        else: self.send({},status=404)
    def do_POST(self):
        global running,session,active_workflow,workflow_error
        if self.headers.get("X-Studio")!=TOKEN: return self.send({},status=403)
        try:
            size=int(self.headers["Content-Length"])
            if self.path == "/plugins":
                if not 0 < size <= 65536:
                    return self.send({"ok": False, "error": {"code": "rejection", "message": "Invalid plugin request size."}}, status=400)
                from plugin_host import handle
                try:
                    request = json.loads(self.rfile.read(size))
                except (ValueError, UnicodeError):
                    return self.send({"ok": False, "error": {"code": "rejection", "message": "Invalid JSON."}}, status=400)
                return self.send(handle(request))
            if self.path == "/library/upload":
                if not 0 < size <= 64*1024*1024:
                    raise ValueError("Choose a nonempty file up to 64 MB.")
                blob = self.rfile.read(size)
                if len(blob) != size:
                    raise ValueError("Upload incomplete. Try again.")
                with LOCK:
                    name = import_asset(unquote(self.headers.get("X-Filename","")), blob)
                    return self.send({"name":name})
            if size>8000000: raise ValueError("المشروع أكبر من الحجم المسموح")
            obj=json.loads(self.rfile.read(size))
            if self.path=="/connection":
                if self.headers.get("Host") != f"127.0.0.1:{PORT}": return self.send({},status=403)
                action=obj.get("action")
                connection=controller.connection
                if action=="key":
                    from vast_connection import api_key
                    try: api_key(obj.get("key"))
                    except Exception: raise ValueError("Windows credential storage failed")
                    return self.send({"ok":True})
                if action=="hf":
                    from vast_connection import hf_token
                    hf_token(obj.get("token"))
                    return self.send({"ok":True})
                if action=="instances": return self.send(controller.list_instances())
                if action=="connect": return self.send(connection.connect(obj.get("instance"),obj.get("auto",False)))
                if action=="disconnect": return self.send(connection.disconnect())
                if action=="command": return self.send({"output":connection.command(obj.get("command"))})
                raise ValueError("Unknown connection action")
            if self.path=="/production":
                if running: raise ValueError("The manual session is active")
                if not active_workflow and obj.get("action") in ("start","retry","resume"): raise ValueError(workflow_error)
                return self.send(controller.action(obj))
            with LOCK:
                if self.path=="/workflow":
                    new = prepare_workflow(str(obj["name"]),obj["graph"])
                    save(WORKFLOW_STATE,dict(name=new["name"],graph=new["graph"]))
                    active_workflow=new
                    workflow_error=""
                    return self.send({"name":new["name"]})
                if self.path=="/save":
                    obj["removedAssets"] = project().get("removedAssets", [])
                    clean_assets(obj)
                    save(DB,obj)
                elif self.path=="/library/remove":
                    name = obj["name"]
                    db = project()
                    if name not in files:
                        raise ValueError("File not found in the library.")
                    if name not in db.setdefault("removedAssets", []):
                        db["removedAssets"].append(name)
                    clean_assets(db)
                    save(DB, db)
                    return self.send(dict(project=db, assets=active_assets(db)))
                elif self.path=="/start":
                    if not active_workflow: raise ValueError(workflow_error)
                    if controller.current().get("status") not in (None,"finished","stopped","needs attention","deployment failed"): raise ValueError("Finish remote production first")
                    if running: raise ValueError("الجلسة تعمل حالياً")
                    url=(controller.connection.snapshot().get("url") or obj.get("url", "")).strip().rstrip("/")
                    if urlsplit(url).scheme not in ("http","https") or not urlsplit(url).hostname: raise ValueError("أدخل رابط ComfyUI")
                    minutes=int(obj["minutes"])
                    if not 2<=minutes<=1440: raise ValueError("المدة من دقيقتين إلى ١٤٤٠ دقيقة")
                    db=project()
                    ep=next(e for e in db["episodes"] if e["id"]==obj["episode"])
                    jobs=[(ep["id"],s,db["rules"]) for s in ep["shots"] if s["approved"]]
                    if not jobs: raise ValueError("اعتمد لقطة واحدة على الأقل")
                    for eid,s,_ in jobs:
                        if len(generation_voices(s)) > 3: raise ValueError("Choose at most 3 voice references per shot.")
                        if len(generation_images(s)) > 9: raise ValueError("Shot "+s["id"]+" exceeds the 9-image limit.")
                        if not all(re.fullmatch(r"[a-f0-9-]{36}",x) for x in (eid,s["id"])) or s["image"] not in files or not s["prompt"].strip() or not 1<=s["seconds"]<=60: raise ValueError("راجع صورة وبرومبت ومدة اللقطة "+s["id"])
                    end=time.time()+60*minutes
                    session=dict(end=end,episode=ep["id"])
                    running=True
                    threading.Thread(target=run,args=(url,end,jobs,active_workflow),daemon=True).start()
                else: return self.send({},status=404)
            self.send({"ok":True})
        except Exception as e: self.send({"error":str(e)},status=400)
    def log_message(self,*args): pass

if __name__=="__main__":
    address=f"http://127.0.0.1:{PORT}"
    try: server=ThreadingHTTPServer(("127.0.0.1",PORT),Handler)
    except OSError:
        with urlopen(address+"/api",timeout=3) as response:
            if "project" not in json.load(response): raise
        webbrowser.open(address)
    else:
        webbrowser.open(address)
        if controller.connection.settings.get("auto"):
            controller.connection.connect(auto=True)
        server.serve_forever()
