"""Worker orchestration runtime: status index cache, canonical shot states, and take naming."""
import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

import worker_config
from workflow_install_remote import redact

CANONICAL_STATES = ("Pending", "Generating", "Completed", "Failed", "Skipped")
_ACTIVE_JOB_STATES = frozenset({"preparing", "submitting", "queued", "waiting"})
_FAILED_JOB_STATES = frozenset({"error", "uncertain"})


def display_filename_for_take(shot_code, take_number):
    num = int(take_number) if isinstance(take_number, int) and take_number > 0 else 1
    code = str(shot_code or "").strip()
    return f"{code}_T{num:02d}.mp4" if code else f"take_{num:02d}.mp4"


def canonical_shot_state(shot, latest_job=None):
    """Map a shot and its latest take record to one of the 5 canonical Worker states."""
    job = latest_job or {}
    status = str(job.get("status") or "").strip().lower()
    if status in _ACTIVE_JOB_STATES:
        return "Generating"
    if shot.get("skipped"):
        return "Skipped"
    if status == "done" and (job.get("video") or job.get("has_video", True)):
        return "Completed"
    if status in _FAILED_JOB_STATES:
        return "Failed"
    return "Pending"


class StatusIndexCache:
    """In-memory mtime/size-keyed cache for episodes/*/*/*/job.json reads on Windows."""

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self._lock = threading.RLock()
        self._entries = {}

    def invalidate(self):
        with self._lock:
            self._entries.clear()

    def list_results(self, data_dir=None):
        root = Path(data_dir) if data_dir is not None else self.data_dir
        paths = sorted(root.glob("*/*/*/job.json"))
        active_keys = set()
        results = []
        with self._lock:
            for p in paths:
                key = str(p)
                active_keys.add(key)
                try:
                    st = p.stat()
                except OSError:
                    self._entries.pop(key, None)
                    continue
                video = (p.parent / "video.mp4").exists()
                cached = self._entries.get(key)
                if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size and cached[2] == video:
                    results.append(dict(cached[3]))
                    continue
                try:
                    j = json.loads(p.read_text("utf-8"))
                except (OSError, ValueError):
                    continue
                relative = p.parent.relative_to(root).as_posix()
                take_num = j.get("take_number")
                shot_code = j.get("shot_code") or ""
                display = j.get("display_filename") or (
                    display_filename_for_take(shot_code, take_num) if (shot_code or take_num) else ""
                )
                row = dict(
                    episode=p.parents[2].name,
                    id=j["shot"],
                    version=p.parent.name,
                    status=j["status"],
                    error=redact(j.get("error", "")),
                    revision=j["revision"],
                    takeId=j.get("take_id"),
                    takeNumber=take_num,
                    shotCode=shot_code,
                    displayFilename=display,
                    promptId=j.get("ticket"),
                    started=j.get("started"),
                    workflowVersion=j.get("workflow_version"),
                    duration=j.get("duration"),
                    quality=j.get("quality"),
                    renderSeconds=j.get("renderSeconds"),
                    seed=j.get("seed"),
                    video="/video/" + relative if video else "",
                    artifactPending=bool(j.get("artifact_pending")),
                )
                self._entries[key] = (st.st_mtime_ns, st.st_size, video, row)
                results.append(dict(row))
            for stale in list(self._entries):
                if stale not in active_keys:
                    self._entries.pop(stale, None)
        return results


def take_download_name(data_dir, eid, sid, version, db=None):
    """Resolve human-friendly EP01S04_T01.mp4 filename for a stored take."""
    folder = Path(data_dir) / eid / sid / version
    job_path = folder / "job.json"
    job = {}
    if job_path.is_file():
        try:
            job = json.loads(job_path.read_text("utf-8"))
        except (OSError, ValueError):
            job = {}
    if job.get("display_filename"):
        return str(job["display_filename"])
    shot_code = str(job.get("shot_code") or "")
    take_number = job.get("take_number")
    if db and (not shot_code or not take_number):
        ep = next((e for e in db.get("episodes", []) if e.get("id") == eid), None)
        shot = next((s for s in (ep or {}).get("shots", []) if s.get("id") == sid), None)
        if shot:
            shot_code = shot_code or str(shot.get("shot_code") or "")
            if not take_number:
                for idx, t in enumerate(shot.get("takes", []), 1):
                    if t.get("version") == version:
                        take_number = t.get("take_number") or idx
                        break
    if not take_number:
        siblings = sorted((Path(data_dir) / eid / sid).glob("*/job.json"))
        for idx, p in enumerate(siblings, 1):
            if p.parent.name == version:
                take_number = idx
                break
    return display_filename_for_take(shot_code, take_number or 1)


