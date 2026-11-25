"""Strict validation, parsing, and building for self-contained AXDAR Workflow ZIP packages."""
import hashlib
import io
import json
import math
import re
import zipfile
import zlib
from pathlib import Path

from provision import MODEL_SUFFIXES

PACKAGE_SCHEMA_VERSION = 1
REQUIRED_FILES = frozenset({
    "manifest.json",
    "workflow.json",
    "nodes.json",
    "model-lock.json",
    "mapping.json",
})
MAX_ZIP_BYTES = 16 * 1024 * 1024
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED = 24 * 1024 * 1024

HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PKG_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
PINNED_REQ_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?==[A-Za-z0-9][A-Za-z0-9.+!_-]*$")
VERSION_RE = re.compile(r"^\d+(?:\.\d+){0,3}$")
CAPABILITY_RE = re.compile(r"^\d+\.\d+$")
PROTECTED_GPU_PACKAGES = frozenset({"torch", "torchvision", "torchaudio", "triton", "xformers"})


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _safe_relpath(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty relative path.")
    if "\\" in value or value.startswith("/") or any(c in value for c in "*?[]\x00\r\n"):
        raise ValueError(f"{label} contains forbidden characters: {value!r}.")
    parts = [p for p in value.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts) or "/".join(parts) != value:
        raise ValueError(f"{label} must be a normalized relative path without '..': {value!r}.")
    return value


def _validate_version_range(obj, label):
    if not isinstance(obj, dict) or "min" not in obj:
        raise ValueError(f"manifest.runtime.{label} must be an object with a 'min' version string.")
    lo = obj["min"]
    hi = obj.get("max")
    if not isinstance(lo, str) or not VERSION_RE.fullmatch(lo):
        raise ValueError(f"manifest.runtime.{label}.min must be a numeric version like '3.10' or '2.4.0'.")
    if hi is not None and (not isinstance(hi, str) or not VERSION_RE.fullmatch(hi)):
        raise ValueError(f"manifest.runtime.{label}.max must be a numeric version like '3.14' or '2.9.0'.")
    if hi is not None and parse_version_tuple(lo) > parse_version_tuple(hi):
        raise ValueError(f"manifest.runtime.{label}.min cannot exceed max.")
    return {"min": lo, **({"max": hi} if hi is not None else {})}


def parse_version_tuple(text):
    cleaned = str(text or "").strip().split("+", 1)[0]
    match = re.match(r"^(\d+(?:\.\d+){0,3})", cleaned)
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def version_in_range(actual, spec):
    actual_t = parse_version_tuple(actual)
    if not actual_t:
        return False
    min_t = parse_version_tuple(spec["min"])
    max_t = parse_version_tuple(spec["max"]) if spec.get("max") is not None else None
    width = max(len(actual_t), len(min_t), len(max_t or ()))
    pad = lambda t: t + (0,) * (width - len(t))
    if pad(actual_t) < pad(min_t):
        return False
    if max_t is not None and pad(actual_t) > pad(max_t):
        return False
    return True


def validate_manifest(data):
    if not isinstance(data, dict):
        raise ValueError("manifest.json must be a JSON object.")
    if data.get("schemaVersion") != PACKAGE_SCHEMA_VERSION:
        raise ValueError(f"manifest.json requires schemaVersion == {PACKAGE_SCHEMA_VERSION}.")
    pkg_id = data.get("id")
    if not isinstance(pkg_id, str) or not PKG_ID_RE.fullmatch(pkg_id):
        raise ValueError("manifest.json 'id' must be lowercase alphanumeric/hyphens (2–64 chars).")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("manifest.json 'name' must be a non-empty string.")
    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("manifest.json 'version' must be a non-empty string.")

    comfyui = data.get("comfyui")
    if not isinstance(comfyui, dict):
        raise ValueError("manifest.json must declare a pinned 'comfyui' object.")
    comfy_rev = comfyui.get("revision")
    if not isinstance(comfy_rev, str) or not HEX40_RE.fullmatch(comfy_rev):
        raise ValueError("manifest.json comfyui.revision must be a pinned 40-character lowercase Git commit SHA.")
    comfy_repo = comfyui.get("repo", "comfyanonymous/ComfyUI")
    if not isinstance(comfy_repo, str) or not REPO_RE.fullmatch(comfy_repo):
        raise ValueError("manifest.json comfyui.repo must be 'owner/repo'.")

    runtime = data.get("runtime")
    if not isinstance(runtime, dict):
        raise ValueError("manifest.json must declare 'runtime' compatibility requirements.")
    py_range = _validate_version_range(runtime.get("python"), "python")
    torch_range = _validate_version_range(runtime.get("torch"), "torch")
    cuda_range = _validate_version_range(runtime.get("cuda"), "cuda")
    min_vram = runtime.get("minVramGb")
    if not isinstance(min_vram, (int, float)) or isinstance(min_vram, bool) or not math.isfinite(min_vram) or min_vram < 0:
        raise ValueError("manifest.json runtime.minVramGb must be a non-negative number.")

    caps = runtime.get("computeCapabilities")
    if caps is not None:
        if not isinstance(caps, list) or not caps or any(not isinstance(c, str) or not CAPABILITY_RE.fullmatch(c) for c in caps):
            raise ValueError("manifest.json runtime.computeCapabilities must be a non-empty list of 'major.minor' strings.")

    attention = runtime.get("attention")
    if not isinstance(attention, dict):
        raise ValueError("manifest.json runtime.attention must be an explicit object.")
    backend = attention.get("backend")
    if backend not in ("none", "sdpa", "sageattention"):
        raise ValueError("manifest.json runtime.attention.backend must be 'none', 'sdpa', or 'sageattention'.")
    att_out = {"backend": backend}
    if backend == "sageattention":
        att_rev = attention.get("revision")
        if not isinstance(att_rev, str) or not HEX40_RE.fullmatch(att_rev):
            raise ValueError("manifest.json runtime.attention.revision must be a pinned 40-char Git commit SHA for sageattention.")
        allow_fb = attention.get("allowSdpaFallback")
        if not isinstance(allow_fb, bool):
            raise ValueError("manifest.json runtime.attention.allowSdpaFallback must be an explicit boolean (true or false).")
        att_out["revision"] = att_rev
        att_out["allowSdpaFallback"] = allow_fb
    else:
        att_out["allowSdpaFallback"] = False

    return {
        "schemaVersion": PACKAGE_SCHEMA_VERSION,
        "id": pkg_id,
        "name": name.strip(),
        "version": version.strip(),
        "description": str(data.get("description") or "").strip(),
        "comfyui": {"repo": comfy_repo, "revision": comfy_rev},
        "runtime": {
            "python": py_range,
            "torch": torch_range,
            "cuda": cuda_range,
            "minVramGb": min_vram,
            **({"computeCapabilities": list(caps)} if caps is not None else {}),
            "attention": att_out,
        },
    }


def validate_workflow_graph(data):
    if not isinstance(data, dict) or not data:
        raise ValueError("workflow.json must be a non-empty ComfyUI API workflow JSON object.")
    if "nodes" in data and isinstance(data["nodes"], list):
        raise ValueError("workflow.json must be executable ComfyUI API JSON, not ComfyUI UI/editor JSON.")
    missing_ct = []
    for nid, node in data.items():
        if not isinstance(nid, str) or not isinstance(node, dict):
            raise ValueError("workflow.json entries must map string node IDs to node objects.")
        ct = node.get("class_type")
        if not isinstance(ct, str) or not ct.strip():
            missing_ct.append(nid)
        if not isinstance(node.get("inputs"), dict):
            raise ValueError(f"workflow.json node {nid!r} is missing an 'inputs' object.")
    if missing_ct:
        raise ValueError("workflow.json is missing class_type for nodes: " + ", ".join(missing_ct) + ".")
    return data


def validate_nodes(data):
    if not isinstance(data, list):
        raise ValueError("nodes.json must be a JSON array of pinned custom node repositories.")
    seen_repos = set()
    seen_classes = set()
    normalized = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"nodes.json entry #{idx + 1} must be an object.")
        repo = item.get("repo")
        if not isinstance(repo, str) or not REPO_RE.fullmatch(repo):
            raise ValueError(f"nodes.json entry #{idx + 1} has invalid repo {repo!r} (expected 'owner/repo').")
        if repo.lower() in seen_repos:
            raise ValueError(f"nodes.json contains duplicate repo {repo!r}.")
        seen_repos.add(repo.lower())
        rev = item.get("revision")
        if not isinstance(rev, str) or not HEX40_RE.fullmatch(rev):
            raise ValueError(f"nodes.json repo {repo!r} must pin a 40-character lowercase Git commit SHA.")
        classes = item.get("classes")
        if not isinstance(classes, list) or not classes or any(not isinstance(c, str) or not c.strip() for c in classes):
            raise ValueError(f"nodes.json repo {repo!r} must list its provided ComfyUI node classes.")
        for cls in classes:
            if cls in seen_classes:
                raise ValueError(f"nodes.json declares class {cls!r} in more than one repository.")
            seen_classes.add(cls)
        reqs = item.get("requirements", [])
        if not isinstance(reqs, list) or any(not isinstance(r, str) for r in reqs):
            raise ValueError(f"nodes.json repo {repo!r} requirements must be a list of pinned 'pkg==version' strings.")
        clean_reqs = []
        for req in reqs:
            req = req.strip()
            if not PINNED_REQ_RE.fullmatch(req):
                raise ValueError(f"nodes.json repo {repo!r} has unpinned Python requirement {req!r}; use exact 'pkg==version'.")
            pkg_name = req.split("==", 1)[0].split("[", 1)[0].lower().replace("_", "-")
            if pkg_name in PROTECTED_GPU_PACKAGES or pkg_name.startswith(("nvidia-", "cuda-")):
                raise ValueError(f"nodes.json repo {repo!r} must not override base GPU runtime package {req!r}.")
            clean_reqs.append(req)
        normalized.append({
            "repo": repo,
            "revision": rev,
            "classes": list(dict.fromkeys(classes)),
            "requirements": clean_reqs,
        })
    return normalized


