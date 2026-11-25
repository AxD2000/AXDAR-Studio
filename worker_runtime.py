"""Worker orchestration runtime: status index cache, canonical shot states, and take naming."""
import json
import threading
from pathlib import Path

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
