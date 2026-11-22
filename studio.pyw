from pathlib import Path
import copy, json, mimetypes, os, re, secrets, shutil, socket, threading, time, uuid, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode, urlsplit, unquote, parse_qs
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import story_engine
import workflow_profiles
from studio_actions import StudioActions, production_prompt, url_for_comfy

BASE = Path(__file__).resolve().parent
desktop_assets = BASE.parent / "New folder" / "assets"
ASSETS = Path(os.environ.get("AXDAR_ASSETS") or (desktop_assets if desktop_assets.is_dir() else BASE / "assets"))
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
    return workflow_profiles.inspect_workflow(name, graph)

actions = StudioActions(globals())
try:
    candidate = actions.bootstrap_workflow()
    active_workflow = actions.store.active() if candidate else None
    workflow_error = "" if active_workflow else "Import or map a compatible ComfyUI API workflow."
except (ValueError, OSError) as exc:
    active_workflow = None
    workflow_error = str(exc)


def save(path, obj):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), "utf-8")
    temp.replace(path)

def project():
    if DB.is_file():
        return json.loads(DB.read_text("utf-8"))
    db = story_engine.new_project([
        {"scope":"all","text":"حافظ على شكل الشخصية وملابسها حسب الصورة الأساسية المختارة."},
        {"scope":"Cozy Sunlit Traditional Living Room.png","text":"حافظ على ترتيب أثاث الغرفة واتجاه الإضاءة."},
        {"scope":"dialog","text":"الكاميرا ثابتة أثناء الحوار."}])
    return actions.commit(db)

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

import production
import vast_deploy
import drive_archive
import take_quality
from episode_editor import Editor,valid_id
from freecut_bridge import FreecutBridge
from workflow_manager import WorkflowManager
from direct_session import DirectSession
from workflow_install_remote import redact

def prepare_shot(url, folder, eid, s, wf, record):
    images, voices = generation_images(s), generation_voices(s)
    for name in dict.fromkeys([*images,*voices]):
        if name not in files or Path(name).name != name:
            raise ValueError("Missing asset: " + name)
        shutil.copy2(files[name], folder/name)
    return workflow_profiles.runtime_graph(wf, s, record["prompt"], images, voices,
        lambda name: upload(url, folder/name, uuid.uuid4().hex + Path(name).suffix),
        "AXDAR/" + eid + "/" + s["id"] + "/" + folder.name)

def download_output(url,folder,h,record):
    outputs=h.get("outputs",{}).get(str(record["output_node"]),{})
    video=next((v for group in outputs.values() if isinstance(group,list) for v in group if isinstance(v,dict) and v.get("filename","").lower().endswith(".mp4")),None)
    if not video: raise ValueError("No MP4 returned from the final output node.")
    path="/view?"+urlencode({k:video[k] for k in ("filename","subfolder","type") if k in video})
    part=folder/"video.part"
    resume=folder/"video.resume.json"
    previous=production.read(resume,{})
    tag=previous.get("etag") if previous.get("path")==path else None
    offset=part.stat().st_size if tag and part.is_file() else 0
    headers={"Range":f"bytes={offset}-","If-Range":tag} if offset else None
    try:
        src=remote(url,path,headers=headers)
    except ValueError as exc:
        if not offset or not isinstance(exc.__cause__,HTTPError) or exc.__cause__.code!=416:
            raise
        src=remote(url,path)

    expected=None
    if src.status==206:
        match=re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)",src.headers.get("Content-Range",""))
        if offset and src.headers.get("ETag")==tag and match:
            start,end,total=map(int,match.groups())
            if start==offset and start<=end==total-1:
                expected=total
        if expected is None:
            src.close()
            src=remote(url,path)
    with src:
        if expected is None:
            if src.status!=200:
                raise ConnectionError("ComfyUI did not return a complete video or a valid resume range.")
            offset=0
            length=src.headers.get("Content-Length","")
            expected=int(length) if length.isdecimal() else None
        with part.open("ab" if offset else "wb") as out:
            tag=src.headers.get("ETag","")
            if tag.startswith('"'):
                production.atomic(resume,{"path":path,"etag":tag})
            else:
                resume.unlink(missing_ok=True)
            shutil.copyfileobj(src,out)
    size=part.stat().st_size
    if not size or expected is not None and size!=expected:
        raise ConnectionError("The ComfyUI video download ended before all bytes arrived.")

