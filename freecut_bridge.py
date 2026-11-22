"""Publish approved AXDAR takes into Freecut's native workspace."""
import functools
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from fractions import Fraction
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlsplit

import editor_media as media
from episode_editor import ordered_shots, source_dialogue, valid_id
from production import atomic, read


FREECUT_PORT = 18754
TAGGED_DIALOGUE = re.compile(r"<d\b[^>]*>(.*?)</d\s*>", re.I | re.S)
LANGUAGE_TAG = re.compile(r"^\[(English|Arabic|French|Spanish|German|Italian)\]\s*", re.I)
LANGUAGES = {"english": "en", "arabic": "ar", "french": "fr",
             "spanish": "es", "german": "de", "italian": "it"}


def dialogue(shot, job, characters):
    settings = job.get("settings") or shot
    source = settings.get("prompt") or shot.get("prompt", "")
    lines = TAGGED_DIALOGUE.findall(source)
    if not lines:
        lines = TAGGED_DIALOGUE.findall(job.get("prompt", ""))
    if not lines:
        lines = [entry["text"] for entry in source_dialogue(settings, characters)]
    language = None
    cleaned = []
    for line in lines:
        text = line.strip()
        mark = LANGUAGE_TAG.match(text)
        if mark:
            language = language or LANGUAGES[mark[1].lower()]
            text = text[mark.end():]
        if text:
            cleaned.append(text)
    if not language:
        language = "ar" if any(re.search(r"[\u0600-\u06ff]", line) for line in cleaned) else "en"
    return language, cleaned


