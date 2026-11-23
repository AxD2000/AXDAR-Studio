"""Department-isolated HTTP route dispatchers for AXDAR Studio."""
import json
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit

import drive_archive
import production
import story_engine
import vast_credentials
from episode_editor import valid_id
from workflow_install_remote import redact

STATIC_ASSETS = {
    "/workspaces.js", "/workspaces.css", "/production-ui.js", "/plugin-manager.js",
    "/connection-ui.js", "/story-ui.js", "/workflow-ui.js", "/editor-ui.js", "/workflow-manager-ui.js",
}


def handle_get(handler, app):
    route = unquote(urlsplit(handler.path).path)
    if handler.headers.get("Host") != f"127.0.0.1:{app.PORT}":
        return handler.send({}, status=403)
    if route in STATIC_ASSETS:
        mime = "text/javascript" if route.endswith(".js") else "text/css"
        return handler.send((app.BASE / route[1:]).read_bytes(), mime + "; charset=utf-8")
    if route == "/":
        html = (app.BASE / "studio.html").read_text("utf-8").replace("__TOKEN__", app.TOKEN).encode()
        return handler.send(html, "text/html; charset=utf-8")
    if route.startswith("/editor/"):
        return _get_editor(handler, app, route)
    if route.startswith("/drive/"):
        return _get_drive(handler, app, route)
    if route == "/api":
        with app.LOCK:
            db = app.project()
            if db.get("schema_version") == story_engine.SCHEMA_VERSION and story_engine.sync_takes(db, app.DATA):
                app.save(app.DB, db)
            return handler.send(dict(
                project=db,
                assets=app.active_assets(db),
                migration=story_engine.migration_preview(db, app.DATA),
            ))
    if route == "/status":
        return _get_status(handler, app)
    return _get_media(handler, app, route)


def _get_editor(handler, app, route):
    if route.startswith("/editor/freecut/srt/"):
        try:
            subtitles = app.freecut.subtitles(route.rsplit("/", 1)[-1]).encode("utf-8")
            return handler.send(subtitles, "text/plain; charset=utf-8")
        except ValueError as exc:
            return handler.send({"error": str(exc)}, status=404)
    if route.startswith("/editor/files/"):
        parts = route.split("/")
        if len(parts) == 6 and valid_id(parts[3]) and valid_id(parts[4]) and parts[5] in ("video.mp4", "subtitles.srt", "subtitles.vtt", "download"):
            _, _, _, eid, jid, name = parts
            job = app.editor.get_job(eid, jid)
            if job and job["status"] == "done" and (name != "download" or job["kind"] == "final"):
                folder = app.editor.job_dir(eid, jid)
                path = folder / ("video.mp4" if name == "download" else name)
                if path.is_file():
                    label = None
                    if name == "download":
                        kind = "review-cut" if job.get("reviewCut") else "episode"
                        label = f"AXDAR-{kind}-{eid[:8]}-{job['media']['width']}x{job['media']['height']}.mp4"
                    elif name == "subtitles.srt":
                        label = f"AXDAR-{eid[:8]}.srt"
                    return handler.send_file(path, label)
    return handler.send({}, status=404)


def _get_drive(handler, app, route):
    if route == "/drive/callback":
        query = parse_qs(urlsplit(handler.path).query)
        state = query.get("state", [""])[0]
        with app.LOCK:
            pending = app.drive_pending
            if pending and secrets.compare_digest(state, pending["state"]):
                app.drive_pending = None
            else:
                pending = None
        try:
            drive_archive.connect(pending, state, query.get("code", [""])[0])
            message = "Google Drive connected. Return to AXDAR Production to enable archive backup."
        except (ValueError, OSError, RuntimeError):
            message = "Google authorization did not complete. Return to AXDAR and retry the connection."
        return handler.send(message.encode(), "text/plain; charset=utf-8")
    if route == "/drive/status":
        try:
            return handler.send({"connected": bool(drive_archive.account())})
        except Exception:
            return handler.send({"connected": False})
    return handler.send({}, status=404)


