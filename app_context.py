"""Explicit dependency-injection container for AXDAR Studio."""
import json
import os
import re
import secrets
import threading
import time
import uuid
from pathlib import Path

import comfy_client
import story_engine
import take_quality
import vast_connection
import vast_credentials
import vast_ssh_ops
import workflow_profiles
from direct_session import DirectSession
from studio_actions import StudioActions
from workflow_manager import WorkflowManager

PHOTOS = comfy_client.PHOTOS
VOICES = comfy_client.VOICES


class AppContainer:
    """Wires the isolated departments via explicit attributes."""

    def __init__(self, base_dir, port=18753):
        self.BASE = Path(base_dir).resolve()
        desktop_assets = self.BASE.parent / "New folder" / "assets"
        self.ASSETS = Path(os.environ.get("AXDAR_ASSETS") or (desktop_assets if desktop_assets.is_dir() else self.BASE / "assets"))
        self.DATA = self.BASE / "episodes"
        self.DATA.mkdir(exist_ok=True)
        (self.DATA / "production").mkdir(exist_ok=True)
        self.DB = self.DATA / "project.json"
        self.LIBRARY = self.BASE / "library"
        self.LIBRARY.mkdir(exist_ok=True)
        self.WORKFLOW_STATE = self.BASE / "selected-workflow.json"

        self.LOCK = threading.RLock()
        self.TOKEN = secrets.token_hex(24)
        self.PORT = port
        self.running = False
        self.session = {}
        self.workspace_started = threading.Event()
        self.restart_requested = threading.Event()

        initial_files = {p.name: p for p in self.ASSETS.rglob("*") if p.is_file()}
        initial_files.update({p.name: p for p in self.LIBRARY.iterdir() if p.is_file() and p.suffix != ".part"})
        self.files = initial_files

        # Pure ComfyUI / Vast / QC collaborators
        self.ssh_ops = vast_ssh_ops
        self.hf_token = vast_credentials.hf_token
        self.identity = vast_credentials.identity
        self.take_quality = take_quality
        self.prepare_workflow = workflow_profiles.inspect_workflow
        self.generation_images = comfy_client.generation_images
        self.generation_voices = comfy_client.generation_voices
        self.prompt = comfy_client.prompt
        self.remote = comfy_client.remote
        self.reference_available = comfy_client.reference_available

        # Department wiring
        self.actions = StudioActions(self)
        try:
            candidate = self.actions.bootstrap_workflow()
            self.active_workflow = self.actions.store.active() if candidate else None
            self.workflow_error = "" if self.active_workflow else "Import or map a compatible ComfyUI API workflow."
        except (ValueError, OSError) as exc:
            self.active_workflow = None
            self.workflow_error = str(exc)

        self.connection = vast_connection.Connection(
            self.DATA / "production" / "connection.json",
            vast_ssh_ops.instances,
            vast_ssh_ops.ssh,
        )
        self.workflow_manager = WorkflowManager(
            self,
            ssh_ops=vast_ssh_ops,
            hf_token_fn=vast_credentials.hf_token,
            identity_fn=vast_credentials.identity,
        )
        self.direct_session = DirectSession(self)

    # Dict-style compatibility adapter for subsystems using ctx["..."]
    def __getitem__(self, key):
        try:
            return getattr(self, key)
        except AttributeError as exc:
            raise KeyError(key) from exc

    def __setitem__(self, key, value):
        setattr(self, key, value)

    def __contains__(self, key):
        return hasattr(self, key)

    def get(self, key, default=None):
        return getattr(self, key, default)

    # Asset store (immutable copy-on-write snapshots)
    def files_snapshot(self):
        with self.LOCK:
            return dict(self.files)

    def import_asset(self, filename, blob):
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename).strip(" .")
        stem, ext = Path(filename).stem[:80].strip(" ."), Path(filename).suffix.lower()
        if not stem or ext not in PHOTOS | VOICES:
            raise ValueError("Choose a supported photo or voice file.")
        if re.match(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)", stem, re.I):
            stem = "_" + stem
        with self.LOCK:
            existing = {name.casefold() for name in self.files}
            number, name = 1, stem + ext
            while name.casefold() in existing or (self.LIBRARY / name).exists():
                number += 1
                name = f"{stem} ({number}){ext}"
            target = self.LIBRARY / name
            temp = self.LIBRARY / (uuid.uuid4().hex + ".part")
            try:
                temp.write_bytes(blob)
                temp.replace(target)
            finally:
                temp.unlink(missing_ok=True)
            updated = dict(self.files)
            updated[name] = target
            self.files = updated
            return name

    def active_assets(self, db):
        refreshed = {p.name: p for p in self.ASSETS.rglob("*") if p.is_file() and p.suffix.lower() in PHOTOS | VOICES}
        refreshed.update({p.name: p for p in self.LIBRARY.iterdir() if p.is_file() and p.suffix.lower() in PHOTOS | VOICES})
        with self.LOCK:
            self.files = refreshed
            hidden = set(db.get("removedAssets", []))
            return [name for name in self.files if name not in hidden]

    def clean_assets(self, db):
        hidden = set(db.get("removedAssets", []))
        for episode in db["episodes"]:
            for shot in episode["shots"]:
                if shot["image"] in hidden or hidden.intersection(shot["refs"]):
                    if shot["image"] in hidden:
                        shot["image"] = ""
                    shot["refs"] = [name for name in shot["refs"] if name not in hidden]
                    shot.update(approved=False, revision=str(uuid.uuid4()))

    def save(self, path, obj):
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), "utf-8")
        temp.replace(path)

    def project(self):
        if self.DB.is_file():
            return json.loads(self.DB.read_text("utf-8"))
        return self.actions.commit(story_engine.new_project())

    # ComfyUI client bindings wired to this container
    def json_api(self, url, path, obj=None):
        return comfy_client.json_api(url, path, obj, remote_fn=self.remote)

    def _stage_fallback(self, url, asset, name):
        connection = self.connection
        with connection.lock:
            if connection.snapshot().get("url") != url:
                raise ConnectionError("Active ComfyUI tunnel changed during upload.")
            target = connection.target()
        self.ssh_ops.stage_reference(target, asset, name)

    def upload(self, url, asset, name):
        return comfy_client.upload(
            url, asset, name,
            fallback_stage=self._stage_fallback,
            remote_fn=self.remote,
            available_fn=self.reference_available,
        )

    def prepare_shot(self, url, folder, eid, shot, wf, record):
        return comfy_client.prepare_shot(
            url, folder, eid, shot, wf, record,
            asset_map=self.files_snapshot(),
            upload_fn=self.upload,
            json_api_fn=self.json_api,
        )

    def download_output(self, url, folder, history, record):
        return comfy_client.download_output(url, folder, history, record, remote_fn=self.remote)

    def run_direct(self, url, wf, target):
        self.running = True
        threading.Thread(target=self.direct_session.run, args=(url, wf, target), daemon=True).start()

    def open_workspace_session(self, mode="resume"):
        mode = mode or "resume"
        if mode not in ("new", "resume"):
            raise ValueError("Choose a new session or resume the previous one.")
        with self.LOCK:
            if mode == "new":
                if self.running:
                    raise ValueError("Pause the current production before opening a new session.")
                self.direct_session.archive_for_new_session()
                self.connection.reconnect(discover_new=True)
            elif not self.workspace_started.is_set():
                self.connection.reconnect()
                if self.direct_session.interrupted:
                    threading.Thread(target=self.direct_session.resume_interrupted, daemon=True).start()
            self.workspace_started.set()
            self.workflow_manager.watch()
            return {"episode": self.session.get("episode")}

    def discover_vast_loop(self):
        self.workspace_started.wait()
        while True:
            connection = self.connection
            view = connection.snapshot()
            if view.get("auto", True) and view["state"] not in ("Ready", "Connecting", "ComfyUI Starting", "Reconnecting"):
                with self.LOCK:
                    resuming = self.direct_session.interrupted and time.time() < self.direct_session.plan["view"]["end"] - 10
                    if not (self.running or self.workflow_manager.busy() or resuming):
                        if view.get("ssh"):
                            connection.reconnect()
                        else:
                            connection.refresh()
            time.sleep(10)
