"""Worker orchestration runtime: status index cache, canonical shot states, and take naming."""
import json
import secrets
import threading
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