def build_shot_worker_overrides(wf, settings, detected, *, eid, shot, take_number=1):
    """Compute strict per-shot ComfyUI input overrides and take metadata from Worker session settings."""
    cfg = settings or {}
    nodes = (detected or {}).get("nodes") or {}
    overrides = {}
    meta = {}

    shot_code = str(shot.get("shot_code") or shot.get("id") or "shot").strip()
    safe_code = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in shot_code)
    meta["output_prefix"] = f"AXDAR/{eid}/{safe_code}_T{int(take_number):02d}"
    meta["apply_prefix"] = True

    if cfg.get("autoCharacterImages") is False:
        meta["disable_auto_images"] = True
    if cfg.get("autoCharacterAudio") is False:
        meta["disable_auto_audio"] = True

    # 1) Seed control (Randomize per Take vs Fixed Seed)
    seed_node = nodes.get("seed")
    if seed_node and seed_node.get("id") and not seed_node.get("linked"):
        mode = cfg.get("seedMode", "random")
        if mode == "random":
            chosen_seed = secrets.randbelow(2**53 - 1) + 1
            overrides[seed_node["id"]] = chosen_seed
            meta["seed"] = chosen_seed
        else:
            fixed = cfg.get("fixedSeed")
            if isinstance(fixed, int) and fixed >= 0:
                overrides[seed_node["id"]] = fixed
                meta["seed"] = fixed
            elif seed_node.get("default") is not None:
                meta["seed"] = seed_node["default"]

    # 2) Resolution & Aspect Ratio (Selector mode vs Numeric width/height mode)
    res = nodes.get("resolution") or {}
    res_mode = res.get("mode", "none")
    preset = cfg.get("resolutionPreset", "workflow_default")
    aspect = str(cfg.get("aspectRatio") or "").strip()
    multiple = int(res.get("multiple") or 32)
    if res_mode == "selector":
        applied = {"mode": "selector", "multiple": multiple}
        aspect_entry = res.get("aspectRatio")
        mp_entry = res.get("megapixels")
        if aspect and aspect_entry and aspect_entry.get("id"):
            matched = worker_config.match_aspect_combo(aspect, res.get("aspectOptions") or [])
            chosen_aspect = matched or aspect
            overrides[aspect_entry["id"]] = chosen_aspect
            applied["aspectRatio"] = chosen_aspect
        if preset != "workflow_default" and mp_entry and mp_entry.get("id"):
            mp_val = worker_config.resolve_megapixels(
                preset,
                aspect or str(res.get("defaultAspectRatio") or "16:9"),
                multiple=multiple,
                custom_megapixels=cfg.get("customMegapixels"),
            )
            if mp_val is not None:
                overrides[mp_entry["id"]] = mp_val
                applied["megapixels"] = mp_val
        if "aspectRatio" in applied or "megapixels" in applied:
            meta["applied_resolution"] = applied
    elif res_mode == "numeric":
        w_entry = res.get("width")
        h_entry = res.get("height")
        if w_entry and h_entry and (preset != "workflow_default" or aspect):
            eff_preset = preset if preset != "workflow_default" else "720p"
            dims = worker_config.resolve_dimensions(
                eff_preset,
                aspect or f"{res.get('defaultWidth') or 16}:{res.get('defaultHeight') or 9}",
                multiple=multiple,
                custom_width=cfg.get("customWidth"),
                custom_height=cfg.get("customHeight"),
            )
            if dims:
                w, h = dims
                overrides[w_entry["id"]] = w
                overrides[h_entry["id"]] = h
                meta["applied_resolution"] = {"mode": "numeric", "width": w, "height": h, "multiple": multiple}

    # 3) FPS (Frames per Second) and direct Frame Count override
    fps_entry = nodes.get("fps")
    frames_entry = nodes.get("frames")
    base_fps = (fps_entry or {}).get("default") or (wf.get("mapping") or {}).get("fps")
    eff_fps = base_fps
    if cfg.get("fpsOverride") is not None and fps_entry and fps_entry.get("id") and not fps_entry.get("linked"):
        eff_fps = cfg["fpsOverride"]
        overrides[fps_entry["id"]] = eff_fps
        meta["applied_fps"] = eff_fps

    eff_frames = None
    if cfg.get("framesOverride") is not None and frames_entry and frames_entry.get("id") and frames_entry.get("editable"):
        eff_frames = int(cfg["framesOverride"])
        overrides[frames_entry["id"]] = eff_frames
        meta["applied_frames"] = eff_frames

    # Recompute expected playback_seconds for take_quality/inspect_video when FPS or direct frames change
    if eff_frames is not None and eff_fps and float(eff_fps) > 0:
        meta["playback_seconds"] = eff_frames / float(eff_fps)
    elif meta.get("applied_fps") and wf.get("version"):
        from workflow_profiles import timing
        base_timing = timing(wf, shot["seconds"])
        if base_timing.get("frames") and float(eff_fps) > 0:
            meta["playback_seconds"] = base_timing["frames"] / float(eff_fps)

    if overrides:
        meta["worker_overrides"] = overrides
    return meta


