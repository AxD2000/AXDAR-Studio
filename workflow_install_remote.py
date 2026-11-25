"""Remote package-driven preflight, installer, and verifier supervised independently of SSH."""
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from urllib.request import urlopen

import production
import provision
import workflow_packages

PHASES = (
    "Vast connection",
    "Environment preflight",
    "Package dependency check",
    "Custom node installation",
    "Model verification",
    "ComfyUI restart",
    "Final verification",
)

_SAFE_HF_NAMES = frozenset({
    "hf_transfer", "hf_endpoint", "hf_datasets", "hf_mirrors",
    "hf_cache", "hf_models", "hf_spaces", "hf_token", "hf_home",
})


def redact(text, token="", extra_tokens=()):
    text = str(text or "")
    for secret in (token, *(extra_tokens or ())):
        if isinstance(secret, str) and len(secret) >= 8:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(\bBearer\s+)[A-Za-z0-9._-]{16,}", r"\1[REDACTED]", text)
    return re.sub(
        r"\bhf_[A-Za-z0-9]{10,}\b",
        lambda match: match.group(0) if match.group(0).lower() in _SAFE_HF_NAMES else "[REDACTED]",
        text,
    )


def _canonical_hash(obj):
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def probe_environment(root, api=None):
    """Collect verified ComfyUI, Python, PyTorch, CUDA, GPU, and disk facts on the target host."""
    root = Path(root)
    comfy_rev = provision.git_head(root)
    py = provision.comfy_python()
    probe_code = (
        "import json, sys; "
        "out = {'python': '.'.join(map(str, sys.version_info[:3])), 'torch': None, 'cuda': None, "
        "'gpuName': None, 'gpuArch': None, 'vramGb': 0.0}; "
        "\ntry:\n"
        "  import torch\n"
        "  out['torch'] = str(torch.__version__)\n"
        "  out['cuda'] = str(torch.version.cuda or '')\n"
        "  if torch.cuda.is_available():\n"
        "    props = torch.cuda.get_device_properties(0)\n"
        "    out['gpuName'] = str(props.name)\n"
        "    out['gpuArch'] = f'{props.major}.{props.minor}'\n"
        "    out['vramGb'] = round(props.total_memory / (1024**3), 2)\n"
        "except Exception as exc:\n"
        "  out['probeError'] = str(exc)\n"
        "print(json.dumps(out))"
    )
    runtime = {"python": None, "torch": None, "cuda": None, "gpuName": None, "gpuArch": None, "vramGb": 0.0}
    try:
        out = subprocess.check_output([py, "-c", probe_code], stderr=subprocess.DEVNULL, text=True, timeout=40)
        runtime.update(json.loads(out))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        runtime["probeError"] = str(exc)

    if api is not None and not runtime.get("vramGb"):
        try:
            stats = api("/system_stats")
            devices = stats.get("devices") or []
            if devices:
                dev = devices[0]
                if dev.get("vram_total"):
                    runtime["vramGb"] = round(dev["vram_total"] / (1024**3), 2)
                if dev.get("name") and not runtime.get("gpuName"):
                    runtime["gpuName"] = str(dev["name"])
        except Exception:
            pass

    models_root = root / "models"
    disk_target = models_root if models_root.is_dir() else root
    disk_free = shutil.disk_usage(disk_target).free if disk_target.exists() else 0

    env = {
        "comfyuiRoot": str(root),
        "comfyuiRevision": comfy_rev,
        "comfyuiGitClean": provision.git_is_clean(root) if comfy_rev else False,
        "pythonVersion": runtime.get("python"),
        "torchVersion": runtime.get("torch"),
        "cudaVersion": runtime.get("cuda"),
        "gpuName": runtime.get("gpuName"),
        "gpuArch": runtime.get("gpuArch"),
        "vramGb": float(runtime.get("vramGb") or 0.0),
        "diskFreeBytes": int(disk_free),
    }
    if runtime.get("probeError"):
        env["probeError"] = runtime["probeError"]
    env["environmentFingerprint"] = _canonical_hash({
        "comfyuiRevision": env["comfyuiRevision"],
        "pythonVersion": env["pythonVersion"],
        "torchVersion": env["torchVersion"],
        "cudaVersion": env["cudaVersion"],
        "gpuName": env["gpuName"],
        "gpuArch": env["gpuArch"],
        "vramGb": round(env["vramGb"], 1),
    })
    return env


def compute_verified_fingerprint(env, result):
    return _canonical_hash({
        "base": env["environmentFingerprint"],
        "nodes": {r["repo"]: r["actualRevision"] for r in result.get("nodeRepos", [])},
        "models": {m["name"]: m["source"]["sha256"] for m in result.get("models", []) if m["status"] == "Installed"},
        "attention": (result.get("attention") or {}).get("status"),
    })


