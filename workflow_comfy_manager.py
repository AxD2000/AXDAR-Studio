"""Post-installation ComfyUI-Manager custom-node discovery, installation, update, and restart coordination."""
import json
import re
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import provision

STAGE_NAME = "ComfyUI-Manager sync"
PROTECTED_ADAPTER_NAMES = frozenset({"sol_attn_minimax_pr117"})
PROTECTED_GPU_PKGS = frozenset({"torch", "torchvision", "torchaudio", "triton", "xformers"})
PINNED_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)


def _normalize_repo(value):
    text = str(value or "").strip().lower().rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def _repo_basename(value):
    norm = _normalize_repo(value)
    return norm.rsplit("/", 1)[-1] if norm else ""


def _req_pkg_name(req):
    text = str(req or "").strip()
    match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", text)
    return match.group(1).lower().replace("_", "-") if match else ""


class ComfyManagerClient:
    """Thin HTTP client targeting ONLY verified ComfyUI-Manager REST API endpoints."""

    def __init__(self, base_url, *, request_fn=None, urlopen_fn=None, timeout=30):
        self.base_url = str(base_url).rstrip("/")
        self._request_fn = request_fn
        self._urlopen = urlopen_fn or urlopen
        self.timeout = timeout

    def request(self, method, path, payload=None, *, timeout=None, expect_json=True):
        url = self.base_url + path
        if self._request_fn is not None:
            return self._request_fn(method, path, payload, timeout or self.timeout, expect_json)
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        target = url if (method == "GET" and payload is None) else Request(url, data=data, headers=headers, method=method)
        try:
            with self._urlopen(target, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
                status_code = getattr(resp, "status", 200)
                if not expect_json:
                    return status_code, raw.decode("utf-8", errors="replace")
                if not raw:
                    return status_code, {}
                return status_code, json.loads(raw.decode("utf-8", errors="replace"))
        except HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")[:800] if exc.fp else str(exc)
            finally:
                exc.close()
            return exc.code, body
        except (URLError, OSError, ValueError) as exc:
            raise RuntimeError(f"ComfyUI-Manager request {method} {path} failed: {exc}") from exc

    def version(self):
        try:
            status, body = self.request("GET", "/manager/version", expect_json=False, timeout=10)
            if status == 200 and isinstance(body, str):
                text = body.strip()
                if text and not text.startswith("{") and len(text) <= 64:
                    return text
        except RuntimeError:
            pass
        return None

    def get_mappings(self, modes=("default", "cache", "local")):
        last_err = None
        for mode in modes:
            try:
                status, data = self.request("GET", "/customnode/getmappings?" + urlencode({"mode": mode}), timeout=45)
                if status == 200 and isinstance(data, dict):
                    return data
                last_err = f"HTTP {status}"
            except RuntimeError as exc:
                last_err = str(exc)
        raise RuntimeError(f"Unable to fetch ComfyUI-Manager node mappings: {last_err}")

    def get_node_list(self, modes=("default", "cache", "local")):
        last_err = None
        for mode in modes:
            try:
                status, data = self.request("GET", "/customnode/getlist?" + urlencode({"mode": mode}), timeout=45)
                if status == 200 and isinstance(data, dict):
                    packs = data.get("node_packs") if isinstance(data.get("node_packs"), dict) else data
                    return data.get("channel", "default"), packs
                last_err = f"HTTP {status}"
            except RuntimeError as exc:
                last_err = str(exc)
        raise RuntimeError(f"Unable to fetch ComfyUI-Manager node list: {last_err}")

    def get_installed(self):
        status, data = self.request("GET", "/customnode/installed", timeout=20)
        if status == 200 and isinstance(data, dict):
            return data
        return {}

    def import_fail_info(self, *, cnr_id=None, url=None):
        body = {"cnr_id": cnr_id} if cnr_id else {"url": url}
        try:
            status, data = self.request("POST", "/customnode/import_fail_info", body, timeout=15)
            if status == 200 and isinstance(data, dict):
                return data
        except RuntimeError:
            pass
        return None

    def queue_status(self):
        status, data = self.request("GET", "/manager/queue/status", timeout=15)
        if status != 200 or not isinstance(data, dict):
            raise RuntimeError(f"Failed to read ComfyUI-Manager queue status (HTTP {status}).")
        return {
            "total_count": int(data.get("total_count", 0)),
            "done_count": int(data.get("done_count", 0)),
            "in_progress_count": int(data.get("in_progress_count", 0)),
            "is_processing": bool(data.get("is_processing", False)),
        }

    def queue_install(self, pack_id, pack_info, *, channel="default", mode="cache", ui_id=None, skip_post_install=False):
        version = str(pack_info.get("version") or "latest")
        payload = {
            "id": pack_id,
            "version": version,
            "selected_version": "latest" if version != "unknown" else "unknown",
            "repository": pack_info.get("repository") or "",
            "files": list(pack_info.get("files") or ([pack_info["repository"]] if pack_info.get("repository") else [])),
            "channel": channel or "default",
            "mode": mode,
            "ui_id": ui_id or pack_id,
        }
        if skip_post_install:
            payload["skip_post_install"] = True
        status, body = self.request("POST", "/manager/queue/install", payload, timeout=30, expect_json=False)
        if status != 200:
            raise RuntimeError(f"Manager rejected install for {pack_id} (HTTP {status}): {body}")
        return True

    def queue_update(self, pack_id, pack_info, *, ui_id=None):
        active_ver = str(pack_info.get("active_version") or pack_info.get("version") or "nightly")
        files = list(pack_info.get("files") or ([pack_info["repository"]] if pack_info.get("repository") else []))
        payload = {
            "id": pack_id,
            "version": active_ver,
            "files": files,
            "ui_id": ui_id or pack_id,
        }
        status, body = self.request("POST", "/manager/queue/update", payload, timeout=30, expect_json=False)
        if status != 200:
            raise RuntimeError(f"Manager rejected update for {pack_id} (HTTP {status}): {body}")
        return True

    def queue_start(self):
        status, body = self.request("POST", "/manager/queue/start", {}, timeout=20, expect_json=False)
        if status not in (200, 201):
            raise RuntimeError(f"Manager failed to start task queue (HTTP {status}): {body}")
        return status

    def reboot(self):
        status, body = self.request("POST", "/manager/reboot", {}, timeout=15, expect_json=False)
        if status != 200:
            raise RuntimeError(f"Manager reboot request failed (HTTP {status}): {body}")
        return True


def _pack_matches_spec(pack_id, pack_info, spec):
    spec_repo = _normalize_repo(spec.get("repo") or spec.get("url") or "")
    spec_name = str(spec.get("name") or _repo_basename(spec_repo)).strip().lower()
    pack_repo = _normalize_repo(pack_info.get("repository") or "")
    pack_files = {_normalize_repo(f) for f in (pack_info.get("files") or []) if isinstance(f, str)}
    pack_id_l = str(pack_id or "").strip().lower()
    pack_title_l = str(pack_info.get("title") or "").strip().lower()
    if spec_repo and (spec_repo == pack_repo or spec_repo in pack_files):
        return True
    if spec_name and spec_name in (pack_id_l, _repo_basename(pack_repo), pack_title_l):
        return True
    return False


def _match_mapping_key_to_pack_id(map_key, node_packs):
    if map_key in node_packs:
        return map_key
    norm_key = _normalize_repo(map_key)
    for pid, pinfo in node_packs.items():
        if not isinstance(pinfo, dict):
            continue
        if _normalize_repo(pinfo.get("repository") or "") == norm_key:
            return pid
        if any(_normalize_repo(f) == norm_key for f in (pinfo.get("files") or []) if isinstance(f, str)):
            return pid
    return None


def resolve_workflow_nodepacks(graph, object_info, mappings, node_packs, node_specs=()):
    """Identify ONLY nodepacks required by the active workflow (missing vs already installed)."""
    required_classes = sorted({n["class_type"] for n in graph.values() if isinstance(n, dict) and n.get("class_type")})
    present_classes = set(object_info.keys()) if isinstance(object_info, dict) else set()
    missing_classes = [cls for cls in required_classes if cls not in present_classes]

    class_to_packs = {}
    for map_key, entry in (mappings or {}).items():
        if not isinstance(entry, list) or not entry:
            continue
        pack_id = _match_mapping_key_to_pack_id(map_key, node_packs)
        if not pack_id:
            continue
        declared = set(entry[0]) if isinstance(entry[0], list) else set()
        meta = entry[1] if len(entry) > 1 and isinstance(entry[1], dict) else {}
        pattern = meta.get("nodename_pattern")
        compiled = None
        if isinstance(pattern, str) and pattern:
            try:
                compiled = re.compile(pattern)
            except re.error:
                compiled = None
        for cls in required_classes:
            if cls in declared or (compiled is not None and compiled.match(cls)):
                class_to_packs.setdefault(cls, []).append(pack_id)

    missing_pack_ids = []
    workflow_pack_ids = []

    def add_unique(target, pid):
        if pid and pid not in target:
            target.append(pid)

    for cls in missing_classes:
        for pid in class_to_packs.get(cls, ()):
            pinfo = node_packs.get(pid) or {}
            state = str(pinfo.get("state") or "").lower()
            if state != "installed":
                add_unique(missing_pack_ids, pid)
            add_unique(workflow_pack_ids, pid)

    for cls in required_classes:
        for pid in class_to_packs.get(cls, ()):
            add_unique(workflow_pack_ids, pid)

    # Also include nodepacks explicitly associated with the active workflow package.
    for spec in (node_specs or ()):
        if not isinstance(spec, dict):
            continue
        for pid, pinfo in (node_packs or {}).items():
            if isinstance(pinfo, dict) and _pack_matches_spec(pid, pinfo, spec):
                add_unique(workflow_pack_ids, pid)
                state = str(pinfo.get("state") or "").lower()
                if state in ("not-installed", "disabled"):
                    add_unique(missing_pack_ids, pid)
                break

    return {
        "requiredClasses": required_classes,
        "missingClasses": missing_classes,
        "missingPackIds": missing_pack_ids,
        "workflowPackIds": workflow_pack_ids,
    }


def _pack_has_incompatible_gpu_requirements(pack_info):
    for req in (pack_info.get("pip") or ()):
        pkg = _req_pkg_name(req)
        if pkg in PROTECTED_GPU_PKGS or pkg.startswith(("nvidia-", "cuda-")):
            return req
    return None


def _pack_local_dir(root, pack_id, pack_info, node_specs):
    custom_nodes = Path(root) / "custom_nodes"
    if not custom_nodes.is_dir():
        return None
    candidates = []
    for spec in (node_specs or ()):
        if isinstance(spec, dict) and _pack_matches_spec(pack_id, pack_info, spec):
            name = spec.get("name") or _repo_basename(spec.get("repo") or spec.get("url") or "")
            if name:
                candidates.append(name)
    for raw in (pack_id, _repo_basename(pack_info.get("repository") or "")):
        if raw:
            candidates.append(raw)
    for f in (pack_info.get("files") or ()):
        if isinstance(f, str) and f:
            candidates.append(Path(f).name.removesuffix(".git"))
    seen = set()
    for name in candidates:
        low = name.lower()
        if low in seen:
            continue
        seen.add(low)
        cand = custom_nodes / name
        if cand.is_dir():
            return cand
    return None


def check_update_eligibility(pack_id, pack_info, *, root, manifest, node_specs, installed_map=None):
    """Return (needs_update, eligible, skip_reason) for an already-installed workflow nodepack."""
    if not isinstance(pack_info, dict):
        return False, False, "Not managed by ComfyUI-Manager."

    state = str(pack_info.get("state") or "").lower()
    if state != "installed":
        return False, False, ""

    # Determine whether ComfyUI-Manager actually indicates an update is available.
    updatable = bool(pack_info.get("updatable") is True)
    active_ver = str(pack_info.get("active_version") or "").strip()
    latest_ver = str(pack_info.get("version") or "").strip()
    version_behind = bool(
        active_ver and latest_ver
        and active_ver not in ("unknown", "nightly")
        and latest_ver not in ("unknown", "nightly")
        and active_ver != latest_ver
    )
    if not (updatable or version_behind):
        return False, False, ""

    # Protection 1: Explicit protected adapters (e.g. sol_attn_minimax_pr117).
    protected_names = {n.lower() for n in PROTECTED_ADAPTER_NAMES}
    for extra in ((manifest or {}).get("protectedNodepacks") or ()):
        if isinstance(extra, str) and extra.strip():
            protected_names.add(extra.strip().lower())
    pack_aliases = {
        str(pack_id or "").lower(),
        str(pack_info.get("title") or "").lower(),
        _repo_basename(pack_info.get("repository") or "").lower(),
    }
    if pack_aliases & protected_names:
        return True, False, f"Protected specialized adapter ({pack_id}) must not be overwritten."

    # Protection 2: Package-pinned commit/version lock.
    allow_mgr_update = bool((manifest or {}).get("allowManagerUpdate") is True)
    for spec in (node_specs or ()):
        if isinstance(spec, dict) and _pack_matches_spec(pack_id, pack_info, spec):
            pinned = str(spec.get("revision") or spec.get("ref") or "").strip()
            if pinned and not allow_mgr_update:
                return True, False, f"Pinned by package lock ({pinned[:12]}); preserved for compatibility."

    # Protection 3: Manually installed, unmanaged, or locally patched custom node directory.
    if active_ver == "unknown" and not pack_info.get("repository"):
        return True, False, "Locally installed or unmanaged by ComfyUI-Manager."
    local_dir = _pack_local_dir(root, pack_id, pack_info, node_specs)
    if local_dir is not None:
        if local_dir.name.lower() in protected_names:
            return True, False, f"Protected local adapter directory ({local_dir.name})."
        if (local_dir / ".git").exists():
            if not provision.git_is_clean(local_dir):
                return True, False, f"Locally patched Git repository ({local_dir.name}) has uncommitted changes."
        else:
            # Non-git directory not managed via CNR version
            if not (installed_map and pack_id in installed_map and installed_map[pack_id].get("ver") not in (None, "", "unknown", "nightly")):
                return True, False, f"Manually installed directory ({local_dir.name}) not managed by ComfyUI-Manager."

    # Protection 4: Incompatible CUDA/PyTorch dependency override.
    bad_req = _pack_has_incompatible_gpu_requirements(pack_info)
    if bad_req:
        return True, False, f"Update declares incompatible GPU/PyTorch dependency ({bad_req})."

    return True, True, ""


def wait_for_manager_queue(client, *, timeout_seconds=600, poll_interval=1.5, on_poll=None):
    """Block until ComfyUI-Manager finishes all queued tasks; never return while still processing."""
    deadline = time.monotonic() + timeout_seconds
    last = None
    while True:
        st = client.queue_status()
        last = st
        if on_poll:
            on_poll(st)
        if not st["is_processing"] and st["in_progress_count"] == 0:
            return st
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out waiting for ComfyUI-Manager queue to finish "
                f"(done {st['done_count']}/{st['total_count']}, in_progress={st['in_progress_count']})."
            )
        time.sleep(poll_interval)


