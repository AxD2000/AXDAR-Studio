"""Worker settings persistence, mandatory safety invariants, resolution presets, and sidecar mapping overrides."""
import copy
import math
import re
import threading
from pathlib import Path

import production

MANDATORY_INVARIANTS = {
    "skipUnusedCharacterRefs": True,
    "reconcileBeforeResubmit": True,
    "strictGraphIntegrity": True,
    "verifyOutputIntegrity": True,
}

DEFAULT_SETTINGS = {
    "autoNodeDetection": True,
    "autoCharacterImages": True,
    "autoCharacterAudio": True,
    "autoReconnect": True,
    "continuousExecution": True,
    "runLimit": 0,  # 0 = all approved shots; N > 0 = pause after N shots (Test Batch)
    "autoRetry": False,
    "maxRetries": 1,
    "consecutiveFailureLimit": 2,
    "seedMode": "random",  # "random" | "fixed"
    "fixedSeed": None,
    "resolutionPreset": "workflow_default",  # workflow_default | 360p | 480p | 540p | 720p | 1080p | custom
    "aspectRatio": "",  # "" = workflow default; e.g. "16:9", "9:16", "1:1", "4:3", "3:4", "21:9"
    "customWidth": None,
    "customHeight": None,
    "customMegapixels": None,
    "fpsOverride": None,
    "framesOverride": None,
    "sessionMinutes": 0,  # 0 = no fixed timeout; else 2..1440
    "financialTracking": True,
    "manualHourlyRate": None,
    "autoExportDownloads": True,
}

YOUTUBE_PRESETS = {
    "360p": {"label": "360p (Fast Test)", "short": 360, "megapixels": 0.23},
    "480p": {"label": "480p (SD)", "short": 480, "megapixels": 0.41},
    "540p": {"label": "540p (0.5 MP Base)", "short": 540, "megapixels": 0.52},
    "720p": {"label": "720p (HD)", "short": 720, "megapixels": 0.92},
    "1080p": {"label": "1080p (Full HD)", "short": 1080, "megapixels": 2.07},
}

ASPECT_RATIOS = {
    "16:9": (16, 9),
    "9:16": (9, 16),
    "1:1": (1, 1),
    "4:3": (4, 3),
    "3:4": (3, 4),
    "21:9": (21, 9),
}

FLOW_LIVE_SETTINGS = frozenset({
    "autoReconnect", "continuousExecution", "autoRetry",
    "maxRetries", "consecutiveFailureLimit", "financialTracking", "manualHourlyRate",
})


def snap_multiple(value, multiple=32):
    m = max(1, int(multiple or 32))
    snapped = int(round(float(value) / m) * m)
    return max(m, snapped)


def parse_aspect_ratio(value):
    text = str(value or "").strip()
    if not text:
        return None
    if text in ASPECT_RATIOS:
        return ASPECT_RATIOS[text]
    m = re.search(r"(\d+(?:\.\d+)?)\s*[:x×/]\s*(\d+(?:\.\d+)?)", text)
    if m:
        w, h = float(m.group(1)), float(m.group(2))
        if w > 0 and h > 0:
            return (w, h)
    return None


def resolve_dimensions(preset, aspect_ratio, *, multiple=32, custom_width=None, custom_height=None):
    """Compute exact (width, height) snapped to multiple for YouTube presets or custom dimensions."""
    m = max(1, int(multiple or 32))
    if preset == "custom":
        if custom_width and custom_height:
            return snap_multiple(custom_width, m), snap_multiple(custom_height, m)
        return None
    spec = YOUTUBE_PRESETS.get(preset)
    if not spec:
        return None
    ratio = parse_aspect_ratio(aspect_ratio) or (16, 9)
    rw, rh = ratio
    short = float(spec["short"])
    if rw >= rh:
        height = short
        width = short * (rw / rh)
    else:
        width = short
        height = short * (rh / rw)
    return snap_multiple(width, m), snap_multiple(height, m)