def production_busy():
    return controller.current().get("status") in (
        "waiting for connection","reconnecting","deploying","starting worker",
        "checking ComfyUI and requirements","running")


workspace_started=threading.Event()


def discover_vast():
    workspace_started.wait()
    while True:
        connection=controller.connection
        view=connection.snapshot()
        if view.get("auto",True) and view["state"] not in ("Ready","Connecting","ComfyUI Starting","Reconnecting"):
            with LOCK:
                resuming = direct_session.interrupted and time.time() < direct_session.plan["view"]["end"] - 10
                if not (running or production_busy() or workflow_manager.busy() or resuming):
                    if view.get("ssh"):
                        connection.reconnect()
                    else:
                        connection.refresh()
        time.sleep(30)


def run_direct(url,wf,target):
    global running
    running=True
    threading.Thread(target=direct_session.run,args=(url,wf,target),daemon=True).start()


controller=vast_deploy.Controller(globals())
drive_pending=None
editor=Editor(globals())
freecut=FreecutBridge(editor)
workflow_manager=WorkflowManager(globals())
direct_session=DirectSession(globals())


def open_workspace_session(mode):
    if mode not in ("new","resume"):
        raise ValueError("Choose a new session or resume the previous one.")
    with LOCK:
        if mode=="new":
            if running:
                raise ValueError("Pause the current production before opening a new session.")
            controller.archive_for_new_session()
            direct_session.archive_for_new_session()
            controller.connection.reconnect(discover_new=True)
        elif not workspace_started.is_set():
            controller.connection.reconnect()
            controller.reconnect()
            if direct_session.interrupted:
                threading.Thread(target=direct_session.resume_interrupted,daemon=True).start()
        workspace_started.set()
        workflow_manager.watch()
        return {"episode":session.get("episode") or controller.current().get("episode")}