def run_post_install_sync(
    *,
    root,
    graph,
    api,
    port,
    manifest=None,
    node_specs=(),
    stage=None,
    on_progress=None,
    restart_fn=None,
    client=None,
    queue_timeout=600,
    poll_interval=1.5,
    ready_timeout=180,
    redact_fn=None,
):
    """Additive post-installation ComfyUI-Manager sync for active workflow custom nodes."""
    clean = redact_fn or (lambda s: str(s or ""))
    mgr = client or ComfyManagerClient(f"http://127.0.0.1:{port}")

    sync = {
        "phase": "scanning",
        "managerVersion": None,
        "missingNodepacks": [],
        "currentInstalling": "",
        "installedNodepacks": [],
        "updatableNodepacks": [],
        "currentUpdating": "",
        "updatedNodepacks": [],
        "skippedProtected": [],
        "errors": [],
        "importErrors": [],
        "remainingMissingClasses": [],
        "restartRequired": False,
        "restarted": False,
        "finalStatus": "running",
    }

    def emit(stage_status, message, count=None, total=None):
        if on_progress:
            on_progress(dict(sync))
        if stage:
            stage(STAGE_NAME, stage_status, clean(message), count, total)

    emit("Running", "Scanning workflow custom nodes via ComfyUI-Manager")

    # 1. Check ComfyUI-Manager availability
    mgr_ver = mgr.version()
    sync["managerVersion"] = mgr_ver
    if not mgr_ver:
        sync["phase"] = "skipped"
        sync["finalStatus"] = "skipped"
        emit("Skipped", "ComfyUI-Manager API not detected on this ComfyUI instance")
        return sync

    # 2. Scan workflow against live /object_info and ComfyUI-Manager registry
    try:
        object_info = api("/object_info")
        mappings = mgr.get_mappings()
        channel, node_packs = mgr.get_node_list()
        installed_map = mgr.get_installed()
    except Exception as exc:
        err = clean(str(exc))
        sync["phase"] = "failed"
        sync["finalStatus"] = "failed"
        sync["errors"].append({"nodepack": "ComfyUI-Manager", "operation": "scan", "error": err})
        emit("Failed", f"Failed to scan ComfyUI-Manager registry: {err}")
        return sync

    # Inspect protected local adapters under custom_nodes/ so they are reported transparently if present
    custom_nodes_dir = Path(root) / "custom_nodes"
    if custom_nodes_dir.is_dir():
        for adapter_name in sorted(PROTECTED_ADAPTER_NAMES):
            if (custom_nodes_dir / adapter_name).is_dir():
                sync["skippedProtected"].append({
                    "name": adapter_name,
                    "reason": "Protected specialized adapter preserved untouched.",
                })

    resolved = resolve_workflow_nodepacks(graph, object_info, mappings, node_packs, node_specs)
    missing_pack_ids = list(resolved["missingPackIds"])
    sync["missingNodepacks"] = [
        str((node_packs.get(pid) or {}).get("title") or pid) for pid in missing_pack_ids
    ]

    # Wait if another ComfyUI-Manager queue job is already active
    try:
        wait_for_manager_queue(mgr, timeout_seconds=queue_timeout, poll_interval=poll_interval)
    except Exception as exc:
        err = clean(str(exc))
        sync["phase"] = "failed"
        sync["finalStatus"] = "failed"
        sync["errors"].append({"nodepack": "ComfyUI-Manager", "operation": "queue-wait", "error": err})
        emit("Failed", f"ComfyUI-Manager queue busy: {err}")
        return sync

    # 3. Install verified missing workflow nodepacks
    if missing_pack_ids:
        sync["phase"] = "installing"
        emit("Running", f"Installing {len(missing_pack_ids)} missing workflow nodepack(s)", 0, len(missing_pack_ids))
        for idx, pid in enumerate(missing_pack_ids):
            pinfo = node_packs.get(pid) or {}
            label = str(pinfo.get("title") or pid)
            sync["currentInstalling"] = label
            emit("Running", f"Installing custom nodepack: {label}", idx, len(missing_pack_ids))
            try:
                is_disabled = str(pinfo.get("state") or "").lower() == "disabled"
                mgr.queue_install(
                    pid, pinfo,
                    channel=channel, mode="cache", ui_id=f"axdar-install-{pid}",
                    skip_post_install=is_disabled,
                )
                mgr.queue_start()
                wait_for_manager_queue(mgr, timeout_seconds=queue_timeout, poll_interval=poll_interval)
                # Confirm actual installation result via Manager's installed catalog
                after_installed = mgr.get_installed()
                _, after_packs = mgr.get_node_list(modes=("local", "cache", "default"))
                after_state = str((after_packs.get(pid) or {}).get("state") or "").lower()
                if pid in after_installed or after_state == "installed":
                    sync["installedNodepacks"].append(label)
                    sync["restartRequired"] = True
                else:
                    sync["errors"].append({
                        "nodepack": label,
                        "operation": "install",
                        "error": f"ComfyUI-Manager finished queue but {label} is still not marked installed (state={after_state or 'unknown'}).",
                    })
            except Exception as exc:
                sync["errors"].append({
                    "nodepack": label,
                    "operation": "install",
                    "error": clean(str(exc)),
                })
        sync["currentInstalling"] = ""

    # 4. Detect & update eligible already-installed workflow nodepacks (NEVER Update All)
    sync["phase"] = "updating"
    try:
        _, refreshed_packs = mgr.get_node_list(modes=("default", "cache", "local"))
        refreshed_installed = mgr.get_installed()
    except Exception:
        refreshed_packs = node_packs
        refreshed_installed = installed_map

    eligible_updates = []
    newly_installed_set = set(missing_pack_ids)
    for pid in resolved["workflowPackIds"]:
        if pid in newly_installed_set:
            continue
        pinfo = refreshed_packs.get(pid)
        label = str((pinfo or {}).get("title") or pid)
        needs_upd, eligible, reason = check_update_eligibility(
            pid, pinfo, root=root, manifest=manifest, node_specs=node_specs, installed_map=refreshed_installed,
        )
        if not needs_upd:
            continue
        if not eligible:
            if not any(s["name"] == label for s in sync["skippedProtected"]):
                sync["skippedProtected"].append({"name": label, "reason": reason})
            continue
        eligible_updates.append((pid, pinfo, label))

    sync["updatableNodepacks"] = [label for _, _, label in eligible_updates]
    if eligible_updates:
        emit("Running", f"Updating {len(eligible_updates)} workflow nodepack(s) to latest compatible version", 0, len(eligible_updates))
        for idx, (pid, pinfo, label) in enumerate(eligible_updates):
            sync["currentUpdating"] = label
            emit("Running", f"Updating custom nodepack: {label}", idx, len(eligible_updates))
            try:
                mgr.queue_update(pid, pinfo, ui_id=f"axdar-update-{pid}")
                mgr.queue_start()
                wait_for_manager_queue(mgr, timeout_seconds=queue_timeout, poll_interval=poll_interval)
                _, post_packs = mgr.get_node_list(modes=("local", "cache", "default"))
                post_info = post_packs.get(pid) or {}
                post_state = str(post_info.get("state") or "").lower()
                if post_state in ("not-installed", "disabled", "import-failed"):
                    sync["errors"].append({
                        "nodepack": label,
                        "operation": "update",
                        "error": f"ComfyUI-Manager finished update queue but {label} entered state={post_state}.",
                    })
                else:
                    sync["updatedNodepacks"].append(label)
                    sync["restartRequired"] = True
            except Exception as exc:
                sync["errors"].append({
                    "nodepack": label,
                    "operation": "update",
                    "error": clean(str(exc)),
                })
        sync["currentUpdating"] = ""

    # 5. Restart ComfyUI ONLY after all installs/updates finished AND ONLY if changes occurred
    if sync["restartRequired"]:
        sync["phase"] = "restarting"
        emit("Running", "Restarting ComfyUI after custom node changes")
        try:
            # Ensure queue is strictly idle right before restarting
            wait_for_manager_queue(mgr, timeout_seconds=queue_timeout, poll_interval=poll_interval)
            if restart_fn is not None:
                restart_fn()
            else:
                mgr.reboot()
            sync["restarted"] = True
        except Exception as exc:
            err = clean(str(exc))
            sync["phase"] = "failed"
            sync["finalStatus"] = "failed"
            sync["errors"].append({"nodepack": "ComfyUI", "operation": "restart", "error": err})
            emit("Failed", f"ComfyUI restart failed: {err}")
            return sync

        sync["phase"] = "waiting_ready"
        emit("Running", "Waiting for ComfyUI readiness after restart")
        until = time.monotonic() + ready_timeout
        while True:
            try:
                object_info = api("/object_info")
                if isinstance(object_info, dict) and object_info:
                    break
            except Exception:
                pass
            if time.monotonic() >= until:
                err = "ComfyUI did not become responsive after restart."
                sync["phase"] = "failed"
                sync["finalStatus"] = "failed"
                sync["errors"].append({"nodepack": "ComfyUI", "operation": "readiness", "error": err})
                emit("Failed", err)
                return sync
            time.sleep(2)
    else:
        try:
            object_info = api("/object_info")
        except Exception:
            pass

    # 6. Final verification against actual runtime /object_info and Manager import failure registry
    sync["phase"] = "verifying"
    emit("Running", "Verifying workflow nodes in live ComfyUI registry")
    present_after = set(object_info.keys()) if isinstance(object_info, dict) else set()
    sync["remainingMissingClasses"] = [cls for cls in resolved["requiredClasses"] if cls not in present_after]

    for pid in resolved["workflowPackIds"]:
        pinfo = (refreshed_packs.get(pid) or node_packs.get(pid) or {})
        fail_info = mgr.import_fail_info(cnr_id=pid) or (
            mgr.import_fail_info(url=pinfo.get("repository")) if pinfo.get("repository") else None
        )
        if fail_info:
            msg = fail_info.get("msg") or fail_info.get("error") or json.dumps(fail_info)
            sync["importErrors"].append({
                "nodepack": str(pinfo.get("title") or pid),
                "error": clean(str(msg))[:600],
            })

    has_problems = bool(sync["errors"] or sync["importErrors"] or sync["remainingMissingClasses"])
    sync["phase"] = "failed" if has_problems else "completed"
    sync["finalStatus"] = "failed" if has_problems else "passed"

    summary_parts = []
    if sync["installedNodepacks"]:
        summary_parts.append(f"{len(sync['installedNodepacks'])} installed")
    if sync["updatedNodepacks"]:
        summary_parts.append(f"{len(sync['updatedNodepacks'])} updated")
    if sync["skippedProtected"]:
        summary_parts.append(f"{len(sync['skippedProtected'])} protected skipped")
    if sync["restarted"]:
        summary_parts.append("ComfyUI restarted")
    if sync["remainingMissingClasses"]:
        summary_parts.append(f"{len(sync['remainingMissingClasses'])} missing nodes remain")
    if sync["errors"]:
        summary_parts.append(f"{len(sync['errors'])} error(s)")
    if not summary_parts:
        summary_parts.append("All workflow custom nodes already installed and up to date")

    emit("Failed" if has_problems else "Completed", " · ".join(summary_parts))
    return sync
