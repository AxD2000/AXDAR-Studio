from pathlib import Path
import sys
import json
import subprocess
import tempfile
import os
from plugin_registry import Registry

# Singleton registry instance
REGISTRY = Registry(Path(__file__).resolve().parent)

# Error helper
def _rejection(message: str, code="rejection"):
    return {"ok": False, "error": {"code": code, "message": message}}

# Main handler

def handle(obj, registry=REGISTRY):
    # Validate request is a dict
    if not isinstance(obj, dict):
        return _rejection("Request must be an object")
    try:
        op = obj.get("op")
        if op == "list":
            return {"ok": True, "data": registry.list()}
        elif op == "install":
            registry.install(obj["id"], obj["version"])
            return {"ok": True, "data": registry.list()}
        elif op == "enable":
            if obj.get("approved") is not True:
                return _rejection("Approval required")
            registry.enable(obj["id"], obj["version"], obj.get("digest"))
            return {"ok": True, "data": registry.list()}
        elif op == "disable":
            registry.disable(obj["id"])
            return {"ok": True, "data": registry.list()}
        elif op == "rollback":
            registry.rollback(obj["id"])
            return {"ok": True, "data": registry.list()}
        elif op == "remove":
            registry.remove(obj["id"], obj["version"])
            return {"ok": True, "data": registry.list()}
        elif op == "invoke":
            return _handle_invoke(obj, registry)
        else:
            return _rejection(f"Unsupported operation {op}")
    except (ValueError, KeyError, TypeError, OSError) as e:
        return _rejection(str(e))

# Invoke helper

def _handle_invoke(obj, registry):
    id_ = obj["id"]
    action = obj["action"]
    text = obj.get("text", "")
    if not isinstance(text, str) or len(text) > 4096:
        return _rejection("Text must be a string up to 4096 characters")
    try:
        manifest, script_bytes = registry.resolve(id_, action)
    except Exception as e:
        return _rejection(str(e))
    # Check permissions
    if "execute:local" not in manifest.get("permissions", []):
        return _rejection("Missing execute:local permission")
    timeout = manifest.get("timeout_seconds", 5)
    with tempfile.TemporaryDirectory() as tmpdir:
        action_path = Path(tmpdir) / "action.py"
        action_path.write_bytes(script_bytes)
        # sanitized env
        env = {"TEMP": tmpdir, "TMP": tmpdir, "TMPDIR": tmpdir}
        if "SystemRoot" in os.environ:
            env["SystemRoot"] = os.environ["SystemRoot"]
        cmd = [sys.executable, "-I", "-S", "-B", "-X", "utf8", str(action_path)]
        stdin_json = json.dumps({"api_version": 1, "action": action, "input": {"text": text}}).encode()
        try:
            with tempfile.TemporaryFile() as out_file:
                proc = subprocess.run(
                    cmd,
                    input=stdin_json,
                    stdout=out_file,
                    stderr=subprocess.DEVNULL,
                    timeout=timeout,
                    cwd=tmpdir,
                    env=env,
                    shell=False,
                )
                if proc.returncode != 0:
                    return _rejection(f"Plugin exited with code {proc.returncode}", "plugin_failed")
                if out_file.tell() > 65536:
                    return _rejection("Output too large", "invalid_result")
                out_file.seek(0)
                output_bytes = out_file.read()
            try:
                result = json.loads(output_bytes.decode("utf-8"))
            except (ValueError, RecursionError):
                return _rejection("Expected a UTF-8 JSON result", "invalid_result")
            if not isinstance(result, dict) or set(result) != {"text"} or not isinstance(result["text"], str):
                return _rejection("Malformed result", "invalid_result")
            out_text = result["text"]
            if len(out_text) > 4096:
                return _rejection("Output text too long", "invalid_result")
            return {"ok": True, "data": {"text": out_text}}
        except subprocess.TimeoutExpired:
            return _rejection("Execution timed out", "timeout")

# End of file