_SYSTEM_ERROR_PATTERNS = (
    ("cuda_oom", re.compile(r"cuda\s+out\s+of\s+memory|outofmemoryerror|cublas_status_alloc_failed|hip\s+out\s+of\s+memory", re.I),
     "GPU ran out of VRAM (CUDA OOM). Lower Resolution / Megapixels or duration before resuming."),
    ("missing_model", re.compile(r"checkpoint\s+not\s+found|file\s*not\s*found.*(?:\.safetensors|\.ckpt|\.pt|\.pth|\.bin|\.gguf)|does\s+not\s+match\s+comfyui'?s\s+registered\s+choices|missing\s+workflow\s+dependencies|model\s+.*not\s+found", re.I),
     "Required model weight is missing or not registered in ComfyUI."),
    ("missing_node", re.compile(r"missing\s+package\s+node\s+classes|class_type.*does\s+not\s+exist|cannot\s+execute\s+because\s+a\s+node\s+is\s+missing|modulenotfounderror|importerror", re.I),
     "Required ComfyUI custom node or Python dependency is missing on the server."),
    ("environment_drift", re.compile(r"pre-production\s+environment\s+check\s+failed|fingerprint\s+changed|install\s+and\s+verify\s+this\s+workflow\s+package", re.I),
     "Remote ComfyUI environment does not match the verified Workflow Package."),
    ("workflow_integrity", re.compile(r"strict\s+workflow\s+integrity\s+violation|worker\s+override\s+references\s+missing|cannot\s+overwrite\s+wired\s+link", re.I),
     "Workflow graph integrity guard blocked an invalid node binding."),
    ("comfy_restarted", re.compile(r"comfyui\s+no\s+longer\s+lists\s+this\s+ticket|prompt\s+has\s+no\s+completed\s+execution\s+history", re.I),
     "ComfyUI restarted or cleared its queue/history while the prompt was in flight."),
)

_CONNECTION_ERROR_PATTERNS = (
    ("connection_lost", re.compile(r"connection\s+(?:lost|unavailable|reset|refused|closed)|timed\s+out|timeouterror|urlerror|ssh\s+tunnel|waiting\s+for\s+connection|reconnecting|http\s*(?:502|503|504)", re.I),
     "Transient network or SSH tunnel interruption; reconcile before resubmitting."),
)


def classify_error(message, *, history=None, comfy_log="", remote_log=""):
    """Classify an execution error into system_wide, connection, or shot_specific without auto-mutating code/models."""
    parts = [str(message or "")]
    if isinstance(history, dict):
        for name, meta in (history.get("status") or {}).get("messages", []):
            if name == "execution_error" and isinstance(meta, dict):
                parts.append(str(meta.get("node_type") or ""))
                parts.append(str(meta.get("exception_type") or ""))
                parts.append(str(meta.get("exception_message") or ""))
    combined = "\n".join(p for p in parts if p).strip()
    evidence_pool = "\n".join(x for x in (combined, str(comfy_log or "")[-2000:], str(remote_log or "")[-1000:]) if x)

    for code, pattern, summary in _SYSTEM_ERROR_PATTERNS:
        m = pattern.search(evidence_pool)
        if m:
            return {
                "category": "system_wide",
                "code": code,
                "summary": summary,
                "shouldHalt": True,
                "evidence": redact(combined or m.group(0))[:500],
            }
    for code, pattern, summary in _CONNECTION_ERROR_PATTERNS:
        m = pattern.search(combined)
        if m:
            return {
                "category": "connection",
                "code": code,
                "summary": summary,
                "shouldHalt": False,
                "evidence": redact(combined)[:500],
            }
    return {
        "category": "shot_specific",
        "code": "shot_error",
        "summary": redact(combined)[:300] or "Shot execution failed.",
        "shouldHalt": False,
        "evidence": redact(combined)[:500],
    }