def resolve_megapixels(preset, aspect_ratio="", *, multiple=32, custom_megapixels=None):
    """Resolve megapixels for ResolutionSelector (multiple=32) from YouTube preset or custom value."""
    if preset == "custom":
        if custom_megapixels is not None:
            val = float(custom_megapixels)
            if math.isfinite(val) and 0.05 <= val <= 16.0:
                return round(val, 3)
        return None
    dims = resolve_dimensions(preset, aspect_ratio or "16:9", multiple=multiple)
    if dims:
        w, h = dims
        return round((w * h) / 1_000_000.0, 2)
    spec = YOUTUBE_PRESETS.get(preset)
    return spec["megapixels"] if spec else None


def match_aspect_combo(aspect_ratio, options):
    """Match a short aspect ratio like '16:9' against a ComfyUI COMBO list like ['9:16 (Portrait ...)', '16:9 (Landscape ...)']."""
    if not aspect_ratio or not isinstance(options, list):
        return None
    target = str(aspect_ratio).strip()
    if target in options:
        return target
    parsed = parse_aspect_ratio(target)
    for opt in options:
        text = str(opt)
        if text.lower().startswith(target.lower()):
            return opt
        if parsed and parse_aspect_ratio(text) == parsed:
            return opt
    return None


def validate_settings(raw, current=None):
    """Validate and merge incoming Worker settings while keeping mandatory invariants locked."""
    if not isinstance(raw, dict):
        raise ValueError("Worker settings must be a JSON object.")
    merged = copy.deepcopy(current or DEFAULT_SETTINGS)
    for k, default_val in DEFAULT_SETTINGS.items():
        merged.setdefault(k, default_val)

    for key, val in raw.items():
        if key in MANDATORY_INVARIANTS:
            if val is not True:
                raise ValueError(f"Setting '{key}' is mandatory for production safety and cannot be disabled.")
            continue
        if key not in DEFAULT_SETTINGS:
            raise ValueError(f"Unknown Worker setting '{key}'.")
        if key in (
            "autoNodeDetection", "autoCharacterImages", "autoCharacterAudio",
            "autoReconnect", "continuousExecution", "autoRetry",
            "financialTracking", "autoExportDownloads",
        ):
            if not isinstance(val, bool):
                raise ValueError(f"Worker setting '{key}' must be a boolean.")
            merged[key] = val
        elif key == "runLimit":
            if isinstance(val, bool) or not isinstance(val, int) or not 0 <= val <= 10000:
                raise ValueError("runLimit must be an integer between 0 and 10000.")
            merged[key] = val
        elif key == "maxRetries":
            if isinstance(val, bool) or not isinstance(val, int) or not 0 <= val <= 5:
                raise ValueError("maxRetries must be an integer between 0 and 5.")
            merged[key] = val
        elif key == "consecutiveFailureLimit":
            if isinstance(val, bool) or not isinstance(val, int) or not 1 <= val <= 10:
                raise ValueError("consecutiveFailureLimit must be an integer between 1 and 10.")
            merged[key] = val
        elif key == "seedMode":
            if val not in ("random", "fixed"):
                raise ValueError("seedMode must be 'random' or 'fixed'.")
            merged[key] = val
        elif key == "fixedSeed":
            if val is None or val == "":
                merged[key] = None
            elif isinstance(val, bool) or not isinstance(val, int) or not 0 <= val <= (2**63 - 1):
                raise ValueError("fixedSeed must be a non-negative integer.")
            else:
                merged[key] = val
        elif key == "resolutionPreset":
            allowed = {"workflow_default", "custom", *YOUTUBE_PRESETS.keys()}
            if val not in allowed:
                raise ValueError(f"Unsupported resolutionPreset '{val}'.")
            merged[key] = val
        elif key == "aspectRatio":
            if not isinstance(val, str) or len(val) > 80:
                raise ValueError("aspectRatio must be a short string.")
            merged[key] = val.strip()
        elif key in ("customWidth", "customHeight"):
            if val is None or val == "":
                merged[key] = None
            elif isinstance(val, bool) or not isinstance(val, int) or not 64 <= val <= 8192:
                raise ValueError(f"{key} must be an integer between 64 and 8192.")
            else:
                merged[key] = val
        elif key == "customMegapixels":
            if val is None or val == "":
                merged[key] = None
            elif isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or not 0.05 <= float(val) <= 16.0:
                raise ValueError("customMegapixels must be a number between 0.05 and 16.0.")
            else:
                merged[key] = round(float(val), 3)
        elif key == "fpsOverride":
            if val is None or val == "":
                merged[key] = None
            elif isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or not 1 <= float(val) <= 120:
                raise ValueError("fpsOverride must be between 1 and 120 fps.")
            else:
                merged[key] = int(val) if float(val).is_integer() else float(val)
        elif key == "framesOverride":
            if val is None or val == "":
                merged[key] = None
            elif isinstance(val, bool) or not isinstance(val, int) or not 1 <= val <= 10000:
                raise ValueError("framesOverride must be an integer between 1 and 10000.")
            else:
                merged[key] = val
        elif key == "sessionMinutes":
            if isinstance(val, bool) or not isinstance(val, int) or not (val == 0 or 2 <= val <= 1440):
                raise ValueError("sessionMinutes must be 0 (unlimited) or between 2 and 1440.")
            merged[key] = val
        elif key == "manualHourlyRate":
            if val is None or val == "":
                merged[key] = None
            elif isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or not 0 <= float(val) <= 500:
                raise ValueError("manualHourlyRate must be a number between 0 and 500.")
            else:
                merged[key] = round(float(val), 4)
    return merged


