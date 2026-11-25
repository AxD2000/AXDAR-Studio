"""Thin desktop HTTP shell & launcher for AXDAR Studio."""
import json
import logging
import mimetypes
import re
import socket
import subprocess
import sys
import threading
import webbrowser
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from app_context import AppContainer
import studio_routes

BASE = Path(__file__).resolve().parent
PORT = 18753
app = AppContainer(BASE, PORT)


class Handler(BaseHTTPRequestHandler):
    def send(self, data, mime="application/json", status=200):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path, download=None):
        length = path.stat().st_size
        start, end = 0, length - 1
        range_header = self.headers.get("Range", "")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            if match and any(match.groups()) and length:
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = min(end, int(last)) if last else end
                else:
                    start = max(0, length - int(last))
            if not match or not any(match.groups()) or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{length}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        self.send_response(206 if range_header else 200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Content-Length", str(end - start + 1))
        if range_header:
            self.send_header("Content-Range", f"bytes {start}-{end}/{length}")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.end_headers()
        try:
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining:
                    block = stream.read(min(remaining, 256 * 1024))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        return studio_routes.handle_get(self, app)

    def do_POST(self):
        try:
            return studio_routes.handle_post(self, app)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeError, OSError, HTTPException, subprocess.SubprocessError) as exc:
            self.send({"error": str(exc)}, status=400)
        except Exception:
            logging.exception("Unhandled error in POST %s", self.path)
            self.send({"error": "Unexpected internal Studio error."}, status=500)

    def log_message(self, *args):
        pass


class StudioServer(ThreadingHTTPServer):
    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


if __name__ == "__main__":
    address = f"http://127.0.0.1:{app.PORT}"
    try:
        server = StudioServer(("127.0.0.1", app.PORT), Handler)
    except OSError:
        with urlopen(address + "/api", timeout=3) as response:
            if "project" not in json.load(response):
                raise
        webbrowser.open(address)
    else:
        with server:
            threading.Thread(target=app.open_workspace_session, args=("resume",), daemon=True).start()
            webbrowser.open(address)
            threading.Thread(target=app.discover_vast_loop, daemon=True).start()
            server.serve_forever()
        if app.restart_requested.is_set():
            try:
                app.connection.disconnect()
            finally:
                subprocess.Popen(
                    [sys.executable, "-B", str(BASE / "studio.pyw")],
                    cwd=BASE,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