class WorkerLogStore:
    """Thread-safe local AXDAR Studio / Worker event log with redacted persistence."""

    def __init__(self, log_path=None, max_lines=400):
        self.log_path = Path(log_path) if log_path else None
        self.max_lines = max(50, int(max_lines))
        self._lock = threading.RLock()
        self._lines = []
        if self.log_path and self.log_path.is_file():
            try:
                existing = self.log_path.read_text("utf-8", errors="replace").splitlines()
                self._lines = existing[-self.max_lines:]
            except OSError:
                self._lines = []

    def record(self, level, message):
        clean = redact(str(message or "")).replace("\r", " ").strip()
        if not clean:
            return
        line = f"[{time.strftime('%H:%M:%S')}] [{str(level).upper()}] {clean}"
        with self._lock:
            self._lines.append(line)
            if len(self._lines) > self.max_lines:
                self._lines = self._lines[-self.max_lines:]
            if self.log_path is not None:
                try:
                    self.log_path.parent.mkdir(parents=True, exist_ok=True)
                    with self.log_path.open("a", encoding="utf-8") as fh:
                        fh.write(line + "\n")
                except OSError:
                    pass

    def tail(self, limit=200):
        with self._lock:
            return list(self._lines[-max(1, int(limit)):])


def collect_three_logs(ctx, *, include_remote_ssh=False):
    """Aggregate ComfyUI Log, Remote Host (Vast/SSH) Log, and AXDAR Studio Log + latest error classification."""
    comfy_lines = []
    remote_lines = []
    studio_lines = []

    # 1) AXDAR Studio / Worker log
    wlogs = ctx.get("worker_logs")
    if wlogs is not None:
        studio_lines = wlogs.tail(200)

    # 2) ComfyUI log via existing /internal/logs/raw endpoint
    conn = ctx.get("connection")
    url = conn.snapshot().get("url") if conn is not None else None
    if url:
        try:
            raw = ctx["json_api"](url, "/internal/logs/raw")
            entries = raw.get("entries", []) if isinstance(raw, dict) else []
            for row in entries[-200:]:
                if isinstance(row, dict) and isinstance(row.get("m"), str):
                    ts = str(row.get("t") or "")
                    msg = redact(row["m"]).rstrip()
                    if msg:
                        comfy_lines.append(f"{ts} {msg}".strip())
        except Exception as exc:
            comfy_lines.append(f"[ComfyUI log unavailable: {redact(str(exc))}]")
    else:
        comfy_lines.append("[ComfyUI is not connected.]")

    # 3) Remote Host (Vast/SSH) log — designed with provider field for future RunPod support
    if conn is not None and hasattr(conn, "recent_logs"):
        remote_lines.extend(conn.recent_logs(120))
    if include_remote_ssh and conn is not None and ctx.get("ssh_ops") is not None:
        try:
            target = conn.ssh_target()
            out = ctx["ssh_ops"].ssh(
                target,
                "supervisorctl status 2>/dev/null; echo '---'; tail -n 60 /var/log/portal/comfyui.log /workspace/comfyui.log 2>/dev/null || true",
                timeout=10,
            ).decode("utf-8", errors="replace")
            for line in redact(out).splitlines()[-80:]:
                if line.strip():
                    remote_lines.append(line)
        except Exception as exc:
            remote_lines.append(f"[Remote SSH log check skipped: {redact(str(exc))}]")
    if not remote_lines:
        remote_lines.append("[No Vast/SSH events recorded yet.]")

    session = ctx.get("session") or {}
    classification = session.get("errorClassification")
    if not classification and session.get("error"):
        classification = classify_error(
            session["error"],
            comfy_log="\n".join(comfy_lines[-40:]),
            remote_log="\n".join(remote_lines[-40:]),
        )
    return {
        "provider": "vast",
        "comfyui": "\n".join(comfy_lines),
        "remote": "\n".join(remote_lines),
        "studio": "\n".join(studio_lines),
        "classification": classification,
    }