def _get_status(handler, app):
    if app.workspace_started.is_set():
        app.workflow_manager.watch()
    result = []
    for p in sorted(app.DATA.glob("*/*/*/job.json")):
        j = json.loads(p.read_text("utf-8"))
        relative = p.parent.relative_to(app.DATA).as_posix()
        review = j.get("review")
        video = (p.parent / "video.mp4").exists() and review in (None, "accepted")
        preview = (p.parent / "preview.mp4").exists() or (p.parent / "video.mp4").exists()
        result.append(dict(
            episode=p.parents[2].name, id=j["shot"], version=p.parent.name, status=j["status"],
            error=redact(j.get("error", "")), revision=j["revision"], takeId=j.get("take_id"), review=review,
            promptId=j.get("ticket"), started=j.get("started"), workflowVersion=j.get("workflow_version"),
            duration=j.get("duration"), quality=j.get("quality"), renderSeconds=j.get("renderSeconds"),
            remote=bool(j.get("remote_run")), video="/video/" + relative if video else "",
            artifactPending=bool(j.get("artifact_pending")),
            preview="/take-preview/" + relative if preview and review else "",
        ))
    return handler.send(dict(
        production=app.controller.current(),
        running=app.running,
        session=app.session,
        results=result,
        workflow=app.active_workflow["name"] if app.active_workflow else "Workflow blocked",
        workflowError=app.workflow_error,
        connection=app.controller.connection.snapshot(),
        restartSupported=True,
    ))


def _get_media(handler, app, route):
    p = app.files_snapshot().get(route[7:]) if route.startswith("/asset/") else None
    if re.fullmatch(r"/(video|take-preview)/[a-f0-9-]+/[a-f0-9-]+/\d+", route):
        kind, eid, sid, version = route[1:].split("/")
        folder = app.DATA / eid / sid / version
        if kind == "take-preview":
            p = folder / "preview.mp4"
            if not p.is_file():
                p = folder / "video.mp4"
        elif production.read(folder / "job.json", {}).get("review") in (None, "accepted"):
            p = folder / "video.mp4"
    if p and p.is_file():
        return handler.send_file(p)
    return handler.send({}, status=404)


def handle_post(handler, app):
    if handler.headers.get("Host") != f"127.0.0.1:{app.PORT}" or handler.headers.get("X-Studio") != app.TOKEN:
        return handler.send({}, status=403)
    if app.restart_requested.is_set():
        return handler.send({"error": "Studio is restarting."}, status=503)

    size = int(handler.headers["Content-Length"])
    if handler.path == "/plugins":
        return _post_plugins(handler, size)
    if handler.path == "/library/upload":
        return _post_library_upload(handler, app, size)

    limit = 32 * 1024 * 1024 if handler.path in ("/workflow/import", "/workflow/attach", "/workflow/inspect", "/story/parse", "/story/commit", "/save") else 8 * 1024 * 1024
    if not 0 < size <= limit:
        raise ValueError("Request is empty or exceeds the upload limit.")
    blob = handler.rfile.read(size)
    if len(blob) != size:
        raise ValueError("Upload incomplete.")
    filename = unquote(handler.headers.get("X-Filename", ""))
    binary = handler.path in ("/workflow/import", "/workflow/attach") or (handler.path == "/story/parse" and filename)
    obj = {} if binary else json.loads(blob)

    if handler.path == "/workspace-session":
        return handler.send(app.open_workspace_session(obj.get("mode")))
    if handler.path == "/studio/restart":
        return _post_restart(handler, app)
    if handler.path == "/connection":
        return _post_vast_connection(handler, app, obj)
    if handler.path == "/workflow-manager":
        return handler.send(app.workflow_manager.action(obj))
    if handler.path.startswith("/workflow/") or handler.path == "/workflow":
        return _post_workflow(handler, app, obj, blob if binary else None, filename)
    if handler.path in ("/direct-production", "/production", "/start", "/drive/connect"):
        return _post_production(handler, app, obj)
    if handler.path == "/editor":
        return _post_editor(handler, app, obj)
    if handler.path in ("/migrate", "/save", "/library/remove") or handler.path.startswith(("/project/", "/story/", "/shots/", "/characters/")):
        return _post_story(handler, app, obj, blob if binary else None, filename)
    return handler.send({}, status=404)