def check_package_compatibility(env, manifest, root, node_specs, model_specs, *, allow_comfyui_align=False):
    """Detect environment, revision, hardware, disk, or file conflicts BEFORE modifying anything."""
    root = Path(root)
    incompatibilities = []
    comfyui_mismatch = None

    expected_comfy = manifest["comfyui"]["revision"]
    actual_comfy = env.get("comfyuiRevision")
    if actual_comfy != expected_comfy:
        can_align = bool(env.get("comfyuiGitClean"))
        comfyui_mismatch = {
            "actual": actual_comfy,
            "expected": expected_comfy,
            "repo": manifest["comfyui"].get("repo", "comfyanonymous/ComfyUI"),
            "canAutoAlign": can_align,
        }
        if not (allow_comfyui_align and can_align):
            hint = (
                " Confirm 'Align ComfyUI to pinned revision' during installation (no Jupyter Terminal needed)."
                if can_align
                else " Host ComfyUI directory is missing Git history or has uncommitted local edits."
            )
            incompatibilities.append(
                f"ComfyUI revision mismatch: host has {actual_comfy or 'unknown'}, package requires {expected_comfy}.{hint}"
            )

    rt = manifest["runtime"]
    if not env.get("pythonVersion") or not workflow_packages.version_in_range(env["pythonVersion"], rt["python"]):
        incompatibilities.append(
            f"Python version {env.get('pythonVersion') or 'unknown'} is outside supported range {rt['python']}."
        )
    if not env.get("torchVersion") or not workflow_packages.version_in_range(env["torchVersion"], rt["torch"]):
        incompatibilities.append(
            f"PyTorch version {env.get('torchVersion') or 'unknown'} is outside supported range {rt['torch']}."
        )
    if not env.get("cudaVersion") or not workflow_packages.version_in_range(env["cudaVersion"], rt["cuda"]):
        incompatibilities.append(
            f"CUDA version {env.get('cudaVersion') or 'unknown'} is outside supported range {rt['cuda']}."
        )
    if float(env.get("vramGb") or 0.0) + 0.25 < float(rt["minVramGb"]):
        incompatibilities.append(
            f"Insufficient GPU VRAM: host has {env.get('vramGb', 0):.1f} GB, package requires at least {rt['minVramGb']} GB."
        )
    allowed_caps = rt.get("computeCapabilities")
    if allowed_caps and env.get("gpuArch") not in allowed_caps:
        incompatibilities.append(
            f"GPU compute capability {env.get('gpuArch') or 'unknown'} is not in package supported list {allowed_caps}."
        )

    # Check existing custom node repositories for conflicting revisions (never silently reuse wrong commits).
    for spec in node_specs:
        dest = root / "custom_nodes" / spec["repo"].split("/")[1]
        if dest.exists():
            actual = provision.git_head(dest)
            if actual != spec["revision"]:
                incompatibilities.append(
                    f"Conflicting custom node on host: {spec['repo']} is at {actual or 'untracked'}, package pins {spec['revision']}."
                )

    # Check existing model files at target paths and total disk space needed for missing models.
    missing_bytes = 0
    for m in model_specs:
        status, dest, reason = provision.verify_model_file(root, m, check_hash=False)
        if status == "Incompatible":
            incompatibilities.append(f"Conflicting model file on host ({dest.name}): {reason}")
        elif status == "Missing":
            tmp = dest.with_suffix(".axdar-part")
            partial = tmp.stat().st_size if tmp.exists() else 0
            missing_bytes += max(0, m["bytes"] - partial)

    disk_free = int(env.get("diskFreeBytes") or 0)
    if missing_bytes and disk_free < missing_bytes + 256 * 1024**2:
        incompatibilities.append(
            f"Insufficient disk space: {missing_bytes / 1e9:.2f} GB needed, {disk_free / 1e9:.2f} GB available."
        )

    return {
        "compatible": not incompatibilities,
        "incompatibilities": incompatibilities,
        "comfyuiMismatch": comfyui_mismatch,
        "environment": env,
        "environmentFingerprint": env["environmentFingerprint"],
        "downloadBytes": missing_bytes,
        "diskFreeBytes": disk_free,
    }


def _model_listed_exactly(value, info, graph):
    """Verify the exact model relpath is advertised by ComfyUI /object_info without silent substitution."""
    norm = provision.model_path(value)
    for node in graph.values():
        kind = node["class_type"]
        schema = info.get(kind, {}).get("input", {})
        for field, val in node.get("inputs", {}).items():
            if not isinstance(val, str) or provision.model_path(val) != norm:
                continue
            spec = schema.get("required", {}).get(field) or schema.get("optional", {}).get(field)
            options = spec[0] if spec and isinstance(spec[0], list) else (
                spec[1].get("options") if spec and len(spec) > 1 and isinstance(spec[1], dict) else None
            )
            if isinstance(options, list):
                if any(isinstance(o, str) and provision.model_path(o) == norm for o in options):
                    return True
                return False
            if kind in info:
                return True
    return True


def scan_package(graph, info, root, manifest, node_specs, model_specs, env=None, *, verify_hashes=False, allow_comfyui_align=False):
    """Strictly check package nodes, models, attention, and runtime compatibility with zero guessing."""
    root = Path(root)
    if env is None:
        env = probe_environment(root)
    compat = check_package_compatibility(env, manifest, root, node_specs, model_specs, allow_comfyui_align=allow_comfyui_align)

    missing_classes = sorted({n["class_type"] for n in graph.values()} - info.keys())
    node_statuses = []
    for spec in node_specs:
        dest = root / "custom_nodes" / spec["repo"].split("/")[1]
        actual_rev = provision.git_head(dest) if dest.exists() else None
        unregistered = [c for c in spec["classes"] if c not in info]
        if dest.exists() and actual_rev != spec["revision"]:
            st = "Incompatible"
        elif dest.exists() and not unregistered:
            st = "Installed"
        elif dest.exists() and unregistered:
            st = "Needs restart"
        else:
            st = "Missing"
        node_statuses.append({
            "repo": spec["repo"],
            "revision": spec["revision"],
            "actualRevision": actual_rev,
            "classes": spec["classes"],
            "missingClasses": unregistered,
            "status": st,
        })

    models_report = []
    for m in model_specs:
        rel = workflow_packages.expected_comfy_model_relpath(m)
        disk_status, _dest, reason = provision.verify_model_file(root, m, check_hash=verify_hashes)
        if disk_status == "Installed":
            listed = _model_listed_exactly(rel, info, graph)
            status = "Installed" if listed else "Needs ComfyUI refresh"
            err = "" if listed else "File exists on disk with exact size/checksum, but ComfyUI /object_info has not registered it yet."
        else:
            status = disk_status
            err = reason
        models_report.append({
            "name": rel,
            "folder": m["folder"],
            "status": status,
            "verified": True,
            "error": err or None,
            "source": {k: m[k] for k in ("repo", "revision", "file", "sha256", "bytes")},
        })

    att = manifest["runtime"]["attention"]
    if att["backend"] == "sageattention":
        att_status = "Installed" if provision.sage_ready() else "Missing"
    elif att["backend"] == "sdpa":
        att_status = "SDPA"
    else:
        att_status = "Not required"

    return {
        "packaged": True,
        "nodes": missing_classes,
        "nodeRepos": node_statuses,
        "models": models_report,
        "attention": {
            "backend": att["backend"],
            "allowSdpaFallback": bool(att.get("allowSdpaFallback", False)),
            "status": att_status,
        },
        "sageAttention": att_status if att["backend"] == "sageattention" else None,
        "compatible": compat["compatible"],
        "incompatibilities": compat["incompatibilities"],
        "comfyuiMismatch": compat.get("comfyuiMismatch"),
        "environment": env,
        "environmentFingerprint": env["environmentFingerprint"],
        "downloadBytes": compat["downloadBytes"],
        "diskFreeBytes": compat["diskFreeBytes"],
    }