def compute_financial_summary(connection_snapshot, session, results, settings=None, *, now=None):
    """Compute Financial Monitor data clearly separating Actual (Vast.ai) from Estimated (Local) figures."""
    conn = connection_snapshot if isinstance(connection_snapshot, dict) else {}
    sess = session if isinstance(session, dict) else {}
    cfg = settings if isinstance(settings, dict) else {}
    now_ts = float(now if now is not None else time.time())

    # 1) Actual Vast.ai rate vs optional manual fallback
    vast_rate = conn.get("rate")
    if vast_rate is None:
        vast_rate = sess.get("rate")
    hourly_rate = None
    rate_badge = "Unavailable"
    rate_source_label = "No Vast Rate (Manual SSH)"
    if isinstance(vast_rate, (int, float)) and not isinstance(vast_rate, bool) and vast_rate >= 0:
        hourly_rate = float(vast_rate)
        rate_badge = "Actual · Vast.ai"
        rate_source_label = "Vast.ai Actual Rate · Local Cost Estimates"
    elif isinstance(cfg.get("manualHourlyRate"), (int, float)) and not isinstance(cfg.get("manualHourlyRate"), bool):
        hourly_rate = float(cfg["manualHourlyRate"])
        rate_badge = "Manual"
        rate_source_label = "Manual Rate · Local Cost Estimates"

    minute_rate = (hourly_rate / 60.0) if hourly_rate is not None else None
    uptime_mins = conn.get("uptime") if isinstance(conn.get("uptime"), (int, float)) else None

    # 2) Session elapsed time & local session rental cost estimate
    started = sess.get("started")
    finished = sess.get("finished")
    elapsed_sec = 0.0
    if isinstance(started, (int, float)) and started > 0:
        end_ref = float(finished) if isinstance(finished, (int, float)) and finished >= started else now_ts
        elapsed_sec = max(0.0, end_ref - float(started))
    current_session_cost = (elapsed_sec / 3600.0) * hourly_rate if hourly_rate is not None else None

    # 3) Shot counts & cold-start-adjusted average generation time
    total_shots = int(sess.get("submitted") or 0)
    completed_shots = int(sess.get("completed") or 0)
    failed_shots = int(sess.get("failed") or 0)
    skipped_shots = int(sess.get("skipped") or 0)
    remaining_shots = max(0, total_shots - completed_shots - failed_shots - skipped_shots)

    episode_id = sess.get("episode")
    done_rows = [
        r for r in (results or [])
        if r.get("status") == "done"
        and isinstance(r.get("renderSeconds"), (int, float)) and r["renderSeconds"] > 0
        and (not episode_id or r.get("episode") == episode_id)
        and (not started or not isinstance(r.get("started"), (int, float)) or r["started"] >= started - 5)
    ]
    done_rows.sort(key=lambda r: float(r.get("started") or 0))
    durations = [float(r["renderSeconds"]) for r in done_rows]

    cold_start_only = False
    if not durations:
        avg_shot_sec = None
    elif len(durations) == 1:
        avg_shot_sec = durations[0]
        cold_start_only = True
    else:
        # Exclude Shot #1 cold-start (model loading/warmup) once 2+ shots have completed
        steady = durations[1:]
        avg_shot_sec = sum(steady) / len(steady)

    est_remaining_sec = (remaining_shots * avg_shot_sec) if avg_shot_sec is not None else None
    est_remaining_cost = (
        (est_remaining_sec / 60.0) * minute_rate
        if (est_remaining_sec is not None and minute_rate is not None)
        else None
    )
    if current_session_cost is not None and est_remaining_cost is not None:
        est_total_cost = current_session_cost + est_remaining_cost
    elif total_shots > 0 and avg_shot_sec is not None and minute_rate is not None:
        est_total_cost = total_shots * (avg_shot_sec / 60.0) * minute_rate
    else:
        est_total_cost = current_session_cost

    return {
        "hourlyRate": round(hourly_rate, 4) if hourly_rate is not None else None,
        "minuteRate": round(minute_rate, 5) if minute_rate is not None else None,
        "rateBadge": rate_badge,
        "rateSourceLabel": rate_source_label,
        "instanceUptimeMins": round(float(uptime_mins), 1) if uptime_mins is not None else None,
        "sessionElapsedSeconds": round(elapsed_sec, 1),
        "currentSessionCost": round(current_session_cost, 4) if current_session_cost is not None else None,
        "totalShots": total_shots,
        "completedShots": completed_shots,
        "failedShots": failed_shots,
        "skippedShots": skipped_shots,
        "remainingShots": remaining_shots,
        "avgShotSeconds": round(avg_shot_sec, 2) if avg_shot_sec is not None else None,
        "coldStartOnly": cold_start_only,
        "estimatedRemainingSeconds": round(est_remaining_sec, 1) if est_remaining_sec is not None else None,
        "estimatedRemainingCost": round(est_remaining_cost, 4) if est_remaining_cost is not None else None,
        "estimatedTotalCost": round(est_total_cost, 4) if est_total_cost is not None else None,
        "isEstimate": True,
    }