class Handler(BaseHTTPRequestHandler):
    def send(self, data, mime="application/json", status=200):
        if not isinstance(data,bytes): data=json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type",mime)
        self.send_header("Cache-Control","no-store")
        self.send_header("Content-Length",str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def send_file(self,path,download=None):
        length=path.stat().st_size
        start,end=0,length-1
        range_header=self.headers.get("Range","")
        if range_header:
            match=re.fullmatch(r"bytes=(\d*)-(\d*)",range_header)
            if match and any(match.groups()) and length:
                first,last=match.groups()
                if first:
                    start=int(first)
                    end=min(end,int(last)) if last else end
                else:
                    start=max(0,length-int(last))
            if not match or not any(match.groups()) or start>end:
                self.send_response(416)
                self.send_header("Content-Range",f"bytes */{length}")
                self.send_header("Content-Length","0")
                self.end_headers()
                return
        self.send_response(206 if range_header else 200)
        self.send_header("Content-Type",mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges","bytes")
        self.send_header("Cache-Control","private, no-store")
        self.send_header("Content-Length",str(end-start+1))
        if range_header:
            self.send_header("Content-Range",f"bytes {start}-{end}/{length}")
        if download:
            self.send_header("Content-Disposition",f'attachment; filename="{download}"')
        self.end_headers()
        try:
            with path.open("rb") as stream:
                stream.seek(start)
                remaining=end-start+1
                while remaining:
                    block=stream.read(min(remaining,256*1024))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining-=len(block)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        from urllib.parse import unquote
        route=unquote(urlsplit(self.path).path)
        if self.headers.get("Host") != f"127.0.0.1:{PORT}": return self.send({},status=403)
        if route in ("/workspaces.js", "/workspaces.css", "/production-ui.js", "/plugin-manager.js", "/connection-ui.js", "/story-ui.js", "/workflow-ui.js", "/editor-ui.js", "/workflow-manager-ui.js"):
            mime = "text/javascript" if route.endswith(".js") else "text/css"
            return self.send((BASE/route[1:]).read_bytes(), mime + "; charset=utf-8")
        if route=="/":
            return self.send((BASE/"studio.html").read_text("utf-8").replace("__TOKEN__",TOKEN).encode(),"text/html; charset=utf-8")
        if route.startswith("/editor/freecut/srt/"):
            try:
                subtitles=freecut.subtitles(route.rsplit("/",1)[-1]).encode("utf-8")
                return self.send(subtitles,"text/plain; charset=utf-8")
            except ValueError as exc:
                return self.send({"error":str(exc)},status=404)
        if route.startswith("/editor/files/"):
            parts=route.split("/")
            if len(parts)==6 and valid_id(parts[3]) and valid_id(parts[4]) and parts[5] in ("video.mp4","subtitles.srt","subtitles.vtt","download"):
                _,_,_,eid,jid,name=parts
                job=editor.get_job(eid,jid)
                if job and job["status"]=="done" and (name!="download" or job["kind"]=="final"):
                    folder=editor.job_dir(eid,jid)
                    path=folder/("video.mp4" if name=="download" else name)
                    if path.is_file():
                        label=None
                        if name=="download":
                            kind="review-cut" if job.get("reviewCut") else "episode"
                            label=f"AXDAR-{kind}-{eid[:8]}-{job['media']['width']}x{job['media']['height']}.mp4"
                        elif name=="subtitles.srt":
                            label=f"AXDAR-{eid[:8]}.srt"
                        return self.send_file(path,label)
            return self.send({},status=404)
        if route=="/drive/callback":
            global drive_pending
            query=parse_qs(urlsplit(self.path).query)
            state=query.get("state",[""])[0]
            with LOCK:
                pending=drive_pending
                if pending and secrets.compare_digest(state,pending["state"]):
                    drive_pending=None
                else:
                    pending=None
            try:
                drive_archive.connect(pending,state,query.get("code",[""])[0])
                message="Google Drive connected. Return to AXDAR Production to enable archive backup."
            except (ValueError,OSError,RuntimeError):
                message="Google authorization did not complete. Return to AXDAR and retry the connection."
            return self.send(message.encode(),"text/plain; charset=utf-8")
        if route=="/drive/status":
            try:
                return self.send({"connected":bool(drive_archive.account())})
            except Exception:
                return self.send({"connected":False})
        if route=="/api":
            with LOCK:
                db = project()
                if db.get("schema_version") == story_engine.SCHEMA_VERSION and story_engine.sync_takes(db, DATA):
                    save(DB, db)
                return self.send(dict(project=db, assets=active_assets(db), migration=story_engine.migration_preview(db, DATA)))
        if route=="/status":
            if workspace_started.is_set():
                workflow_manager.watch()
            result=[]
            for p in sorted(DATA.glob("*/*/*/job.json")):
                j=json.loads(p.read_text("utf-8"))
                relative=p.parent.relative_to(DATA).as_posix()
                review=j.get("review")
                video=(p.parent/"video.mp4").exists() and review in (None,"accepted")
                preview=(p.parent/"preview.mp4").exists() or (p.parent/"video.mp4").exists()
                result.append(dict(episode=p.parents[2].name,id=j["shot"],version=p.parent.name,status=j["status"],
                                   error=redact(j.get("error","")),revision=j["revision"],takeId=j.get("take_id"),review=review,
                                   promptId=j.get("ticket"),started=j.get("started"),workflowVersion=j.get("workflow_version"),
                                   duration=j.get("duration"),
                                   quality=j.get("quality"),renderSeconds=j.get("renderSeconds"),
                                   remote=bool(j.get("remote_run")),video="/video/"+relative if video else "",
                                   artifactPending=bool(j.get("artifact_pending")),
                                   preview="/take-preview/"+relative if preview and review else ""))
            return self.send(dict(production=controller.current(),running=running,session=session,results=result,workflow=active_workflow["name"] if active_workflow else "Workflow blocked",workflowError=workflow_error,connection=controller.connection.snapshot()))
        p=files.get(route[7:]) if route.startswith("/asset/") else None
        if re.fullmatch(r"/(video|take-preview)/[a-f0-9-]+/[a-f0-9-]+/\d+",route):
            kind,eid,sid,version=route[1:].split("/")
            folder=DATA/eid/sid/version
            if kind=="take-preview":
                p=folder/"preview.mp4"
                if not p.is_file():
                    p=folder/"video.mp4"
            elif production.read(folder/"job.json",{}).get("review") in (None,"accepted"):
                p=folder/"video.mp4"
        if p and p.is_file():
            return self.send_file(p)
        return self.send({},status=404)
    def do_POST(self):
        global running,session,active_workflow,workflow_error,drive_pending
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
            limit = 32 * 1024 * 1024 if self.path in ("/workflow/import","/workflow/attach","/workflow/inspect","/story/parse","/story/commit","/save") else 8 * 1024 * 1024
            if not 0 < size <= limit:
                raise ValueError("Request is empty or exceeds the upload limit.")
            blob = self.rfile.read(size)
            if len(blob) != size:
                raise ValueError("Upload incomplete.")
            filename = unquote(self.headers.get("X-Filename",""))
            binary = self.path in ("/workflow/import","/workflow/attach") or self.path == "/story/parse" and filename
            obj = {} if binary else json.loads(blob)
            if self.path=="/workspace-session":
                if self.headers.get("Host") != f"127.0.0.1:{PORT}": return self.send({},status=403)
                return self.send(open_workspace_session(obj.get("mode")))
            if self.path=="/workflow-manager":
                return self.send(workflow_manager.action(obj))
            if self.path=="/direct-production":
                with LOCK:
                    if obj.get("action")=="logs":
                        eid,sid,version=obj.get("episode"),obj.get("shot"),obj.get("version")
                        if not valid_id(eid) or not valid_id(sid) or not re.fullmatch(r"[0-9]{1,22}",str(version)):
                            raise ValueError("Select a recorded shot attempt.")
                        folder=DATA/eid/sid/version
                        job=production.read(folder/"job.json",{})
                        if not job:
                            raise ValueError("Select a recorded shot attempt.")
                        hist=production.read(folder/"history.json",{})
                        rows=hist.get("status",{}).get("messages",[])
                        messages=[dict(event=name,node=meta.get("node_id"),nodeType=meta.get("node_type"),
                                       error=redact(meta.get("exception_message",""))[:900])
                                  for name,meta in rows[-15:] if isinstance(meta,dict)]
                        return self.send(dict(promptId=job.get("ticket"),status=job["status"],
                                              error=redact(job.get("error","")),messages=messages))
                    return self.send(direct_session.control(obj))
            if self.path.startswith("/workflow/"):
                if self.path=="/workflow/attach":
                    obj["version"]=self.headers.get("X-Workflow-Version")
                with LOCK:
                    return self.send(actions.workflow(self.path.rsplit("/",1)[1], obj, blob if binary else None, filename))
            if self.path == "/migrate" or self.path.startswith(("/project/","/story/","/shots/","/characters/")):
                with LOCK:
                    return self.send(actions.act(self.path, obj, blob if binary else None, filename))
            if self.path=="/editor":
                if self.headers.get("Host") != f"127.0.0.1:{PORT}":
                    return self.send({},status=403)
                if obj.get("action") in ("freecut-prepare","freecut-status"):
                    return self.send(freecut.action(obj))
                return self.send(editor.action(obj))
            if self.path=="/drive/connect":
                if self.headers.get("Host") != f"127.0.0.1:{PORT}":
                    return self.send({},status=403)
                with LOCK:
                    drive_pending,url=drive_archive.authorization(obj["client"],f"http://127.0.0.1:{PORT}/drive/callback")
                return self.send({"url":url})
            if self.path=="/connection":
                if self.headers.get("Host") != f"127.0.0.1:{PORT}": return self.send({},status=403)
                action=obj.get("action")
                connection=controller.connection
                if action=="key":
                    from vast_connection import api_key
                    try: api_key(obj.get("key"))
                    except Exception: raise ValueError("Windows credential storage failed")
                    action="refresh"
                if action=="hf":
                    from vast_connection import hf_token
                    hf_token(obj.get("token"))
                    return self.send({"ok":True})
                if action=="instances": return self.send(connection.discover_instances())
                if action=="refresh":
                    with LOCK:
                        return self.send(connection.refresh(keep_active=running or production_busy() or workflow_manager.busy()))
                if action=="connect": return self.send(connection.connect(obj.get("instance"),obj.get("auto",False)))
                if action=="connect-ssh": return self.send(connection.connect_ssh(obj.get("ssh"),obj.get("auto",False)))
                if action=="reconnect":
                    with LOCK:
                        return self.send(connection.reconnect())
                if action=="disconnect": return self.send(connection.disconnect())
                if action=="command": return self.send({"output":connection.command(obj.get("command"))})
                raise ValueError("Unknown connection action")
            if self.path=="/production":
                with LOCK:
                    if running: raise ValueError("The manual session is active")
                    if workflow_manager.busy() and obj.get("action") in ("start","resume","retry"):
                        raise ValueError("Wait until the Workflow installation finishes.")
                    if not active_workflow and obj.get("action") == "start" and not obj.get("archive"):
                        raise ValueError(workflow_error)
                    if (obj.get("action")=="start" and not obj.get("archive") and active_workflow
                            and active_workflow.get("format")=="ComfyUI UI JSON"):
                        raise ValueError("Start this workflow from Direct ComfyUI submission so AXDAR can verify the original graph first.")
                    return self.send(controller.action(obj))
            with LOCK:
                if self.path=="/workflow":
                    result = actions.workflow("import", {}, json.dumps(obj["graph"]).encode(), str(obj["name"]))
                    return self.send(dict(result, name=result["profile"]["name"]))
                if self.path=="/save":
                    return self.send(actions.general_save(obj))
                elif self.path=="/library/remove":
                    name = obj["name"]
                    db = project()
                    actions.check(db, obj)
                    if name not in files:
                        raise ValueError("File not found in the library.")
                    if name not in db.setdefault("removedAssets", []):
                        db["removedAssets"].append(name)
                    clean_assets(db)
                    for character in db.get("characters", []):
                        for field in ("images", "voices"):
                            character[field] = [item for item in character.get(field, []) if item != name]
                    actions.commit(db)
                    return self.send(dict(project=db, assets=active_assets(db)))
                elif self.path=="/start":
                    if not active_workflow: raise ValueError(workflow_error)
                    if production_busy():
                        raise ValueError("Finish or pause the active production first.")
                    if workflow_manager.busy():
                        raise ValueError("This Vast instance has an active workflow scan or installation. Open Installation Monitor to check it.")
                    if running: raise ValueError("الجلسة تعمل حالياً")
                    connected=controller.connection.snapshot().get("url")
                    target=controller.connection.target() if connected else None
                    url=(connected or obj.get("url", "")).strip().rstrip("/")
                    if urlsplit(url).scheme not in ("http","https") or not urlsplit(url).hostname: raise ValueError("أدخل رابط ComfyUI")
                    minutes=int(obj.get("minutes",60))
                    if not 2<=minutes<=1440: raise ValueError("المدة من دقيقتين إلى ١٤٤٠ دقيقة")
                    db=project()
                    ep=next(e for e in db["episodes"] if e["id"]==obj["episode"])
                    jobs, skipped = actions.approved_jobs(db, ep)
                    retry=obj.get("retry")
                    if retry:
                        if not valid_id(retry):
                            raise ValueError("Select a failed shot.")
                        jobs=[row for row in jobs if row[1]["id"]==retry]
                        past=sorted((DATA/ep["id"]/retry).glob("*/job.json"),reverse=True)
                        latest=production.read(past[0],{}) if past else {}
                        if not jobs or latest.get("status")!="error" or latest.get("revision")!=jobs[0][1]["revision"]:
                            raise ValueError("Retry is only available for a failed shot of the current revision.")
                    if not jobs:
                        raise ValueError(next((reason for reasons in skipped.values() for reason in reasons), "Approve at least one ready shot."))
                    end=time.time()+60*minutes
                    view=dict(end=end,episode=ep["id"],submitted=len(jobs),blocked=skipped,
                              phase="Preparing ComfyUI",completed=0,failed=0)
                    direct_session.start(view,jobs,active_workflow,target,url,retry=bool(retry))
                    run_direct(url,active_workflow,target)
                    return self.send({"ok":True,"submitted":len(jobs),"blocked":skipped})
                else: return self.send({},status=404)
            self.send({"ok":True})
        except Exception as e: self.send({"error":str(e)},status=400)
    def log_message(self,*args): pass

class StudioServer(ThreadingHTTPServer):
    def server_bind(self):
        if hasattr(socket,"SO_EXCLUSIVEADDRUSE"):
            self.allow_reuse_address=False
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()


if __name__=="__main__":
    address=f"http://127.0.0.1:{PORT}"
    try: server=StudioServer(("127.0.0.1",PORT),Handler)
    except OSError:
        with urlopen(address+"/api",timeout=3) as response:
            if "project" not in json.load(response): raise
        webbrowser.open(address)
    else:
        webbrowser.open(address)
        threading.Thread(target=discover_vast,daemon=True).start()
        server.serve_forever()