def _post_plugins(handler, size):
    if not 0 < size <= 65536:
        return handler.send({"ok": False, "error": {"code": "rejection", "message": "Invalid plugin request size."}}, status=400)
    from plugin_host import handle
    try:
        request = json.loads(handler.rfile.read(size))
    except (ValueError, UnicodeError):
        return handler.send({"ok": False, "error": {"code": "rejection", "message": "Invalid JSON."}}, status=400)
    return handler.send(handle(request))


def _post_library_upload(handler, app, size):
    if not 0 < size <= 64 * 1024 * 1024:
        raise ValueError("Choose a nonempty file up to 64 MB.")
    blob = handler.rfile.read(size)
    if len(blob) != size:
        raise ValueError("Upload incomplete. Try again.")
    name = app.import_asset(unquote(handler.headers.get("X-Filename", "")), blob)
    return handler.send({"name": name})


def _post_restart(handler, app):
    with app.LOCK, app.editor.lock, app.freecut.lock:
        if app.running or app.production_busy() or app.workflow_manager.busy() or app.editor.active or any(
            job.get("state") == "working" for job in app.freecut.jobs.values()
        ):
            raise ValueError("Finish or pause active work before restarting Studio.")
        app.restart_requested.set()
    try:
        return handler.send({"restarting": True})
    finally:
        threading.Thread(target=handler.server.shutdown, daemon=True).start()


def _post_vast_connection(handler, app, obj):
    action = obj.get("action")
    connection = app.controller.connection
    if action == "key":
        try:
            vast_credentials.api_key(obj.get("key"))
        except Exception:
            raise ValueError("Windows credential storage failed")
        action = "refresh"
    if action == "hf":
        vast_credentials.hf_token(obj.get("token"))
        return handler.send({"ok": True})
    if action == "instances":
        return handler.send(connection.discover_instances())
    if action == "refresh":
        with app.LOCK:
            return handler.send(connection.refresh(keep_active=app.running or app.production_busy() or app.workflow_manager.busy()))
    if action == "connect":
        return handler.send(connection.connect(obj.get("instance"), obj.get("auto", False)))
    if action == "connect-ssh":
        return handler.send(connection.connect_ssh(obj.get("ssh"), obj.get("auto", False)))
    if action == "reconnect":
        with app.LOCK:
            return handler.send(connection.reconnect())
    if action == "disconnect":
        return handler.send(connection.disconnect())
    if action == "command":
        return handler.send({"output": connection.command(obj.get("command"))})
    raise ValueError("Unknown connection action")


def _post_workflow(handler, app, obj, raw, filename):
    with app.LOCK:
        if handler.path == "/workflow":
            result = app.actions.workflow("import", {}, json.dumps(obj["graph"]).encode(), str(obj["name"]))
            return handler.send(dict(result, name=result["profile"]["name"]))
        if handler.path == "/workflow/attach":
            obj["version"] = handler.headers.get("X-Workflow-Version")
        return handler.send(app.actions.workflow(handler.path.rsplit("/", 1)[1], obj, raw, filename))


def _post_story(handler, app, obj, raw, filename):
    with app.LOCK:
        if handler.path == "/save":
            return handler.send(app.actions.general_save(obj))
        if handler.path == "/library/remove":
            name = obj["name"]
            db = app.project()
            app.actions.check(db, obj)
            if name not in app.files_snapshot():
                raise ValueError("File not found in the library.")
            if name not in db.setdefault("removedAssets", []):
                db["removedAssets"].append(name)
            app.clean_assets(db)
            for character in db.get("characters", []):
                for field in ("images", "voices"):
                    character[field] = [item for item in character.get(field, []) if item != name]
            app.actions.commit(db)
            return handler.send(dict(project=db, assets=app.active_assets(db)))
        return handler.send(app.actions.act(handler.path, obj, raw, filename))


def _post_editor(handler, app, obj):
    if obj.get("action") in ("freecut-prepare", "freecut-status"):
        return handler.send(app.freecut.action(obj))
    return handler.send(app.editor.action(obj))