def expected_comfy_model_relpath(model_spec):
    folder_parts = model_spec["folder"].split("/")
    filename = Path(model_spec["file"]).name
    return "/".join([*folder_parts[1:], filename]) if len(folder_parts) > 1 else filename


def validate_model_lock(data, graph):
    if not isinstance(data, list):
        raise ValueError("model-lock.json must be a JSON array.")
    seen_targets = set()
    normalized = []
    by_relpath = {}
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"model-lock.json entry #{idx + 1} must be an object.")
        folder = _safe_relpath(item.get("folder"), f"model-lock.json[{idx}].folder")
        repo = item.get("repo")
        if not isinstance(repo, str) or not REPO_RE.fullmatch(repo):
            raise ValueError(f"model-lock.json[{idx}].repo must be 'owner/repo'.")
        rev = item.get("revision")
        if not isinstance(rev, str) or not HEX40_RE.fullmatch(rev):
            raise ValueError(f"model-lock.json[{idx}].revision must be a pinned 40-char commit SHA (not 'main').")
        remote_file = _safe_relpath(item.get("file"), f"model-lock.json[{idx}].file")
        sha256 = item.get("sha256")
        if not isinstance(sha256, str) or not HEX64_RE.fullmatch(sha256):
            raise ValueError(f"model-lock.json[{idx}].sha256 must be a 64-char lowercase SHA-256 hex digest.")
        size = item.get("bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ValueError(f"model-lock.json[{idx}].bytes must be a positive integer.")
        target = folder + "/" + Path(remote_file).name
        if target.lower() in seen_targets:
            raise ValueError(f"model-lock.json contains duplicate target path {target!r}.")
        seen_targets.add(target.lower())
        entry = {
            "folder": folder,
            "repo": repo,
            "file": remote_file,
            "revision": rev,
            "sha256": sha256,
            "bytes": size,
        }
        normalized.append(entry)
        by_relpath[expected_comfy_model_relpath(entry)] = entry

    # Ensure every model filename referenced in workflow.json uses POSIX slashes and is explicitly locked.
    for nid, node in graph.items():
        for key, value in node.get("inputs", {}).items():
            if not isinstance(value, str) or not value.lower().endswith(MODEL_SUFFIXES):
                continue
            if "\\" in value:
                raise ValueError(
                    f"workflow.json node {nid}.{key} uses a Windows backslash path ({value!r}); "
                    "use POSIX forward slashes ('/') for Linux ComfyUI compatibility."
                )
            rel = value.strip("/")
            if rel not in by_relpath:
                raise ValueError(
                    f"workflow.json node {nid}.{key} references model {value!r} which is not locked in model-lock.json "
                    f"(expected matching folder/file entry for {rel!r})."
                )
    return normalized


def validate_mapping(data, graph):
    if not isinstance(data, dict):
        raise ValueError("mapping.json must be a JSON object.")
    output = data.get("output")
    if not isinstance(output, str) or output not in graph:
        raise ValueError("mapping.json must specify a valid 'output' node ID present in workflow.json.")
    positive = data.get("positive")
    if not isinstance(positive, str) or "." not in positive:
        raise ValueError("mapping.json must specify an explicit 'positive' binding ('<nodeId>.<inputKey>').")
    for role in ("positive", "negative", "duration", "frames"):
        val = data.get(role, "")
        if val is None:
            val = ""
        if not isinstance(val, str):
            raise ValueError(f"mapping.json '{role}' must be a string.")
        if val:
            if "." not in val:
                raise ValueError(f"mapping.json '{role}' must have format '<nodeId>.<inputKey>'.")
            nid, key = val.split(".", 1)
            if nid not in graph:
                raise ValueError(f"mapping.json '{role}' references missing node {nid!r}.")
            if key not in graph[nid].get("inputs", {}):
                raise ValueError(f"mapping.json '{role}' references missing input {val!r}.")
    if sum(bool(data.get(k)) for k in ("duration", "frames", "fixedSeconds")) != 1:
        raise ValueError("mapping.json must declare exactly one timing control: 'duration', 'frames', or 'fixedSeconds'.")
    for role in ("images", "audio"):
        items = data.get(role, [])
        if not isinstance(items, list) or any(not isinstance(x, str) or "." not in x for x in items):
            raise ValueError(f"mapping.json '{role}' must be a list of '<nodeId>.<inputKey>' strings.")
        for fid in items:
            nid, _key = fid.split(".", 1)
            if nid not in graph:
                raise ValueError(f"mapping.json '{role}' references missing node {nid!r}.")
    params = data.get("parameters", {})
    if not isinstance(params, dict):
        raise ValueError("mapping.json 'parameters' must be an object.")
    for fid in params:
        if not isinstance(fid, str) or "." not in fid:
            raise ValueError(f"mapping.json parameter key {fid!r} must have format '<nodeId>.<inputKey>'.")
        nid, key = fid.split(".", 1)
        if nid not in graph or key not in graph[nid].get("inputs", {}):
            raise ValueError(f"mapping.json parameter {fid!r} references missing workflow input.")
    return dict(
        positive=positive,
        negative=data.get("negative") or "",
        duration=data.get("duration") or "",
        frames=data.get("frames") or "",
        images=list(data.get("images") or []),
        audio=list(data.get("audio") or []),
        output=output,
        parameters=dict(params),
        **{k: data[k] for k in ("fps", "fixedSeconds", "minSeconds", "maxSeconds", "minFrames", "maxFrames", "frameStep", "frameBase", "promptPrefix") if k in data and data[k] is not None},
    )


def _bind_input_ref(spec, label):
    if isinstance(spec, str) and "." in spec:
        return spec
    if isinstance(spec, dict) and isinstance(spec.get("node"), (str, int)) and isinstance(spec.get("field"), str) and spec["field"].strip():
        return f"{spec['node']}.{spec['field'].strip()}"
    raise ValueError(f"manifest.json inputs.{label} must specify {{'node': '<id>', 'field': '<name>'}} or '<id>.<name>'.")


def _parse_bootstrap_package(manifest_raw, rel_members, filename):
    if not isinstance(manifest_raw, dict):
        raise ValueError("manifest.json must be a JSON object.")
    if manifest_raw.get("schema_version") != PACKAGE_SCHEMA_VERSION:
        raise ValueError(f"manifest.json requires schema_version == {PACKAGE_SCHEMA_VERSION}.")
    pkg_id = manifest_raw.get("package_id")
    if not isinstance(pkg_id, str) or not PKG_ID_RE.fullmatch(pkg_id):
        raise ValueError("manifest.json 'package_id' must be lowercase alphanumeric/hyphens (2–64 chars).")
    version = manifest_raw.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("manifest.json 'version' must be a non-empty string.")
    entrypoint = _safe_relpath(manifest_raw.get("entrypoint"), "manifest.json entrypoint")
    if not entrypoint.lower().endswith(".sh"):
        raise ValueError("manifest.json entrypoint must be a shell script (e.g. 'install.sh').")
    if entrypoint not in rel_members:
        raise ValueError(f"Workflow Package is missing entrypoint {entrypoint!r} declared in manifest.json.")
    wf_rel = _safe_relpath(manifest_raw.get("workflow_api") or "workflow_api.json", "manifest.json workflow_api")
    if wf_rel not in rel_members:
        raise ValueError(f"Workflow Package is missing workflow API file {wf_rel!r} declared in manifest.json.")

    def load_member_json(rel):
        try:
            return json.loads(rel_members[rel].decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{rel} is not valid UTF-8 JSON: {exc}") from None

    workflow = validate_workflow_graph(load_member_json(wf_rel))
    output_node = manifest_raw.get("output_node")
    if not isinstance(output_node, (str, int)) or str(output_node) not in workflow:
        raise ValueError("manifest.json 'output_node' must reference a valid node ID in workflow_api.")
    output_node = str(output_node)

    inputs = manifest_raw.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError("manifest.json must declare an 'inputs' mapping object.")

    mapping_raw = {"output": output_node, "images": [], "audio": [], "parameters": {}}
    if "prompt" in inputs or "positive" in inputs:
        mapping_raw["positive"] = _bind_input_ref(inputs.get("prompt") or inputs.get("positive"), "prompt")
    if "negative" in inputs or "negative_prompt" in inputs:
        mapping_raw["negative"] = _bind_input_ref(inputs.get("negative") or inputs.get("negative_prompt"), "negative")
    if "duration" in inputs:
        mapping_raw["duration"] = _bind_input_ref(inputs["duration"], "duration")
    if "frames" in inputs:
        mapping_raw["frames"] = _bind_input_ref(inputs["frames"], "frames")
    if "fixedSeconds" in manifest_raw:
        mapping_raw["fixedSeconds"] = manifest_raw["fixedSeconds"]

    img_spec = inputs.get("images") if "images" in inputs else inputs.get("image_paths")
    if img_spec is not None:
        if isinstance(img_spec, list):
            mapping_raw["images"] = [_bind_input_ref(item, "images") for item in img_spec]
        else:
            mapping_raw["images"] = [_bind_input_ref(img_spec, "image_paths")]

    aud_spec = inputs.get("audio") if "audio" in inputs else inputs.get("audio_paths")
    if aud_spec is not None:
        if isinstance(aud_spec, list):
            mapping_raw["audio"] = [_bind_input_ref(item, "audio") for item in aud_spec]
        else:
            mapping_raw["audio"] = [_bind_input_ref(aud_spec, "audio")]

    fps = manifest_raw.get("fps")
    if fps is None:
        out_in = workflow[output_node].get("inputs", {})
        for key in ("frame_rate", "fps"):
            cand = out_in.get(key)
            if isinstance(cand, (int, float)) and not isinstance(cand, bool) and math.isfinite(cand) and cand > 0:
                fps = cand
                break
    if fps is not None:
        mapping_raw["fps"] = fps
    for opt in ("minSeconds", "maxSeconds", "minFrames", "maxFrames", "frameStep", "frameBase", "promptPrefix"):
        if opt in manifest_raw and manifest_raw[opt] is not None:
            mapping_raw[opt] = manifest_raw[opt]
    mapping = validate_mapping(mapping_raw, workflow)

    nodes = []
    if "nodes.lock.json" in rel_members:
        raw_nodes = load_member_json("nodes.lock.json")
        if not isinstance(raw_nodes, list):
            raise ValueError("nodes.lock.json must be a JSON array.")
        for idx, item in enumerate(raw_nodes):
            if not isinstance(item, dict):
                raise ValueError(f"nodes.lock.json entry #{idx + 1} must be an object.")
            url = str(item.get("url") or "").strip()
            name = str(item.get("name") or "").strip()
            repo = str(item.get("repo") or "").strip()
            if not repo and "github.com/" in url:
                repo = url.split("github.com/", 1)[1].removesuffix(".git").strip("/")
            nodes.append({
                "name": name or (repo.split("/")[-1] if repo else f"node-{idx + 1}"),
                "repo": repo or name,
                "url": url,
                "revision": str(item.get("ref") or item.get("revision") or ""),
                "classes": list(item.get("classes") or []),
                "requirements": list(item.get("requirements") or []),
            })

    models = []
    if "models.lock.json" in rel_members:
        raw_models = load_member_json("models.lock.json")
        if not isinstance(raw_models, list):
            raise ValueError("models.lock.json must be a JSON array.")
        for idx, item in enumerate(raw_models):
            if not isinstance(item, dict):
                raise ValueError(f"models.lock.json entry #{idx + 1} must be an object.")
            local_path = _safe_relpath(item.get("local") or item.get("file"), f"models.lock.json[{idx}].local")
            folder = local_path.rsplit("/", 1)[0] if "/" in local_path else ""
            remote_file = _safe_relpath(item.get("remote") or item.get("file") or local_path, f"models.lock.json[{idx}].remote")
            size = item.get("bytes", 0)
            size = int(size) if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else 0
            models.append({
                "folder": folder,
                "local": local_path,
                "repo": str(item.get("repo") or ""),
                "file": remote_file,
                "revision": str(item.get("revision") or ""),
                "sha256": str(item.get("sha256") or ""),
                "bytes": size,
            })

    display_name = str(manifest_raw.get("name") or f"{pkg_id} ({version})").strip()
    manifest = dict(manifest_raw)
    manifest.update({
        "id": pkg_id,
        "name": display_name,
        "version": version.strip(),
        "entrypoint": entrypoint,
        "workflow_api": wf_rel,
        "packageKind": "bootstrap",
    })
    package_hash = _sha256(_canonical_json({rel: _sha256(data) for rel, data in sorted(rel_members.items())}))
    return {
        "packageKind": "bootstrap",
        "filename": str(filename or "workflow.zip"),
        "packageHash": package_hash,
        "manifest": manifest,
        "workflow": workflow,
        "nodes": nodes,
        "models": models,
        "mapping": mapping,
        "entrypoint": entrypoint,
        "files": sorted(rel_members),
    }


def parse_package(raw_zip_bytes, filename="workflow.zip"):
    if not isinstance(raw_zip_bytes, (bytes, bytearray)) or not raw_zip_bytes:
        raise ValueError("Upload a non-empty Workflow Package (.zip) file.")
    if len(raw_zip_bytes) > MAX_ZIP_BYTES:
        raise ValueError("Workflow Package (.zip) exceeds the 16 MB size limit.")
    if not zipfile.is_zipfile(io.BytesIO(raw_zip_bytes)):
        raise ValueError("Standalone JSON workflows are not accepted. Import a packaged Workflow ZIP (.zip) containing manifest.json.")

    raw_members = {}
    total_size = 0
    try:
        with zipfile.ZipFile(io.BytesIO(raw_zip_bytes), "r") as zf:
            bad = zf.testzip()
            if bad is not None:
                raise ValueError(f"Workflow Package ZIP archive is corrupted (CRC error in {bad!r}).")
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace("\\", "/")
                # Reject absolute paths, drive letters, traversal, control chars, symlinks
                if (name.startswith("/") or re.match(r"^[A-Za-z]:", name)
                        or ".." in name.split("/") or any(c in name for c in "\x00\r\n")):
                    raise ValueError(f"Unsafe path in Workflow Package: {info.filename!r}.")
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError(f"Symbolic links are not permitted in Workflow Packages: {info.filename!r}.")
                if info.file_size > MAX_MEMBER_BYTES:
                    raise ValueError(f"File {name!r} inside Workflow Package exceeds size limit.")
                total_size += info.file_size
                if total_size > MAX_TOTAL_UNCOMPRESSED:
                    raise ValueError("Uncompressed Workflow Package exceeds size limit.")
                parts = [p for p in name.split("/") if p]
                if not parts or any(p in (".", "..") for p in parts):
                    raise ValueError(f"Unsafe path in Workflow Package: {info.filename!r}.")
                norm = "/".join(parts)
                if parts[-1].lower() == "models.txt":
                    raise ValueError("models.txt is not accepted as a production dependency manifest. Provide a locked model manifest.")
                if norm in raw_members:
                    raise ValueError(f"Duplicate file {norm!r} inside Workflow Package.")
                raw_members[norm] = zf.read(info)
    except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
        raise ValueError(f"Workflow Package ZIP archive is invalid or corrupted: {exc}") from None

    if not raw_members:
        raise ValueError("Workflow Package ZIP archive is empty.")

    # Normalize a single shared top-level directory wrapper if manifest.json is inside it
    rel_members = raw_members
    if "manifest.json" not in raw_members:
        tops = {k.split("/", 1)[0] for k in raw_members if "/" in k}
        if len(tops) == 1 and all("/" in k for k in raw_members):
            prefix = next(iter(tops)) + "/"
            stripped = {k[len(prefix):]: v for k, v in raw_members.items()}
            if "manifest.json" in stripped:
                rel_members = stripped

    if "manifest.json" not in rel_members:
        raise ValueError("Workflow Package is missing mandatory file(s): manifest.json.")

    try:
        manifest_raw = json.loads(rel_members["manifest.json"].decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"manifest.json is not valid UTF-8 JSON: {exc}") from None

    if isinstance(manifest_raw, dict) and ("entrypoint" in manifest_raw or ("schema_version" in manifest_raw and "schemaVersion" not in manifest_raw)):
        return _parse_bootstrap_package(manifest_raw, rel_members, filename)

    # Strict 5-file declarative package validation
    members = {}
    for rel, content in rel_members.items():
        if "/" in rel:
            raise ValueError(f"Unexpected nested path in Workflow Package: {rel!r}.")
        if rel.lower().endswith(".sh"):
            raise ValueError("Shell scripts (.sh) are not permitted inside declarative 5-file Workflow Packages.")
        members[rel] = content

    missing = sorted(REQUIRED_FILES - set(members))
    if missing:
        raise ValueError("Workflow Package is missing mandatory file(s): " + ", ".join(missing) + ".")

    extra = sorted(set(members) - REQUIRED_FILES)
    if extra:
        raise ValueError("Workflow Package contains unexpected file(s): " + ", ".join(extra) + ". Only the 5 mandatory package files are permitted.")

    def load_json(fname):
        try:
            return json.loads(members[fname].decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{fname} is not valid UTF-8 JSON: {exc}") from None

    manifest = validate_manifest(manifest_raw)
    workflow = validate_workflow_graph(load_json("workflow.json"))
    nodes = validate_nodes(load_json("nodes.json"))
    models = validate_model_lock(load_json("model-lock.json"), workflow)
    mapping = validate_mapping(load_json("mapping.json"), workflow)

    package_hash = _sha256(_canonical_json({
        "manifest": manifest,
        "workflow": workflow,
        "nodes": nodes,
        "models": models,
        "mapping": mapping,
    }))
    return {
        "packageKind": "declarative",
        "filename": str(filename or "workflow.zip"),
        "packageHash": package_hash,
        "manifest": manifest,
        "workflow": workflow,
        "nodes": nodes,
        "models": models,
        "mapping": mapping,
    }


def build_package_bytes(manifest, workflow, nodes, models, mapping):
    """Build a deterministic, validated .zip Workflow Package in memory."""
    buf = io.BytesIO()
    files = {
        "manifest.json": manifest,
        "workflow.json": workflow,
        "nodes.json": nodes,
        "model-lock.json": models,
        "mapping.json": mapping,
    }
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in sorted(files):
            info = zipfile.ZipInfo(fname, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, json.dumps(files[fname], ensure_ascii=False, indent=2) + "\n")
    raw = buf.getvalue()
    # Self-validate before returning
    parse_package(raw, "package.zip")
    return raw
