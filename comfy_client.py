"""Standalone ComfyUI HTTP client, reference upload, shot preparation, and resumable MP4 download."""
import json
import re
import shutil
import uuid
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import production
import workflow_profiles

PHOTOS = {".jpg", ".jpeg", ".png", ".webp", ".avif"}
VOICES = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}


def generation_images(shot):
    return list(dict.fromkeys(
        name for name in [shot["image"], *shot["refs"]]
        if name and Path(name).suffix.lower() in PHOTOS
    ))


def generation_voices(shot):
    return list(dict.fromkeys(
        name for name in shot["refs"]
        if Path(name).suffix.lower() in VOICES
    ))


def prompt(shot):
    return "\n".join(x for x in [
        shot["prompt"],
        *[f"<Audio {i}> is the voice reference for {Path(name).stem}." for i, name in enumerate(generation_voices(shot), 1)],
        shot["change"],
    ] if x)


def remote(url, path, body=None, headers=None):
    try:
        return urlopen(Request(url + path, data=body, headers=headers or {}), timeout=45)
    except HTTPError as exc:
        if exc.code in (408, 429, 500, 502, 503, 504):
            raise
        raise ValueError(f"ComfyUI {exc.code}: {exc.read(1800).decode(errors='replace')}") from exc


def json_api(url, path, obj=None, *, remote_fn=None):
    call = remote_fn or remote
    body = json.dumps(obj).encode() if obj is not None else None
    with call(url, path, body, {"Content-Type": "application/json"}) as response:
        return json.load(response)


def reference_available(url, asset, name):
    path = "/view?" + urlencode({"filename": name, "type": "input"})
    try:
        with urlopen(Request(url + path, method="HEAD"), timeout=6) as existing:
            return existing.headers.get("Content-Length") == str(asset.stat().st_size)
    except HTTPError as exc:
        exc.close()
    except (OSError, HTTPException):
        pass
    return False


def upload(url, asset, name, *, fallback_stage=None, remote_fn=None, available_fn=None):
    is_available = available_fn or reference_available
    call = remote_fn or remote
    if is_available(url, asset, name):
        return name
    boundary = uuid.uuid4().hex
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode()
        + asset.read_bytes()
        + f"\r\n--{boundary}--\r\n".encode()
    )
    try:
        with call(url, "/upload/image", body, {"Content-Type": "multipart/form-data; boundary=" + boundary}) as response:
            info = json.load(response)
    except (OSError, HTTPException):
        if fallback_stage is None:
            raise
        fallback_stage(url, asset, name)
        if not is_available(url, asset, name):
            raise ConnectionError("ComfyUI has not confirmed the uploaded reference.")
        return name
    return "/".join(filter(None, [info.get("subfolder"), info["name"]]))


def prepare_shot(url, folder, eid, shot, wf, record, *, asset_map, upload_fn=None, json_api_fn=None):
    images, voices = generation_images(shot), generation_voices(shot)
    for name in dict.fromkeys([*images, *voices]):
        if name not in asset_map or Path(name).name != name:
            raise ValueError("Missing asset: " + name)
        shutil.copy2(asset_map[name], folder / name)
    do_upload = upload_fn or upload
    do_api = json_api_fn or json_api

    def upload_reference(name):
        asset = folder / name
        uploaded = "axdar_" + production.digest(asset) + asset.suffix.lower()
        return do_upload(url, asset, uploaded)

    return workflow_profiles.runtime_graph(
        wf, shot, record["prompt"], images, voices,
        upload_reference,
        "AXDAR/" + eid + "/" + shot["id"] + "/" + folder.name,
        model_info=lambda kind: do_api(url, "/object_info/" + quote(kind, safe="")),
    )


def download_output(url, folder, history, record, *, remote_fn=None):
    call = remote_fn or remote
    outputs = history.get("outputs", {}).get(str(record["output_node"]), {})
    video = next(
        (
            v for group in outputs.values() if isinstance(group, list)
            for v in group if isinstance(v, dict) and v.get("filename", "").lower().endswith(".mp4")
        ),
        None,
    )
    if not video:
        raise ValueError("No MP4 returned from the final output node.")
    path = "/view?" + urlencode({k: video[k] for k in ("filename", "subfolder", "type") if k in video})
    part = folder / "video.part"
    resume = folder / "video.resume.json"
    previous = production.read(resume, {})
    tag = previous.get("etag") if previous.get("path") == path else None
    offset = part.stat().st_size if tag and part.is_file() else 0
    headers = {"Range": f"bytes={offset}-", "If-Range": tag} if offset else None
    try:
        src = call(url, path, headers=headers)
    except ValueError as exc:
        if not offset or not isinstance(exc.__cause__, HTTPError) or exc.__cause__.code != 416:
            raise
        src = call(url, path)

    expected = None
    if src.status == 206:
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", src.headers.get("Content-Range", ""))
        if offset and src.headers.get("ETag") == tag and match:
            start, end, total = map(int, match.groups())
            if start == offset and start <= end == total - 1:
                expected = total
        if expected is None:
            src.close()
            src = call(url, path)
    with src:
        if expected is None:
            if src.status != 200:
                raise ConnectionError("ComfyUI did not return a complete video or a valid resume range.")
            offset = 0
            length = src.headers.get("Content-Length", "")
            expected = int(length) if length.isdecimal() else None
        with part.open("ab" if offset else "wb") as out:
            tag = src.headers.get("ETag", "")
            if tag.startswith('"'):
                production.atomic(resume, {"path": path, "etag": tag})
            else:
                resume.unlink(missing_ok=True)
            shutil.copyfileobj(src, out)
    size = part.stat().st_size
    if not size or expected is not None and size != expected:
        raise ConnectionError("The ComfyUI video download ended before all bytes arrived.")
