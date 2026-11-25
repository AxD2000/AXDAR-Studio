"""Pure Vast API and verified OpenSSH transport primitives (zero cross-module imports)."""
import hashlib
import ipaddress
import json
import re
import subprocess
import threading
import uuid
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from vast_credentials import SSH_CONNECT_SECONDS, api_key, identity_options

STORE = "/workspace/axdar-studio"
_ssh_account_synced = False
_ssh_sync_lock = threading.Lock()


def sync_account_ssh_key_async(force=False):
    """Best-effort background sync of AXDAR's local SSH public key to the Vast account."""
    global _ssh_account_synced
    with _ssh_sync_lock:
        if _ssh_account_synced and not force:
            return
        _ssh_account_synced = True

    def _run():
        global _ssh_account_synced
        try:
            from vast_ssh_setup import ensure_account_ssh_key
            ensure_account_ssh_key()
        except Exception:
            with _ssh_sync_lock:
                _ssh_account_synced = False

    threading.Thread(target=_run, daemon=True).start()


def _digest(path):
    with path.open("rb") as stream:
        if hasattr(hashlib, "file_digest"):
            return hashlib.file_digest(stream, "sha256").hexdigest()
        h = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
        return h.hexdigest()


def instance_target(row):
    host, port = row.get("ssh_host"), row.get("ssh_port")
    mappings = (row.get("ports") or {}).get("22/tcp", [])
    if row.get("public_ipaddr") and mappings:
        try:
            direct_host = str(ipaddress.ip_address(row["public_ipaddr"]))
            direct_port = int(mappings[0]["HostPort"])
            if ":" not in direct_host and 1 <= direct_port <= 65535:
                host, port = direct_host, direct_port
        except (ValueError, KeyError, TypeError, IndexError):
            pass
    return dict(
        id=row["id"],
        status=row.get("actual_status"),
        host=host,
        port=port,
        rate=row.get("dph_total"),
        gpu=row.get("gpu_name", "GPU"),
        uptime=row.get("uptime_mins"),
    )


def instances():
    key = api_key()
    if not key:
        raise ValueError("Save your Vast API key in the Vast connection panel first.")
    params = {"limit": 25}
    result = []
    while True:
        req = Request("https://console.vast.ai/api/v1/instances/?" + urlencode(params), headers={"Authorization": "Bearer " + key})
        with urlopen(req, timeout=15) as stream:
            data = json.load(stream)
        for row in data["instances"]:
            result.append(instance_target(row))
        if not data.get("next_token"):
            sync_account_ssh_key_async()
            return result
        params["after_token"] = data["next_token"]


def ssh(target, command, *, data=None, stream=None, out=None, timeout=70):
    host = str(target["host"])
    port = int(target["port"])
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", host) or not 1 <= port <= 65535:
        raise ValueError("Invalid SSH host or port")
    args = [
        "ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", f"ConnectTimeout={SSH_CONNECT_SECONDS}",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
        *identity_options(target), "-p", str(port), "root@" + host, command,
    ]
    p = subprocess.run(
        args, input=data, stdin=stream, stdout=out or subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if p.returncode:
        raise OSError(p.stderr.decode(errors="replace").strip()[-550:] or "Remote command failed")
    return p.stdout


def put(target, path, local):
    sha = path.rsplit("/", 1)[1] if path.startswith(STORE + "/blobs/") else _digest(local)
    tmp = path + "." + uuid.uuid4().hex + ".part"
    with local.open("rb") as src:
        ssh(target, f"umask 077; cat > {tmp} && echo '{sha}  {tmp}' | sha256sum -c --status && mv {tmp} {path}", stream=src, timeout=240)


def put_json(target, path, data):
    tmp = path + "." + uuid.uuid4().hex + ".tmp"
    ssh(target, f"umask 077; cat > {tmp} && mv {tmp} {path}", data=json.dumps(data).encode())


def stage_reference(target, asset, name):
    if not re.fullmatch(r"axdar_[0-9a-f]{64}\.[a-z0-9]{1,8}", name):
        raise ValueError("Invalid reference filename.")
    roots = ("/workspace/ComfyUI", "/opt/ComfyUI", "/root/ComfyUI")
    search = (
        "for root in " + " ".join(roots) + "; do "
        'if test -f "$root/main.py" && test -d "$root/input"; then printf "%s" "$root"; exit; fi; done'
    )
    root = ssh(target, search, timeout=45).decode().strip()
    if root not in roots:
        raise ConnectionError("The original ComfyUI input folder could not be confirmed over SSH.")
    put(target, root + "/input/" + name, asset)
    return name


def deploy_files(target, planned):
    manifest = "".join(f"{_digest(src)}  {dst}\n" for dst, src in planned.items())
    check = ssh(
        target,
        'while read -r hash path; do echo "$hash  $path" | sha256sum -c --status 2>/dev/null || echo "$path"; done',
        data=manifest.encode(),
    )
    for dst in check.decode().splitlines():
        if dst not in planned:
            raise ValueError("Unexpected checksum response from SSH host")
        if dst.startswith(STORE + "/blobs/") and _digest(planned[dst]) != dst.rsplit("/", 1)[1]:
            raise ValueError("An asset changed during deployment. Start a new snapshot.")
        put(target, dst, planned[dst])
