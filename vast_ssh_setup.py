"""Enroll a Vast SSH endpoint through the account API and authenticated Jupyter TLS."""
import base64
import ipaddress
import json
import re
import ssl
import subprocess
import time
import uuid
from pathlib import Path
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from vast_connection import VAST_IDENTITY, api_key


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def vast(path, payload=None):
    key = api_key()
    if not key:
        raise ValueError("Save your Vast API key first.")
    request = Request("https://console.vast.ai/api/v0/" + path,
                      json.dumps(payload).encode() if payload is not None else None,
                      {"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with build_opener(NoRedirect()).open(request, timeout=12) as response:
        return json.load(response)


def host_key_from_jupyter(target):
    try:
        import websocket
    except ImportError:
        raise ValueError("Install requirements-connection.txt for automatic SSH verification.") from None
    instance = vast(f"instances/{int(target['id'])}/")["instances"]
    if isinstance(instance, list):
        instance = next(row for row in instance if int(row["id"]) == int(target["id"]))
    ip = str(ipaddress.IPv4Address(instance["public_ipaddr"]))
    ports = instance["ports"]
    if (int(instance["id"]) != int(target["id"]) or ip != target["host"]
            or int(target["port"]) not in {int(p["HostPort"]) for p in ports["22/tcp"]}):
        raise ValueError("Automatic SSH verification requires the Vast direct SSH endpoint.")
    port = int(ports["8080/tcp"][0]["HostPort"])
    if not 1 <= port <= 65535:
        raise ValueError("Vast did not provide a valid Jupyter HTTPS port.")
    address = f"{ip}:{port}"
    with build_opener(NoRedirect()).open("https://console.vast.ai/static/jvastai_root.cer", timeout=10) as response:
        certificate = response.read(65536)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    pem = certificate.decode("ascii") if certificate.lstrip().startswith(b"-----BEGIN CERTIFICATE-----") else ssl.DER_cert_to_PEM_cert(certificate)
    tls.load_verify_locations(cadata=pem)
    opener = build_opener(NoRedirect(), HTTPSHandler(context=tls))
    headers = {"Authorization": "token " + instance["jupyter_token"], "Content-Type": "application/json"}

    def request(path, method="GET", body=None):
        return opener.open(Request("https://" + address + path, body, headers, method=method), timeout=10)

    with request("/api/kernelspecs") as response:
        kernels = json.load(response)["kernelspecs"]
    name = next(name for name, kernel in kernels.items() if kernel["spec"]["language"] == "python")
    with request("/api/kernels", "POST", json.dumps({"name": name}).encode()) as response:
        kernel = str(uuid.UUID(json.load(response)["id"]))
    session, message = uuid.uuid4().hex, uuid.uuid4().hex
    kernel_path = "/api/kernels/" + kernel
    connection = None
    try:
        connection = websocket.create_connection(
            "wss://" + address + kernel_path + "/channels?session_id=" + session,
            header=headers, sslopt={"context": tls}, timeout=10, redirect_limit=0,
            origin="https://" + address, http_no_proxy=[ip])
        connection.send(json.dumps({
            "header":{"msg_id":message, "session":session, "username":"axdar", "msg_type":"execute_request","version":"5.3"},
            "parent_header":{}, "metadata":{}, "channel":"shell", "buffers":[],
            "content":{"code":'from pathlib import Path\nprint(Path("/etc/ssh/ssh_host_ed25519_key.pub").read_text())',
                       "silent":False, "store_history":False, "user_expressions":{}, "allow_stdin":False},
        }))
        end = time.monotonic() + 15
        while time.monotonic() < end:
            reply = json.loads(connection.recv())
            if reply.get("parent_header", {}).get("msg_id") != message:
                continue
            if reply.get("msg_type") == "error":
                break
            if reply.get("msg_type") == "stream":
                match = re.match(r"(ssh-ed25519 [A-Za-z0-9+/=]+)[ \r\n]", reply["content"]["text"])
                if match:
                    key = match.group(1)
                    wire = base64.b64decode(key.split()[1], validate=True)
                    if len(wire) == 51 and wire.startswith(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"):
                        return key
    finally:
        if connection:
            connection.close()
        try:
            with request(kernel_path, "DELETE"):
                pass
        except OSError:
            pass
    raise ValueError("Authenticated Jupyter did not return an SSH host key.")


def trust_host(target):
    folder = Path.home() / ".ssh"
    known = folder / "known_hosts"
    host = target["host"] if target["port"] == 22 else f"[{target['host']}]:{target['port']}"
    if known.exists():
        result = subprocess.run(["ssh-keygen", "-F", host, "-f", str(known)], capture_output=True, timeout=5,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 1:
            raise ValueError("The existing SSH host entry needs verification. AXDAR kept it.")
    try:
        key = host_key_from_jupyter(target)
    except (KeyError, OSError, StopIteration, TypeError):
        raise ValueError("Vast did not provide authenticated Jupyter HTTPS to verify this SSH host.") from None
    folder.mkdir(exist_ok=True)
    existing = known.read_bytes() if known.is_file() else b""
    with known.open("ab") as out:
        if existing and not existing.endswith(b"\n"):
            out.write(b"\n")
        out.write(f"{host} {key}\n".encode("ascii"))


def attach_key(target):
    public = VAST_IDENTITY.with_suffix(".pub")
    if not VAST_IDENTITY.is_file() and not public.exists():
        VAST_IDENTITY.parent.mkdir(exist_ok=True)
        subprocess.run(["ssh-keygen","-q","-t","ed25519","-N","","-C","AXDAR Studio Vast","-f",str(VAST_IDENTITY)],
                       stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=12,
                       creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    if not VAST_IDENTITY.is_file() or not public.is_file():
        raise ValueError("AXDAR's local Vast SSH key pair is incomplete.")
    key = " ".join(public.read_text("utf-8").strip().split()[:2])
    if not re.fullmatch(r"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp\d+) [A-Za-z0-9+/=]+", key):
        raise ValueError("The local SSH public key is invalid.")
    path = f"instances/{int(target['id'])}/ssh/"
    if key not in json.dumps(vast(path)) and not vast(path, {"ssh_key": key}).get("success"):
        raise ValueError("Vast did not attach this computer's SSH public key.")