def scan(graph, info, exists=None):
    """Legacy read-only scanner preserved for existing non-packaged profiles."""
    missing = sorted({n["class_type"] for n in graph.values()} - info.keys())
    models = {}
    verified = provision.model_sources()
    for filename, source in list(verified.items()):
        folder = source["folder"].split("/")
        verified["/".join([*folder[1:], filename])] = source
    for node in graph.values():
        kind = node["class_type"]
        schema = info.get(kind, {}).get("input", {})
        for field, value in node["inputs"].items():
            if not isinstance(value, str) or not value.lower().endswith(provision.MODEL_SUFFIXES):
                continue
            name = provision.model_path(value)
            source = verified.get(name)
            spec = schema.get("required", {}).get(field) or schema.get("optional", {}).get(field)
            options = spec[0] if spec and isinstance(spec[0], list) else (
                spec[1].get("options") if spec and len(spec) > 1 and isinstance(spec[1], dict) else None
            )
            listed = isinstance(options, list) and provision.model_choice(value, options) is not None
            on_disk = exists(name) if exists else False
            if listed or on_disk and options is None and kind in info:
                status = "Installed"
            elif on_disk and isinstance(options, list):
                status = "Incompatible"
            else:
                status = "Missing" if source or isinstance(options, list) else "Requires manual action"
            previous = models.get(name)
            if previous and previous["status"] == "Incompatible":
                continue
            models[name] = dict(
                name=name, status=status, verified=bool(source),
                source={k: source[k] for k in ("repo", "revision", "file", "sha256", "bytes")} if source else None,
            )
    return {"nodes": missing, "models": [models[name] for name in sorted(models)]}


def root_path():
    configured = os.environ.get("COMFY_ROOT")
    candidates = [Path(configured)] if configured else []
    candidates.extend((Path("/workspace/ComfyUI"), Path("/opt/ComfyUI"), Path.home() / "ComfyUI"))
    for root in candidates:
        if (root / "main.py").is_file():
            return root.resolve()
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            if b"main.py" not in (proc / "cmdline").read_bytes():
                continue
            cwd = (proc / "cwd").resolve(strict=True)
            if (cwd / "main.py").is_file() and (cwd / "comfy").is_dir():
                return cwd
        except OSError:
            continue
    raise ValueError("Could not discover the active ComfyUI installation.")