def safe_folder_name(title, fallback="AXDAR_Episode"):
    """Sanitize a project or episode title into a safe Windows/POSIX folder name."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(title or "")).strip(" .")
    cleaned = cleaned[:80].strip(" .")
    if not cleaned:
        return fallback
    if re.match(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)", cleaned, re.I):
        cleaned = "_" + cleaned
    return cleaned


def _link_or_copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def export_episode_to_downloads(data_dir, db, episode_id, *, downloads_root=None, all_takes=False):
    """Assemble completed verified takes with EP01S04_T01.mp4 names into Downloads and emit Take Delivery manifest."""
    import production
    import story_engine

    root = Path(data_dir)
    ep = next((e for e in (db or {}).get("episodes", []) if e.get("id") == episode_id), None)
    if not ep:
        raise ValueError("Episode not found.")
    story_engine.assign_shot_codes(db, ep)

    dl_base = Path(downloads_root) if downloads_root is not None else (Path.home() / "Downloads")
    dest = dl_base / "AXDAR_Exports" / safe_folder_name(ep.get("title"), f"EP{ep.get('episode_number', 1):02d}")
    dest.mkdir(parents=True, exist_ok=True)

    exported_items = []
    for idx, shot in enumerate(ep.get("shots", []), 1):
        sid = shot["id"]
        shot_code = shot.get("shot_code") or story_engine.format_shot_code(ep.get("episode_number", 1), idx)
        shot_dir = root / episode_id / sid
        if not shot_dir.is_dir():
            continue
        completed = []
        for pos, job_path in enumerate(sorted(shot_dir.glob("*/job.json")), 1):
            video_path = job_path.parent / "video.mp4"
            if not video_path.is_file():
                continue
            job = production.read(job_path, {})
            if not isinstance(job, dict) or job.get("status") != "done":
                continue
            tnum = job.get("take_number") if isinstance(job.get("take_number"), int) and job["take_number"] > 0 else pos
            fname = job.get("display_filename") or display_filename_for_take(shot_code, tnum)
            completed.append({
                "shotId": sid,
                "shotCode": shot_code,
                "takeId": job.get("take_id") or "",
                "takeNumber": tnum,
                "version": job_path.parent.name,
                "filename": fname,
                "sourceVideo": video_path,
                "duration": job.get("duration"),
                "seed": job.get("seed"),
            })
        if not completed:
            continue
        selected_id = shot.get("selected_take_id") or ""
        preferred = next((c for c in completed if selected_id and c["takeId"] == selected_id), completed[-1])
        chosen_list = completed if all_takes else [preferred]
        for item in chosen_list:
            target = dest / item["filename"]
            _link_or_copy(item["sourceVideo"], target)
            exported_items.append({
                "shotId": item["shotId"],
                "shotCode": item["shotCode"],
                "takeId": item["takeId"],
                "takeNumber": item["takeNumber"],
                "filename": item["filename"],
                "exportedPath": str(target),
                "sourceVideo": str(item["sourceVideo"]),
                "duration": item["duration"],
                "seed": item["seed"],
                "selected": item is preferred,
            })

    manifest = {
        "schemaVersion": 1,
        "episodeId": episode_id,
        "episodeTitle": ep.get("title") or "",
        "exportedAt": time.time(),
        "exportedCount": len(exported_items),
        "items": exported_items,
    }
    production.atomic(dest / "manifest.json", manifest)
    return {
        "ok": True,
        "destination": str(dest),
        "exportedCount": len(exported_items),
        "items": exported_items,
    }