def _post_production(handler, app, obj):
    if handler.path == "/drive/connect":
        with app.LOCK:
            app.drive_pending, url = drive_archive.authorization(obj["client"], f"http://127.0.0.1:{app.PORT}/drive/callback")
        return handler.send({"url": url})
    if handler.path == "/direct-production":
        with app.LOCK:
            if obj.get("action") == "logs":
                eid, sid, version = obj.get("episode"), obj.get("shot"), obj.get("version")
                if not valid_id(eid) or not valid_id(sid) or not re.fullmatch(r"[0-9]{1,22}", str(version)):
                    raise ValueError("Select a recorded shot attempt.")
                folder = app.DATA / eid / sid / version
                job = production.read(folder / "job.json", {})
                if not job:
                    raise ValueError("Select a recorded shot attempt.")
                hist = production.read(folder / "history.json", {})
                rows = hist.get("status", {}).get("messages", [])
                messages = [
                    dict(event=name, node=meta.get("node_id"), nodeType=meta.get("node_type"),
                         error=redact(meta.get("exception_message", ""))[:900])
                    for name, meta in rows[-15:] if isinstance(meta, dict)
                ]
                return handler.send(dict(promptId=job.get("ticket"), status=job["status"],
                                         error=redact(job.get("error", "")), messages=messages))
            return handler.send(app.direct_session.control(obj))
    if handler.path == "/production":
        with app.LOCK:
            if app.running:
                raise ValueError("The manual session is active")
            if app.workflow_manager.busy() and obj.get("action") in ("start", "resume", "retry"):
                raise ValueError("Wait until the Workflow installation finishes.")
            if not app.active_workflow and obj.get("action") == "start" and not obj.get("archive"):
                raise ValueError(app.workflow_error)
            if (obj.get("action") == "start" and not obj.get("archive") and app.active_workflow
                    and app.active_workflow.get("format") == "ComfyUI UI JSON"):
                raise ValueError("Start this workflow from Direct ComfyUI submission so AXDAR can verify the original graph first.")
            return handler.send(app.controller.action(obj))
    # /start (Direct submission)
    with app.LOCK:
        if not app.active_workflow:
            raise ValueError(app.workflow_error)
        if app.production_busy():
            raise ValueError("Finish or pause the active production first.")
        if app.workflow_manager.busy():
            raise ValueError("This Vast instance has an active workflow scan or installation. Open Installation Monitor to check it.")
        if app.running:
            raise ValueError("الجلسة تعمل حالياً")
        connected = app.controller.connection.snapshot().get("url")
        target = app.controller.connection.target() if connected else None
        url = (connected or obj.get("url", "")).strip().rstrip("/")
        if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname:
            raise ValueError("أدخل رابط ComfyUI")
        minutes = int(obj.get("minutes", 60))
        if not 2 <= minutes <= 1440:
            raise ValueError("المدة من دقيقتين إلى ١٤٤٠ دقيقة")
        db = app.project()
        ep = next(e for e in db["episodes"] if e["id"] == obj["episode"])
        jobs, skipped = app.actions.approved_jobs(db, ep)
        retry = obj.get("retry")
        if retry:
            if not valid_id(retry):
                raise ValueError("Select a failed shot.")
            jobs = [row for row in jobs if row[1]["id"] == retry]
            past = sorted((app.DATA / ep["id"] / retry).glob("*/job.json"), reverse=True)
            latest = production.read(past[0], {}) if past else {}
            if not jobs or latest.get("status") != "error" or latest.get("revision") != jobs[0][1]["revision"]:
                raise ValueError("Retry is only available for a failed shot of the current revision.")
        if not jobs:
            raise ValueError(next((reason for reasons in skipped.values() for reason in reasons), "Approve at least one ready shot."))
        end = time.time() + 60 * minutes
        view = dict(end=end, episode=ep["id"], submitted=len(jobs), blocked=skipped,
                    phase="Preparing ComfyUI", completed=0, failed=0)
        app.direct_session.start(view, jobs, app.active_workflow, target, url, retry=bool(retry))
        app.run_direct(url, app.active_workflow, target)
        return handler.send({"ok": True, "submitted": len(jobs), "blocked": skipped})