class FreecutFiles(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
            self.send_error(403)
            return
        super().do_GET()

    def translate_path(self, request):
        route = unquote(urlsplit(request).path)
        root = Path(self.directory).resolve()
        target = (root / route.lstrip("/")).resolve()
        if target.is_relative_to(root) and target.is_file():
            return str(target)
        if target.is_relative_to(root) and not Path(route).suffix:
            return str(root / "index.html")
        return str(root / ".missing")

    def end_headers(self):
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        super().end_headers()

    def log_message(self, *_args):
        pass


class FreecutBridge:
    def __init__(self, editor):
        self.editor = editor
        self.lock = threading.RLock()
        self.jobs = {}
        self.server = None

    @property
    def workspace(self):
        return self.editor.data / "editor" / "freecut"

    @property
    def tools(self):
        return self.editor.ctx["BASE"] / "tools" / "freecut"

    def start_server(self):
        if self.server is None:
            dist = self.tools / "dist"
            if not (dist / "index.html").is_file():
                raise ValueError("Install Freecut with setup-freecut.ps1 in the Studio folder.")
            handler = functools.partial(FreecutFiles, directory=str(dist))
            self.server = ThreadingHTTPServer(("127.0.0.1", FREECUT_PORT), handler)
            threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def action(self, obj):
        eid = obj.get("episode")
        if not valid_id(eid):
            raise ValueError("Select an AXDAR episode.")
        with self.lock:
            if obj["action"] == "freecut-prepare" and self.jobs.get(eid, {}).get("state") != "working":
                self.start_server()
                self.jobs[eid] = {"state": "working", "message": "Preparing approved takes"}
                threading.Thread(target=self.prepare_job, args=(eid,), daemon=True).start()
            result = self.jobs.get(eid) or {"state": "idle"}
            return {**result, "workspace": str(self.workspace),
                    "url": f"http://127.0.0.1:{FREECUT_PORT}/editor/{eid}"}

    def prepare_job(self, eid):
        try:
            with self.editor.ctx["LOCK"], self.editor.lock:
                db = self.editor.ctx["project"]()
                episode = next((ep for ep in db["episodes"] if ep["id"] == eid), None)
                if not episode:
                    raise ValueError("This episode is no longer in AXDAR.")
                assembly = self.editor.load(eid)
                entries = [(shot, self.editor.takes(eid, shot["id"]))
                           for shot in ordered_shots(episode) if shot.get("approved")]
                comparison = next((job for jid in [*reversed(assembly["finals"]), assembly["draft"]]
                                   if (job := self.editor.get_job(eid, jid)) and job["status"] == "done"), None)
            result = self.prepare(episode, entries, db.get("characters", []), comparison)
            result["state"] = "ready"
        except Exception as exc:
            result = {"state": "error", "message": str(exc)[:350]}
        with self.lock:
            self.jobs[eid] = result

    def publish_media(self, asset_id, name, source, info, sha, stamp):
        folder = self.workspace / "media" / asset_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "source.mp4"
        if not path.is_file():
            part = folder / f"{uuid.uuid4()}.part"
            try:
                shutil.copyfile(source, part)
                os.replace(part, path)
            finally:
                part.unlink(missing_ok=True)
        metadata = folder / "metadata.json"
        if not metadata.exists():
            try:
                fps = float(Fraction(info.get("fps") or "30"))
            except (ValueError, ZeroDivisionError):
                fps = 30
            stat = source.stat()
            atomic(metadata, dict(id=asset_id, storageType="workspace", fileName=name + ".mp4",
                fileSize=stat.st_size, fileLastModified=stat.st_mtime_ns // 1_000_000,
                mimeType="video/mp4", duration=info["duration"], width=info["width"], height=info["height"],
                fps=fps or 30, codec=info["codec"], bitrate=round(stat.st_size * 8 / info["duration"]),
                audioCodec=info.get("audioCodec"), tags=[], createdAt=stamp, updatedAt=stamp,
                contentHash=sha))
        return read(metadata)

    def subtitles(self, eid):
        if not valid_id(eid):
            raise ValueError("Select an AXDAR episode.")
        folder = self.workspace / "projects" / eid
        project = read(folder / "project.json")
        if not project:
            raise ValueError("Prepare this episode in Freecut first.")
        fps = project["metadata"]["fps"]
        cues = []
        for item in project["timeline"]["items"]:
            captions = item.get("transcriptCaptions")
            if not captions or not captions.get("enabled"):
                continue
            offset = item["from"] / fps
            source = item.get("sourceStart", 0) / (item.get("sourceFps") or fps)
            speed = item.get("speed") or 1
            duration = item["durationInFrames"] / fps
            for cue in captions["cues"]:
                start = max(0, (cue["startSeconds"] - source) / speed)
                end = min(duration, (cue["endSeconds"] - source) / speed)
                if start < end:
                    cues.append(dict(start=offset+start, end=offset+end, text=cue["text"]))
        if not cues:
            raise ValueError("There are no enabled synchronized subtitles in the saved project.")
        cues.sort(key=lambda cue: (cue["start"], cue["end"]))
        return "\n".join(f"{i}\n{media.time_text(cue['start'])} --> {media.time_text(cue['end'])}\n{cue['text']}\n"
                         for i, cue in enumerate(cues, 1))

    def align_media(self, entries):
        if not entries:
            return {}
        python = os.environ.get("AXDAR_SUBTITLE_PYTHON") or str(self.tools / "align" /
            ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
        ffmpeg = media.tool("ffmpeg")
        env = dict(os.environ, PATH=str(Path(ffmpeg).parent) + os.pathsep + os.environ.get("PATH", ""))
        with TemporaryDirectory(prefix="axdar-subtitles-") as temp:
            folder = Path(temp)
            manifest, output = folder / "input.json", folder / "result.json"
            pending, results = [], {}
            for entry in entries:
                wav = folder / (entry["media"] + ".wav")
                try:
                    media.run([ffmpeg, "-v", "error", "-nostdin", "-i", str(entry["source"]),
                               "-map", "0:a:0", "-ar", "16000", "-ac", "1", str(wav)], timeout=120)
                    pending.append(dict(media=entry["media"], wav=str(wav), language=entry["language"],
                                        lines=entry["lines"], duration=entry["duration"]))
                except (OSError, ValueError):
                    results[entry["media"]] = {"error": "The approved take has no readable dialogue audio."}
            if pending:
                atomic(manifest, pending)
                try:
                    run = subprocess.run([python, str(Path(__file__).with_name("subtitle_align.py")),
                                          str(manifest), str(output)], capture_output=True, timeout=1800,
                                         creationflags=media.CREATION, env=env)
                    if run.returncode or not output.is_file():
                        raise ValueError("Stable-ts could not start. Check the subtitle installation and model.")
                    results.update({item["media"]: item for item in read(output)})
                except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                    message = str(exc) if isinstance(exc, ValueError) else "Stable-ts is unavailable or timed out."
                    results.update({item["media"]: {"error": message} for item in pending})
            return results

    def prepare(self, episode, entries, characters, comparison=None):
        eid = episode["id"]
        folder = self.workspace / "projects" / eid
        path = folder / "project.json"
        manifest = folder / "axdar-takes.json"
        record = read(manifest, {"seeded": [], "alignment": {}})
        stamp = time.time_ns() // 1_000_000
        ready = {}
        clips = []
        seen_shots = set()
        pending = []
        for shot, takes in entries:
            for take in (item for item in takes if item["ready"]):
                key = str(uuid.uuid5(uuid.UUID(shot["id"]), take["version"]))
                label = shot.get("sourceHeading") or f"Shot {len(clips) + 1}"
                if shot["id"] not in seen_shots:
                    clips.append((shot["id"], key, label))
                    seen_shots.add(shot["id"])
                ready[key] = self.publish_media(key, label, take["path"], take["media"], take["sha"], stamp)
                job = read(take["path"].with_name("job.json"), {})
                language, lines = dialogue(shot, job, characters)
                if lines and not record["alignment"].get(key, {}).get("cues"):
                    pending.append(dict(media=key, source=take["path"], duration=take["media"]["duration"],
                                        language=language, lines=lines, label=label))
        if not ready and not path.exists():
            raise ValueError("This episode has no approved local takes.")
        if comparison:
            comparison_id = str(uuid.uuid5(uuid.UUID(eid), comparison["id"]))
            source = self.editor.job_dir(eid, comparison["id"]) / "video.mp4"
            ready[comparison_id] = self.publish_media(comparison_id, "AXDAR Assembly comparison", source,
                                                       media.probe(source), None, stamp)
        for key, outcome in self.align_media(pending).items():
            record["alignment"][key] = outcome
        first = next(iter(ready.values()), None)
        project = read(path)
        if not project:
            project = dict(id=eid, name=episode["title"], description="AXDAR Studio approved takes",
                           schemaVersion=15, createdAt=stamp, updatedAt=stamp, duration=0,
                           metadata=dict(width=first["width"], height=first["height"],
                                         fps=round(first["fps"]) or 30, backgroundColor="#000000"),
                           timeline=dict(tracks=[dict(id="axdar-video", name="Approved takes", kind="video",
                              order=0, height=60, visible=True, locked=False, muted=False, solo=False,
                              syncLock=True, items=[])], items=[], transitions=[]))
        items = project["timeline"]["items"]
        existing = {item["id"] for item in items}
        fps = project["metadata"]["fps"]
        start = max((item["from"] + item["durationInFrames"] for item in items), default=0)
        changed = not path.exists()
        for shot_id, asset, label in clips:
            if shot_id in record["seeded"] or shot_id in existing:
                continue
            old = ready[asset]
            length = max(1, round(old["duration"] * fps))
            tracks = project["timeline"]["tracks"]
            track = next((track for track in tracks if track.get("kind") == "video"), None)
            if not track:
                track = dict(id="axdar-video", name="Approved takes", kind="video", order=len(tracks),
                             height=60, visible=True, locked=False, muted=False, solo=False, items=[])
                tracks.append(track)
            item = dict(id=shot_id, type="video", trackId=track["id"],
                        from_=start, durationInFrames=length, label=label, mediaId=asset, src="",
                        sourceStart=0, sourceEnd=round(old["duration"] * old["fps"]), sourceFps=old["fps"],
                        sourceDuration=round(old["duration"] * old["fps"]), volume=0, speed=1, transform={})
            item["from"] = item.pop("from_")
            items.append(item)
            record["seeded"].append(shot_id)
            changed = True
            start += length
        fresh = {entry["media"] for entry in pending}
        for item in items:
            oid = item.get("mediaId")
            cues = record["alignment"].get(oid, {}).get("cues")
            if oid in fresh and cues and not item.get("transcriptCaptions"):
                item["transcriptCaptions"] = dict(type="transcript", mediaId=oid, enabled=True, updatedAt=stamp,
                    cues=[dict(cue, id=str(uuid.uuid4())) for cue in cues])
                changed = True
        folder.mkdir(parents=True, exist_ok=True)
        marker = self.workspace / ".freecut-workspace.json"
        if not marker.exists():
            atomic(marker, {"schemaVersion": "2.0", "createdAt": stamp})
        link_path = folder / "media-links.json"
        links = read(link_path, {"version": "1.0", "mediaIds": []})
        linked = {item["id"] for item in links["mediaIds"]}
        if ready.keys() - linked:
            links["mediaIds"].extend({"id": key, "addedAt": stamp} for key in ready if key not in linked)
            atomic(link_path, links)
        if changed:
            project.update(updatedAt=stamp, duration=start / fps)
            atomic(path, project)
        atomic(manifest, record)
        with self.lock:
            index_path = self.workspace / "index.json"
            index = read(index_path, {"version": "1.0", "projects": []})
            if not any(item["id"] == eid for item in index["projects"]):
                index["projects"].append(dict(id=eid, name=project["name"], updatedAt=project["updatedAt"]))
                index["updatedAt"] = stamp
                atomic(index_path, index)
        return {"clips": len(clips), "comparison": bool(comparison),
                "subtitles": any(item.get("transcriptCaptions", {}).get("enabled") for item in items),
                "reviews": [{"clip": ready[key]["fileName"], "message": value["error"]}
                            for key, value in record["alignment"].items() if key in ready and value.get("error")]}