def _extract_zip_safely(pkg_zip, dest_dir):
    """Validate archive CRC integrity and safely extract all package files into dest_dir."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_root = dest_dir.resolve()
    raw = Path(pkg_zip).read_bytes()
    parsed = workflow_packages.parse_package(raw, Path(pkg_zip).name)
    entries = {}
    with zipfile.ZipFile(pkg_zip, "r") as zf:
        bad = zf.testzip()
        if bad is not None:
            raise ValueError(f"Uploaded package.zip failed CRC check on {bad!r}.")
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            if (name.startswith("/") or re.match(r"^[A-Za-z]:", name)
                    or ".." in name.split("/") or any(c in name for c in "\x00\r\n")):
                raise ValueError(f"Unsafe path in package.zip: {info.filename!r}.")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError(f"Symlinks are forbidden in package.zip: {info.filename!r}.")
            parts = [p for p in name.split("/") if p]
            if not parts or any(p in (".", "..") for p in parts):
                raise ValueError(f"Unsafe path in package.zip: {info.filename!r}.")
            entries["/".join(parts)] = zf.read(info)
    if "manifest.json" not in entries:
        tops = {k.split("/", 1)[0] for k in entries if "/" in k}
        if len(tops) == 1 and all("/" in k for k in entries):
            prefix = next(iter(tops)) + "/"
            stripped = {k[len(prefix):]: v for k, v in entries.items()}
            if "manifest.json" in stripped:
                entries = stripped
    for rel, content in entries.items():
        target = (dest_root / rel).resolve()
        try:
            target.relative_to(dest_root)
        except ValueError as exc:
            raise ValueError(f"Path escapes extraction root: {rel!r}.") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        if rel.lower().endswith(".sh"):
            try:
                target.chmod(0o700)
            except OSError:
                pass
    return parsed, dest_root


def _unpack_remote_package(folder):
    """Safely extract uploaded package.zip on /workspace and return parsed package payload."""
    pkg_zip = folder / "package.zip"
    if not pkg_zip.is_file():
        return None
    parsed, _dest = _extract_zip_safely(pkg_zip, folder / "package")
    return parsed


def _supervisor_has_comfyui():
    if not shutil.which("supervisorctl"):
        return False
    try:
        res = subprocess.run(
            ["supervisorctl", "status", "comfyui"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=15,
        )
        return res.returncode in (0, 3) and "comfyui" in (res.stdout or "").lower()
    except (OSError, subprocess.SubprocessError):
        return False


def _stream_bootstrap_command(cmd, cwd, env, secret_tokens, event_prefix, on_event):
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    hf_tok = env.get("HF_TOKEN", "")
    assert proc.stdout is not None
    for raw_line in proc.stdout:
        clean_line = redact(raw_line, hf_tok, extra_tokens=secret_tokens)
        print(clean_line, end="", flush=True)
        stripped = clean_line.strip()
        if event_prefix and stripped.startswith(event_prefix.strip()):
            payload = stripped[len(event_prefix.strip()):].strip()
            try:
                ev = json.loads(payload)
            except ValueError:
                ev = None
            if isinstance(ev, dict):
                on_event(ev)
    return proc.wait()


def _execute_bootstrap(folder, config, api, idle, stage, report_fn, metrics_fn=None):
    """Safely extract a bootstrap Workflow ZIP package on /workspace and execute its install.sh entrypoint."""
    root = root_path()
    info = api("/object_info")
    stage(PHASES[0], "Completed", str(root))

    pkg_zip = folder / "package.zip"
    if not pkg_zip.is_file():
        raise ValueError("Uploaded package.zip was not found in the remote installation workspace.")
    parsed, pkg_dir = _extract_zip_safely(pkg_zip, folder / "package")
    manifest = parsed["manifest"]
    graph = config.get("graph") or parsed["workflow"]
    node_specs = parsed.get("nodes") or config.get("nodes") or []
    model_specs = parsed.get("models") or config.get("models") or []
    entrypoint = manifest["entrypoint"]
    entry_path = (pkg_dir / entrypoint).resolve()
    try:
        entry_path.relative_to(pkg_dir)
    except ValueError as exc:
        raise ValueError(f"Entrypoint escapes package directory: {entrypoint!r}.") from exc
    if not entry_path.is_file():
        raise ValueError(f"Package entrypoint {entrypoint!r} was not found after extraction.")

    extra_secrets = []
    settings_path = pkg_dir / "settings.json"
    if settings_path.is_file():
        try:
            sdata = json.loads(settings_path.read_text("utf-8-sig"))
            for key in ("hf_token", "civitai_token"):
                val = str(sdata.get(key) or "").strip()
                if val and "PASTE_YOUR" not in val and len(val) >= 8:
                    extra_secrets.append(val)
        except Exception:
            pass

    def emit_metrics(**vals):
        if metrics_fn:
            metrics_fn(**vals)

    models_total = max(len(model_specs), int(manifest.get("model_count") or 0))
    nodes_total = len(node_specs)
    files_total = nodes_total + models_total
    progress_state = {
        "nodesCompleted": 0,
        "modelsCompleted": 0,
        "downloadSpeedMiBs": 0.0,
        "currentFile": "",
        "filePercent": 0.0,
    }

    def push_metrics():
        emit_metrics(
            filesTotal=files_total,
            filesCompleted=min(files_total, progress_state["nodesCompleted"] + progress_state["modelsCompleted"]),
            modelsTotal=models_total,
            modelsCompleted=progress_state["modelsCompleted"],
            nodesTotal=nodes_total,
            nodesCompleted=progress_state["nodesCompleted"],
            downloadSpeedMiBs=progress_state["downloadSpeedMiBs"],
            currentFile=progress_state["currentFile"],
            filePercent=progress_state["filePercent"],
        )

    push_metrics()

    stage(PHASES[1], "Running", "Probing remote ComfyUI environment")
    env_info = probe_environment(root, api)
    missing_classes = sorted({n["class_type"] for n in graph.values()} - info.keys())
    models_root = root / "models"
    model_rows = []
    for m in model_specs:
        rel = m.get("local") or workflow_packages.expected_comfy_model_relpath(m)
        on_disk = (models_root / rel).is_file()
        model_rows.append({
            "name": rel,
            "folder": m.get("folder", ""),
            "status": "Installed" if on_disk else "Missing",
            "verified": bool(m.get("sha256")),
            "source": {k: m.get(k) for k in ("repo", "revision", "file", "sha256", "bytes")},
        })
    node_rows = []
    for n in node_specs:
        name = n.get("name") or str(n.get("repo", "")).split("/")[-1]
        dest = root / "custom_nodes" / name
        node_rows.append({
            "repo": n.get("repo") or name,
            "revision": n.get("revision") or "",
            "actualRevision": provision.git_head(dest) if dest.exists() else None,
            "status": "Installed" if dest.exists() else "Missing",
        })
    progress_state["nodesCompleted"] = sum(1 for r in node_rows if r["status"] == "Installed")
    progress_state["modelsCompleted"] = sum(1 for m in model_rows if m["status"] == "Installed")
    push_metrics()

    result = {
        "packaged": True,
        "packageKind": "bootstrap",
        "nodes": missing_classes,
        "nodeRepos": node_rows,
        "models": model_rows,
        "compatible": True,
        "incompatibilities": [],
        "environment": env_info,
        "environmentFingerprint": env_info["environmentFingerprint"],
    }
    report_fn(result)
    stage(PHASES[1], "Completed", f"Environment {env_info['environmentFingerprint'][:12]} ready")
    stage(PHASES[2], "Completed", f"{len(missing_classes)} missing classes · {models_total} locked models")

    if config["mode"] == "scan":
        available = not missing_classes and all(m["status"] == "Installed" for m in model_rows)
        stage(PHASES[4], "Completed", "Checked bootstrap package dependencies")
        stage(PHASES[6], "Completed" if available else "Failed",
              "All package dependencies present" if available else "Run installation to execute package installer")
        return 0 if available else 2

    idle()
    event_prefix = str(manifest.get("install_event_prefix") or "AXDAR_EVENT ")
    child_env = dict(os.environ)
    child_env["COMFY_URL"] = f"http://127.0.0.1:{config['port']}"
    child_env["COMFY_ROOT"] = str(root)
    child_env["COMFY_PYTHON"] = provision.comfy_python()
    child_env["PYTHONUNBUFFERED"] = "1"
    if child_env.get("HF_TOKEN") and not child_env.get("HUGGING_FACE_HUB_TOKEN"):
        child_env["HUGGING_FACE_HUB_TOKEN"] = child_env["HF_TOKEN"]

    def on_event(ev):
        ev_stage = str(ev.get("stage") or "")
        if ev_stage == "node_installed":
            progress_state["nodesCompleted"] = min(nodes_total, progress_state["nodesCompleted"] + 1)
            progress_state["currentFile"] = str(ev.get("name") or "")
            stage(PHASES[3], "Running", f"Installed {ev.get('name', '')}", progress_state["nodesCompleted"], nodes_total)
            push_metrics()
        elif ev_stage == "models":
            completed_m = int(ev.get("completed") or progress_state["modelsCompleted"])
            total_m = int(ev.get("total_files") or models_total or 1)
            bps = float(ev.get("bytes_per_second") or 0.0)
            dl_bytes = float(ev.get("downloaded_bytes") or 0.0)
            tot_bytes = float(ev.get("total_bytes") or 0.0)
            pct = round((dl_bytes * 100.0 / tot_bytes), 1) if tot_bytes > 0 else (100.0 if ev.get("status") == "verified" else 0.0)
            progress_state["modelsCompleted"] = completed_m
            progress_state["downloadSpeedMiBs"] = round(bps / (1024.0 * 1024.0), 2)
            progress_state["currentFile"] = str(ev.get("file") or "")
            progress_state["filePercent"] = pct
            stage(
                PHASES[4], "Running",
                f"{progress_state['currentFile']} · {pct:.0f}% · {progress_state['downloadSpeedMiBs']:.1f} MiB/s",
                completed_m, total_m,
            )
            push_metrics()
        elif ev_stage == "failed":
            err_msg = redact(str(ev.get("error") or "Installer failed"), child_env.get("HF_TOKEN", ""), extra_tokens=extra_secrets)
            stage(PHASES[6], "Failed", err_msg)

    supervised = bool(manifest.get("install_only_command") and _supervisor_has_comfyui())
    stage(PHASES[3], "Running", f"Executing {entrypoint}" + (" (--install-only)" if supervised else ""))
    progress_state["nodesCompleted"] = 0
    progress_state["modelsCompleted"] = 0
    push_metrics()

    if supervised:
        subprocess.run(["supervisorctl", "stop", "comfyui"], check=False, timeout=60)
        rc = _stream_bootstrap_command(["bash", str(entry_path), "--install-only"], pkg_dir, child_env, extra_secrets, event_prefix, on_event)
        stage(PHASES[3], "Completed" if rc == 0 else "Failed", f"{entrypoint} exited with code {rc}")
        stage(PHASES[4], "Completed" if rc == 0 else "Failed", f"{progress_state['modelsCompleted']}/{models_total} models processed")
        stage(PHASES[5], "Running", "Restarting ComfyUI supervisor service")
        subprocess.run(["supervisorctl", "start", "comfyui"], check=True, timeout=90)
        until = time.monotonic() + 180
        while True:
            try:
                info = api("/object_info")
                break
            except (OSError, ValueError):
                if time.monotonic() >= until:
                    raise
                time.sleep(2)
        stage(PHASES[5], "Completed")
        if rc != 0:
            return 1
        if manifest.get("validate_command"):
            stage(PHASES[6], "Running", f"Running {entrypoint} --validate")
            vrc = _stream_bootstrap_command(["bash", str(entry_path), "--validate"], pkg_dir, child_env, extra_secrets, event_prefix, on_event)
            if vrc != 0:
                stage(PHASES[6], "Failed", f"Validation failed (exit {vrc})")
                return 1
    else:
        rc = _stream_bootstrap_command(["bash", str(entry_path)], pkg_dir, child_env, extra_secrets, event_prefix, on_event)
        stage(PHASES[3], "Completed" if rc == 0 else "Failed", f"{entrypoint} exited with code {rc}")
        stage(PHASES[4], "Completed" if rc == 0 else "Failed", f"{progress_state['modelsCompleted']}/{models_total} models processed")
        stage(PHASES[5], "Completed" if rc == 0 else "Skipped", "Managed by package entrypoint")
        if rc != 0:
            return 1
        until = time.monotonic() + 120
        while True:
            try:
                info = api("/object_info")
                break
            except (OSError, ValueError):
                if time.monotonic() >= until:
                    raise
                time.sleep(2)

    stage(PHASES[6], "Running", "Verifying workflow nodes against ComfyUI /object_info")
    final_env = probe_environment(root, api)
    missing_after = sorted({n["class_type"] for n in graph.values()} - info.keys())
    final_models = [
        {
            "name": m.get("local") or workflow_packages.expected_comfy_model_relpath(m),
            "folder": m.get("folder", ""),
            "status": "Installed" if (models_root / (m.get("local") or workflow_packages.expected_comfy_model_relpath(m))).is_file() else "Missing",
            "verified": True,
            "source": {k: m.get(k) for k in ("repo", "revision", "file", "sha256", "bytes")},
        }
        for m in model_specs
    ]
    final_nodes = [
        {
            "repo": n.get("repo") or n.get("name", ""),
            "revision": n.get("revision") or "",
            "actualRevision": provision.git_head(root / "custom_nodes" / (n.get("name") or str(n.get("repo", "")).split("/")[-1])),
            "status": "Installed" if (root / "custom_nodes" / (n.get("name") or str(n.get("repo", "")).split("/")[-1])).exists() else "Missing",
        }
        for n in node_specs
    ]
    progress_state["nodesCompleted"] = sum(1 for r in final_nodes if r["status"] == "Installed")
    progress_state["modelsCompleted"] = sum(1 for m in final_models if m["status"] == "Installed")
    progress_state["downloadSpeedMiBs"] = 0.0
    progress_state["filePercent"] = 100.0 if not missing_after else progress_state["filePercent"]
    push_metrics()
    fp = _canonical_hash({
        "base": final_env["environmentFingerprint"],
        "packageKind": "bootstrap",
        "nodes": {r["repo"]: r["actualRevision"] for r in final_nodes},
        "models": [m["name"] for m in final_models if m["status"] == "Installed"],
    })
    final_env["environmentFingerprint"] = fp
    final_result = {
        "packaged": True,
        "packageKind": "bootstrap",
        "nodes": missing_after,
        "nodeRepos": final_nodes,
        "models": final_models,
        "compatible": True,
        "incompatibilities": [],
        "environment": final_env,
        "environmentFingerprint": fp,
    }
    report_fn(final_result)
    available = not missing_after and all(m["status"] == "Installed" for m in final_models)
    stage(
        PHASES[6],
        "Completed" if available else "Failed",
        (
            "Bootstrap package installed and verified. Production readiness still requires a verified real video."
            if available else "Missing workflow nodes or models after installation."
        ),
    )
    return 0 if available else 2


def _execute_packaged(folder, config, api, idle, stage, report_fn, metrics_fn=None):
    root = root_path()
    info = api("/object_info")
    stage(PHASES[0], "Completed", str(root))

    unpacked = _unpack_remote_package(folder)
    if unpacked is not None:
        manifest = unpacked["manifest"]
        node_specs = unpacked["nodes"]
        model_specs = unpacked["models"]
        graph = config.get("graph") or unpacked["workflow"]
    else:
        manifest = config["manifest"]
        node_specs = config["nodes"]
        model_specs = config["models"]
        graph = config["graph"]

    def emit_metrics(**vals):
        if metrics_fn:
            metrics_fn(**vals)

    total_files = len(node_specs) + len(model_specs)
    emit_metrics(filesTotal=total_files, filesCompleted=0, modelsTotal=len(model_specs),
                 modelsCompleted=0, nodesTotal=len(node_specs), nodesCompleted=0,
                 downloadSpeedMiBs=0.0, currentFile="", filePercent=0.0)

    # Phase 1: Mandatory environment & compatibility preflight BEFORE any mutation
    stage(PHASES[1], "Running", "Probing ComfyUI revision, Python, PyTorch, CUDA, GPU, and disk")
    env = probe_environment(root, api)
    align_requested = bool(config["mode"] == "install" and config.get("alignComfyui") is True)
    result = scan_package(
        graph, info, root, manifest, node_specs, model_specs,
        env=env, verify_hashes=False, allow_comfyui_align=align_requested,
    )
    report_fn(result)
    if not result["compatible"]:
        stage(PHASES[1], "Failed", " · ".join(result["incompatibilities"]))
        return 3
    comfyui_aligned = False
    if align_requested and result.get("comfyuiMismatch"):
        idle()
        expected_rev = manifest["comfyui"]["revision"]
        stage(PHASES[1], "Running", f"Aligning ComfyUI to pinned revision {expected_rev[:12]}")
        comfyui_aligned = provision.align_comfyui_revision(root, manifest["comfyui"]["repo"], expected_rev)
        env = probe_environment(root, api)
        result = scan_package(graph, info, root, manifest, node_specs, model_specs, env=env, verify_hashes=False)
        report_fn(result)
        if not result["compatible"]:
            stage(PHASES[1], "Failed", " · ".join(result["incompatibilities"]))
            return 3
    stage(PHASES[1], "Completed", f"Environment {env['environmentFingerprint'][:12]} compatible")

    # Phase 2: Background dependency check summary
    done_nodes = sum(1 for r in result["nodeRepos"] if r["status"] == "Installed")
    done_models = sum(1 for m in result["models"] if m["status"] == "Installed")
    emit_metrics(filesTotal=total_files, filesCompleted=done_nodes + done_models,
                 modelsTotal=len(model_specs), modelsCompleted=done_models,
                 nodesTotal=len(node_specs), nodesCompleted=done_nodes,
                 downloadSpeedMiBs=0.0, currentFile="", filePercent=0.0)
    stage(PHASES[2], "Completed", f"{len(result['nodes'])} missing classes · {len(model_specs)} locked models")
    if config["mode"] == "scan":
        stage(PHASES[4], "Completed", "Checked locked package nodes, models, and environment compatibility")
        att_ok = result["attention"]["status"] in ("Installed", "SDPA", "Not required")
        all_models_ok = all(m["status"] == "Installed" for m in result["models"])
        all_nodes_ok = not result["nodes"] and all(r["status"] == "Installed" for r in result["nodeRepos"])
        available = all_nodes_ok and all_models_ok and att_ok
        stage(PHASES[6], "Completed" if available else "Failed",
              "All package dependencies verified on this environment" if available else "Package dependencies need installation")
        return 0 if available else 2

    # Install mode: reject any unexpected manual shell script
    if (folder / "install.sh").exists():
        raise ValueError("Manual Bash scripts are prohibited in packaged workflow installations.")
    idle()

    # Phase 3: Install strictly pinned custom nodes
    stage(PHASES[3], "Running", "Installing pinned custom nodes from nodes.json", 0, len(node_specs))
    installed_repos = provision.install_package_nodes(root, node_specs)
    done_nodes = len(node_specs)
    emit_metrics(filesTotal=total_files, filesCompleted=done_nodes + done_models,
                 modelsTotal=len(model_specs), modelsCompleted=done_models,
                 nodesTotal=len(node_specs), nodesCompleted=done_nodes,
                 downloadSpeedMiBs=0.0, currentFile="", filePercent=0.0)
    stage(PHASES[3], "Completed", f"{len(installed_repos)} repo(s) cloned, {len(node_specs)} verified", len(node_specs), len(node_specs))

    # Phase 4: Download and SHA-256 verify strictly locked models
    absent = [m for m in model_specs if provision.verify_model_file(root, m, check_hash=True)[0] == "Missing"]
    completed_models = len(model_specs) - len(absent)
    emit_metrics(filesTotal=total_files, filesCompleted=done_nodes + completed_models,
                 modelsTotal=len(model_specs), modelsCompleted=completed_models,
                 nodesTotal=len(node_specs), nodesCompleted=done_nodes,
                 downloadSpeedMiBs=0.0, currentFile="", filePercent=0.0)
    for index, m in enumerate(absent):
        rel = workflow_packages.expected_comfy_model_relpath(m)
        stage(PHASES[4], "Running", rel, index, len(absent))
        emit_metrics(filesTotal=total_files, filesCompleted=done_nodes + completed_models,
                     modelsTotal=len(model_specs), modelsCompleted=completed_models,
                     nodesTotal=len(node_specs), nodesCompleted=done_nodes,
                     downloadSpeedMiBs=0.0, currentFile=rel, filePercent=0.0)
        for item in result["models"]:
            if item["name"] == rel:
                item["status"] = "Downloading"
        report_fn(result)

        def on_progress(done_bytes, total_bytes, speed_mibs, _idx=index, _name=rel, _done_m=completed_models):
            pct = (done_bytes * 100.0 / total_bytes) if total_bytes else 100.0
            emit_metrics(
                filesTotal=total_files, filesCompleted=done_nodes + _done_m,
                modelsTotal=len(model_specs), modelsCompleted=_done_m,
                nodesTotal=len(node_specs), nodesCompleted=done_nodes,
                downloadSpeedMiBs=round(float(speed_mibs), 2),
                currentFile=_name, filePercent=round(pct, 1),
            )
            stage(
                PHASES[4], "Running",
                f"{_name} · {done_bytes/1e9:.2f}/{total_bytes/1e9:.2f} GB ({pct:.0f}%) · {speed_mibs:.1f} MiB/s",
                _idx, len(absent),
            )

        try:
            provision.install_package_models(root, [m], progress=on_progress)
        except (OSError, ValueError) as exc:
            for item in result["models"]:
                if item["name"] == rel:
                    item["status"] = "Failed"
                    item["error"] = redact(str(exc), os.environ.get("HF_TOKEN", ""))
            report_fn(result)
            raise
        completed_models += 1
        emit_metrics(filesTotal=total_files, filesCompleted=done_nodes + completed_models,
                     modelsTotal=len(model_specs), modelsCompleted=completed_models,
                     nodesTotal=len(node_specs), nodesCompleted=done_nodes,
                     downloadSpeedMiBs=0.0, currentFile=rel, filePercent=100.0)
        for item in result["models"]:
            if item["name"] == rel:
                item["status"] = "Installed"
                item["error"] = None
        report_fn(result)
    stage(PHASES[4], "Completed", "All locked models verified with SHA-256", len(absent), len(absent))

    # Attention backend handling (strictly governed by manifest.runtime.attention)
    att_cfg = manifest["runtime"]["attention"]
    sage_installed = False
    sage_fallback_used = False
    if att_cfg["backend"] == "sageattention" and not provision.sage_ready():
        idle()
        stage("SageAttention", "Running", f"Building pinned SageAttention ({att_cfg['revision'][:12]})")
        try:
            provision.install_sage(root, revision=att_cfg["revision"])
            sage_installed = True
            stage("SageAttention", "Completed", "Pinned build passed GPU check")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            if att_cfg.get("allowSdpaFallback") is True:
                sage_fallback_used = True
                print(f"[sage] WARN: Pinned build failed ({redact(str(exc))}); manifest explicitly authorizes SDPA fallback.", flush=True)
                stage("SageAttention", "Skipped", "Pinned CUDA build failed; using manifest-authorized SDPA fallback")
            else:
                stage("SageAttention", "Failed", f"Pinned SageAttention failed and manifest forbids SDPA fallback: {redact(str(exc))}")
                return 1

    # Phase 5: Restart ComfyUI when ComfyUI, custom nodes, or attention changed
    need_restart = bool(comfyui_aligned or installed_repos or sage_installed)
    if not need_restart:
        pre_check = scan_package(graph, api("/object_info"), root, manifest, node_specs, model_specs, env=env, verify_hashes=False)
        need_restart = any(r["status"] == "Needs restart" for r in pre_check["nodeRepos"]) or any(
            m["status"] == "Needs ComfyUI refresh" for m in pre_check["models"]
        )
    if need_restart:
        idle()
        stage(PHASES[5], "Running", "Restarting ComfyUI supervisor service")
        subprocess.run(["supervisorctl", "restart", "comfyui"], check=True, timeout=90)
        until = time.monotonic() + 120
        while True:
            try:
                info = api("/object_info")
                break
            except (OSError, ValueError):
                if time.monotonic() >= until:
                    raise
                time.sleep(2)
        stage(PHASES[5], "Completed")
    else:
        stage(PHASES[5], "Skipped", "ComfyUI restart was unnecessary")

    # Phase 6: Final strict verification (SHA-256 + /object_info + exact revisions)
    stage(PHASES[6], "Running", "Running final package verification")
    final_env = probe_environment(root, api)
    result = scan_package(graph, api("/object_info"), root, manifest, node_specs, model_specs, env=final_env, verify_hashes=True)
    if sage_fallback_used and att_cfg.get("allowSdpaFallback") is True and result["attention"]["status"] == "Missing":
        result["attention"]["status"] = "Fallback (SDPA — authorized by manifest)"
        result["sageAttention"] = "Fallback (SDPA — authorized by manifest)"
    # Compute full post-install environment fingerprint including verified node commits & model hashes
    result["environmentFingerprint"] = compute_verified_fingerprint(final_env, result)
    result["environment"]["environmentFingerprint"] = result["environmentFingerprint"]
    report_fn(result)

    att_ok = result["attention"]["status"] in ("Installed", "SDPA", "Not required", "Fallback (SDPA — authorized by manifest)")
    all_nodes_ok = not result["nodes"] and all(r["status"] == "Installed" for r in result["nodeRepos"])
    all_models_ok = all(m["status"] == "Installed" for m in result["models"])
    available = result["compatible"] and all_nodes_ok and all_models_ok and att_ok
    stage(
        PHASES[6],
        "Completed" if available else "Failed",
        (
            "Installation verified on this environment. Production readiness still requires a verified real video."
            if available else "Review unresolved package verification issues"
        ),
    )
    return 0 if available else 2


def execute(folder, config):
    port = config["port"]
    base = f"http://127.0.0.1:{port}"

    def api(path):
        with urlopen(base + path, timeout=10) as response:
            return json.load(response)

    def idle():
        q = api("/queue")
        if q["queue_running"] or q["queue_pending"]:
            raise ValueError("ComfyUI has an active or queued generation. Finish it before installation.")

    state = {"stages": {}, "report": {}, "metrics": {}}

    def stage(name, status, message="", count=None, total=None):
        old = state["stages"].get(name, {})
        state["stages"][name] = dict(
            status=status, message=redact(message),
            started=old.get("started", time.time()), updated=time.time(), count=count, total=total,
        )
        production.atomic(folder / "stage.json", state)

    def report_fn(result):
        state["report"] = result
        production.atomic(folder / "stage.json", state)

    def metrics_fn(**vals):
        state["metrics"].update(vals)
        production.atomic(folder / "stage.json", state)

    stage(PHASES[0], "Running")
    if config.get("packaged"):
        if config.get("packageKind") == "bootstrap" or (isinstance(config.get("manifest"), dict) and config["manifest"].get("entrypoint")):
            return _execute_bootstrap(folder, config, api, idle, stage, report_fn, metrics_fn=metrics_fn)
        return _execute_packaged(folder, config, api, idle, stage, report_fn, metrics_fn=metrics_fn)

    # Legacy non-packaged read-only scan support (no manual Bash execution permitted)
    if config.get("mode") != "scan":
        raise ValueError("Only packaged Workflow ZIPs (.zip) are supported for automated installation.")
    root = root_path()
    info = api("/object_info")
    stage(PHASES[0], "Completed", str(root))
    stage(PHASES[1], "Skipped", "Legacy profile")
    models_root = root / "models"

    def exists(name):
        path = Path(name)
        if path.is_absolute() or ".." in path.parts:
            return False
        return any(p.is_file() for p in models_root.glob("*/" + str(path))) if not any(c in name for c in "*?[]") else False

    graph = config["graph"]
    result = scan(graph, info, exists)
    if provision.requires_sage(graph):
        result["sageAttention"] = "Installed" if provision.sage_ready() else "Missing"
    result["downloadBytes"] = sum(m["source"]["bytes"] for m in result["models"] if m["status"] == "Missing" and m.get("source"))
    result["diskFreeBytes"] = shutil.disk_usage(models_root if models_root.is_dir() else root).free
    report_fn(result)
    stage(PHASES[2], "Completed", f"{len(result['nodes'])} missing")
    available = not result["nodes"] and all(m["status"] == "Installed" for m in result["models"])
    stage(PHASES[6], "Completed" if available else "Failed", "Legacy scan finished")
    return 0 if available else 2


def run(folder):
    try:
        with (folder / "started").open("x") as lock:
            lock.write(str(os.getpid()))
    except FileExistsError:
        return
    if (folder / "cancel").exists():
        production.atomic(folder / "result.json", dict(status="cancelled", exitCode=None, finished=time.time()))
        return
    config = production.read(folder / "spec.json")
    deadline = time.monotonic() + config["timeout"]
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    token = env.get("HF_TOKEN", "")
    status = "failed"
    child = subprocess.Popen(
        [sys.executable, __file__, str(folder), "execute"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, env=env,
    )
    with selectors.DefaultSelector() as monitor, (folder / "terminal.log").open("a", encoding="utf-8", buffering=1) as log:
        monitor.register(child.stdout, selectors.EVENT_READ)
        pending = b""
        try:
            while monitor.get_map():
                if (folder / "cancel").exists() or time.monotonic() >= deadline:
                    status = "cancelled" if (folder / "cancel").exists() else "timed out"
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        child.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        pass
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    break
                for key, _ in monitor.select(0.4):
                    data = os.read(key.fd, 65536)
                    if not data:
                        monitor.unregister(key.fileobj)
                        continue
                    pending += data
                    last = max(pending.rfind(b"\n"), pending.rfind(b"\r"))
                    if last != -1:
                        lines = pending[:last + 1].replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                        log.write(redact(lines.decode("utf-8", errors="replace"), token))
                        pending = pending[last + 1:]
                    if len(pending) > 1024 * 1024:
                        pending = b""
                        log.write("[Overlong terminal line omitted]\n")
            if pending:
                log.write(redact(pending.decode("utf-8", errors="replace"), token))
            code = child.wait(timeout=8)
            if status not in ("cancelled", "timed out"):
                if code == 0:
                    status = "installed"
                elif code == 2:
                    status = "needs attention"
                elif code == 3:
                    status = "incompatible"
                else:
                    status = "failed"
        except Exception:
            code = -1
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
    production.atomic(folder / "result.json", dict(status=status, exitCode=code, finished=time.time()))


def snapshot(folder):
    state = production.read(folder / "stage.json", {})
    state.update(production.read(folder / "result.json", {}) or {})
    claim = folder / "started"
    state["started"] = claim.is_file()
    if state["started"] and "status" not in state and time.time() - claim.stat().st_mtime > 10:
        try:
            pid = int(claim.read_text("ascii"))
            command = (Path("/proc") / str(pid) / "cmdline").read_bytes()
        except (OSError, ValueError):
            command = b""
        if os.fsencode(str(folder)) not in command:
            state.update(status="failed", finished=time.time(), exitCode=None)
    if (folder / "terminal.log").is_file():
        with (folder / "terminal.log").open("rb") as log:
            log.seek(max(0, log.seek(0, 2) - 60000))
            state["log"] = log.read().decode("utf-8", errors="replace")
    return state


def verify_live_environment(config):
    """Fast read-only pre-production recheck of host compatibility and environment fingerprint."""
    port = config["port"]
    base = f"http://127.0.0.1:{port}"

    def api(path):
        with urlopen(base + path, timeout=10) as response:
            return json.load(response)

    root = root_path()
    info = api("/object_info")
    env = probe_environment(root, api)
    manifest = config["manifest"]
    if config.get("packageKind") == "bootstrap" or (isinstance(manifest, dict) and manifest.get("entrypoint")):
        missing_classes = sorted({n["class_type"] for n in config["graph"].values()} - info.keys())
        models_root = root / "models"
        model_rows = [
            {
                "name": m.get("local") or workflow_packages.expected_comfy_model_relpath(m),
                "status": "Installed" if (models_root / (m.get("local") or workflow_packages.expected_comfy_model_relpath(m))).is_file() else "Missing",
            }
            for m in (config.get("models") or [])
        ]
        node_rows = [
            {
                "repo": n.get("repo") or n.get("name", ""),
                "actualRevision": provision.git_head(root / "custom_nodes" / (n.get("name") or str(n.get("repo", "")).split("/")[-1])),
                "status": "Installed" if (root / "custom_nodes" / (n.get("name") or str(n.get("repo", "")).split("/")[-1])).exists() else "Missing",
            }
            for n in (config.get("nodes") or [])
        ]
        fp = _canonical_hash({
            "base": env["environmentFingerprint"],
            "packageKind": "bootstrap",
            "nodes": {r["repo"]: r["actualRevision"] for r in node_rows},
            "models": [m["name"] for m in model_rows if m["status"] == "Installed"],
        })
        env["environmentFingerprint"] = fp
        return {
            "packaged": True,
            "packageKind": "bootstrap",
            "compatible": True,
            "incompatibilities": [],
            "nodes": missing_classes,
            "nodeRepos": node_rows,
            "models": model_rows,
            "environment": env,
            "environmentFingerprint": fp,
        }
    result = scan_package(
        config["graph"], info, root, manifest, config["nodes"], config["models"],
        env=env, verify_hashes=False,
    )
    att_cfg = manifest["runtime"]["attention"]
    if (att_cfg["backend"] == "sageattention" and att_cfg.get("allowSdpaFallback") is True
            and result["attention"]["status"] == "Missing"):
        result["attention"]["status"] = "Fallback (SDPA — authorized by manifest)"
        result["sageAttention"] = "Fallback (SDPA — authorized by manifest)"
    result["environmentFingerprint"] = compute_verified_fingerprint(env, result)
    result["environment"]["environmentFingerprint"] = result["environmentFingerprint"]
    return result


if __name__ == "__main__":
    work = Path(sys.argv[1])
    if len(sys.argv) > 2:
        if sys.argv[2] == "snapshot":
            print(json.dumps(snapshot(work)))
        elif sys.argv[2] == "verify-env":
            print(json.dumps(verify_live_environment(production.read(work / "spec.json"))))
        else:
            try:
                sys.exit(execute(work, production.read(work / "spec.json")))
            except Exception as exc:
                print(redact(str(exc), os.environ.get("HF_TOKEN", "")), file=sys.stderr)
                sys.exit(1)
    else:
        run(work)
