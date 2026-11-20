"""Minimal thread‑safe plugin registry.

State file structure
====================

The registry persists a JSON file with the following format::

    {
        "versions": {
            "plugin_id": {
                "1.0": {
                    "manifest_raw": "base64 string",
                    "code": "base64 string",
                    "digest": "sha256 hex"
                }
            }
        },
        "active": {"plugin_id": "1.0"},
        "previous": {"plugin_id": "0.9"}
    }

All public methods operate on this structure.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Dict, Tuple

from plugin_manifest import validate

# Constants
_MAX_FILE_SIZE = 64 * 1024  # 64 KiB
_NULL_BYTE = b"\0"
_ID_RE = re.compile(r"[a-z0-9]+(?:\.[a-z0-9]+)*")
_VER_RE = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6}){2}")


class Registry:
    def __init__(self, base: Path):
        self.base = Path(base).resolve()
        self.catalog = self.base / "plugins" / "catalog"
        self.state_path = self.base / "plugins" / "state.json"
        self.lock = threading.RLock()

    # ---------- internal helpers ----------

    def _load_state(self) -> Dict:
        if self.state_path.exists():
            with self.state_path.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
        else:
            data = {"versions": {}, "active": {}, "previous": {}}
        # Ensure structure
        data.setdefault("versions", {})
        data.setdefault("active", {})
        data.setdefault("previous", {})
        return data

    def _save_state(self, state: Dict) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fp:
            json.dump(state, fp, separators=(",", ":"))
        os.replace(tmp, self.state_path)

    def _read_file(self, path: Path) -> bytes:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing or symlink file: {path}")
        if path.stat().st_size > _MAX_FILE_SIZE:
            raise ValueError(f"File too large: {path}")
        return path.read_bytes()

    def _validate_catalog_entry(self, id: str, version: str) -> Tuple[bytes, bytes, dict]:
        if not isinstance(id, str) or len(id) > 80 or not _ID_RE.fullmatch(id) or not isinstance(version, str) or not _VER_RE.fullmatch(version):
            raise ValueError("Invalid id or version format")
        manifest_path = self.catalog / id / version / "manifest.json"
        script_path = self.catalog / id / version / "action.py"
        if any(path.resolve() != path for path in (manifest_path, script_path)):
            raise ValueError("Linked package paths are unsupported")
        manifest_bytes = self._read_file(manifest_path)
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"Invalid JSON in {manifest_path}: {exc}") from exc
        validate(manifest)
        if manifest.get("id") != id or manifest.get("version") != version:
            raise ValueError("Manifest id/version mismatch with path")
        script_bytes = self._read_file(script_path)
        return manifest_bytes, script_bytes, manifest

    def _compute_digest(self, manifest_bytes: bytes, script_bytes: bytes) -> str:
        h = hashlib.sha256()
        h.update(manifest_bytes)
        h.update(_NULL_BYTE)
        h.update(script_bytes)
        return h.hexdigest()

    # ---------- public API ----------

    def list(self) -> Dict:
        catalog = []
        if self.catalog.exists():
            for id_dir in self.catalog.iterdir():
                if not id_dir.is_dir() or id_dir.is_symlink():
                    continue
                for ver_dir in id_dir.iterdir():
                    if not ver_dir.is_dir() or ver_dir.is_symlink():
                        continue
                    try:
                        m_bytes, s_bytes, _ = self._validate_catalog_entry(id_dir.name, ver_dir.name)
                        digest = self._compute_digest(m_bytes, s_bytes)
                        catalog.append({"id": id_dir.name, "version": ver_dir.name, "digest": digest})
                    except Exception:
                        continue
        state = self._load_state()
        installed = []
        for plugin_id, versions in state["versions"].items():
            for version, package in versions.items():
                manifest = json.loads(base64.b64decode(package["manifest_raw"]))
                installed.append(dict(id=plugin_id, version=version, digest=package["digest"],
                                      label=manifest["label"], actions=manifest["actions"],
                                      active=state["active"].get(plugin_id) == version))
        return {"catalog": catalog, "installed": installed}

    def install(self, id: str, version: str) -> None:
        with self.lock:
            state = self._load_state()
            versions = state["versions"].setdefault(id, {})
            if version in versions:
                # ensure idempotent
                existing = versions[version]
                m_bytes, s_bytes, _ = self._validate_catalog_entry(id, version)
                digest = self._compute_digest(m_bytes, s_bytes)
                if existing["digest"] != digest:
                    raise ValueError("Same id/version with different bytes already installed")
                return
            m_bytes, s_bytes, _ = self._validate_catalog_entry(id, version)
            digest = self._compute_digest(m_bytes, s_bytes)
            versions[version] = {
                "manifest_raw": base64.b64encode(m_bytes).decode("ascii"),
                "code": base64.b64encode(s_bytes).decode("ascii"),
                "digest": digest,
            }
            self._save_state(state)

    def enable(self, id: str, version: str, digest: str) -> None:
        with self.lock:
            state = self._load_state()
            versions = state["versions"].get(id, {})
            if not versions or version not in versions:
                raise ValueError("Plugin not installed")
            if versions[version]["digest"] != digest:
                raise ValueError("Digest mismatch")
            prev_active = state["active"].get(id)
            if prev_active and prev_active != version:
                state["previous"][id] = prev_active
            state["active"][id] = version
            self._save_state(state)

    def disable(self, id: str) -> None:
        with self.lock:
            state = self._load_state()
            if id in state["active"]:
                del state["active"][id]
                self._save_state(state)

    def rollback(self, id: str) -> None:
        with self.lock:
            state = self._load_state()
            if id not in state["active"]:
                raise ValueError("No active plugin to rollback")
            prev = state["previous"].get(id)
            if not prev:
                raise ValueError("No previous version to rollback to")
            # Ensure previous exists in installed
            if prev not in state["versions"].get(id, {}):
                raise ValueError("Previous version not installed")
            state["active"][id] = prev
            state["previous"][id] = None
            self._save_state(state)

    def remove(self, id: str, version: str) -> None:
        with self.lock:
            state = self._load_state()
            if id not in state["versions"] or version not in state["versions"][id]:
                raise ValueError("Plugin not installed")
            if state["active"].get(id) == version:
                raise ValueError("Active plugin must be disabled before removal")
            del state["versions"][id][version]
            if not state["versions"][id]:
                del state["versions"][id]
            if state["previous"].get(id) == version:
                del state["previous"][id]
            self._save_state(state)

    def resolve(self, id: str, action: str = "action") -> Tuple[Dict, bytes]:
        with self.lock:
            state = self._load_state()
            active_ver = state["active"].get(id)
            if not active_ver:
                raise ValueError("No active plugin for id")
            version_data = state["versions"].get(id, {}).get(active_ver)
            if not version_data:
                raise ValueError("Active plugin data missing")
            m_bytes = base64.b64decode(version_data["manifest_raw"])  # type: ignore[arg-type]
            s_bytes = base64.b64decode(version_data["code"])  # type: ignore[arg-type]
            expected = self._compute_digest(m_bytes, s_bytes)
            if expected != version_data["digest"]:
                raise ValueError("Digest mismatch on resolve")
            manifest = json.loads(m_bytes.decode("utf-8"))
            validate(manifest)
            if not any(item["id"] == action for item in manifest["actions"]):
                raise ValueError(f"Action {action!r} not defined in manifest")
            return manifest, s_bytes

# End of file