class WorkerConfigStore:
    """Persists Worker settings and sidecar per-workflow mapping overrides without touching .zip packages."""

    def __init__(self, settings_path, overrides_dir):
        self.settings_path = Path(settings_path)
        self.overrides_dir = Path(overrides_dir)
        self._lock = threading.RLock()
        self._settings = self._load_settings()

    def _load_settings(self):
        raw = production.read(self.settings_path, None)
        if not isinstance(raw, dict):
            return copy.deepcopy(DEFAULT_SETTINGS)
        try:
            clean = {k: v for k, v in raw.items() if k in DEFAULT_SETTINGS}
            return validate_settings(clean, DEFAULT_SETTINGS)
        except ValueError:
            return copy.deepcopy(DEFAULT_SETTINGS)

    def get(self):
        with self._lock:
            return copy.deepcopy(self._settings)

    def update(self, changes):
        with self._lock:
            updated = validate_settings(changes, self._settings)
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            production.atomic(self.settings_path, updated)
            self._settings = updated
            return copy.deepcopy(self._settings)

    def public_state(self):
        return {
            "settings": self.get(),
            "mandatory": dict(MANDATORY_INVARIANTS),
            "presets": {
                "resolutions": [
                    {"id": "workflow_default", "label": "Workflow Default"},
                    *[{"id": k, "label": v["label"], "short": v["short"], "megapixels": v["megapixels"]} for k, v in YOUTUBE_PRESETS.items()],
                    {"id": "custom", "label": "Custom"},
                ],
                "aspectRatios": ["", *ASPECT_RATIOS.keys()],
            },
        }

    def _override_path(self, version):
        if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version):
            raise ValueError("Invalid workflow version hash.")
        return self.overrides_dir / f"{version}.json"

    def get_mapping_override(self, version):
        if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version):
            return {}
        with self._lock:
            data = production.read(self._override_path(version), {})
            return copy.deepcopy(data) if isinstance(data, dict) else {}

    def save_mapping_override(self, version, override):
        if not isinstance(override, dict):
            raise ValueError("Mapping override must be a JSON object.")
        allowed_roles = {
            "positive", "negative", "duration", "frames", "fps", "seed",
            "resolutionWidth", "resolutionHeight", "aspectRatio", "megapixels",
            "images", "audio", "output", "anchors",
        }
        unknown = set(override) - allowed_roles
        if unknown:
            raise ValueError("Unsupported mapping override field(s): " + ", ".join(sorted(unknown)))
        with self._lock:
            path = self._override_path(version)
            path.parent.mkdir(parents=True, exist_ok=True)
            production.atomic(path, override)
            return copy.deepcopy(override)

    def clear_mapping_override(self, version):
        with self._lock:
            path = self._override_path(version)
            path.unlink(missing_ok=True)
            return {}
