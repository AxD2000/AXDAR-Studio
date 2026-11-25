"""Package-driven workflow installation, environment preflight, and production-readiness tracking."""
import json
import re
import subprocess
import threading
import time
import uuid
from urllib.error import HTTPError
from urllib.request import Request, build_opener

import production
from provision import ModelRedirect
from workflow_install_remote import PHASES
from workflow_profiles import _upstream

INSTALL_ROOT = "/workspace/axdar-studio/installations"
SSH_CHECK_SECONDS = 45
TERMINAL = {"installed", "ready", "incompatible", "needs attention", "failed", "cancelled", "timed out"}
FILES = ("workflow_install_remote.py", "workflow_comfy_manager.py", "production.py", "provision.py", "workflow_packages.py")
HF_VALUE = r"hf_[A-Za-z0-9]{10,256}"


def _default_identity(target):
    return str(target.get("id") or f"{target['host']}:{target['port']}")


def saved_hf_token(token_fn=None):
    if token_fn is None:
        return None
    try:
        return bool(token_fn())
    except Exception:
        return None


class WorkflowManager:
    def __init__(self, ctx, *, ssh_ops=None, hf_token_fn=None, identity_fn=None, ssh_check_seconds=SSH_CHECK_SECONDS):
        self.ctx = ctx
        self.connection = ctx["connection"]
        self.ssh_ops = ssh_ops if ssh_ops is not None else ctx.get("ssh_ops")
        self._hf_token = hf_token_fn if hf_token_fn is not None else ctx.get("hf_token")
        self._identity = identity_fn if identity_fn is not None else (ctx.get("identity") or _default_identity)
        self._ssh_check_seconds = ssh_check_seconds
        directory = ctx["DATA"] / "production"
        self.path = directory / "workflow-installations.json"
        self.profiles = ctx["DATA"] / "production" / "workflow-installation-profiles"
        self.lock = threading.RLock()
        self.watchers = {}
        legacy = production.read(directory / "workflow-installation.json", {})
        self.records = production.read(self.path, {str(legacy["instance"]): legacy} if legacy.get("instance") else {})
        self.telemetry = {}
        self.telemetry_lock = threading.Lock()

    def _token(self, value=None):
        if self._hf_token is None:
            raise ValueError("Hugging Face credential provider is not configured.")
        return self._hf_token(value) if value is not None else self._hf_token()

    def _require_ssh(self):
        if self.ssh_ops is None:
            raise ValueError("SSH transport is not configured for Workflow Manager.")
        return self.ssh_ops

    def selected_instance(self):
        try:
            return self._identity(self.connection.target())
        except ValueError:
            settings = self.connection.snapshot()
            if settings.get("instance"):
                return str(settings["instance"])
            if settings.get("ssh"):
                return self._identity(settings["ssh"])
            return None

    def save(self, record, **changes):
        with self.lock:
            if self.records.get(record["instance"]) is not record:
                return
            record.update(changes)
            production.atomic(self.path, self.records)

    def busy(self, instance=None):
        with self.lock:
            record = self.records.get(instance or self.selected_instance())
            return bool(record and record.get("status") not in TERMINAL)

    def profile_path(self, version):
        if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version):
            raise ValueError("Unknown workflow version.")
        return self.profiles / (version + ".json")

    def profile(self, version):
        return production.read(self.profile_path(version), {})

    def save_profile(self, version, **changes):
        with self.lock:
            data = self.profile(version)
            data.update(changes)
            self.profiles.mkdir(parents=True, exist_ok=True)
            production.atomic(self.profile_path(version), data)

    @staticmethod
    def _env_key(instance, env_fingerprint):
        return f"{instance}::{env_fingerprint or 'unknown'}"

    def environment_state(self, version, instance=None, env_fingerprint=None):
        """Return the installation & production-readiness record for (version, instance, env_fingerprint)."""
        if not version or not re.fullmatch(r"[0-9a-f]{64}", str(version)):
            return None
        instance = str(instance) if instance not in (None, "") else self.selected_instance()
        if not instance:
            return None
        prof = self.profile(version)
        envs = prof.get("environments") or {}
        if env_fingerprint:
            return envs.get(self._env_key(instance, env_fingerprint))
        latest_key = (prof.get("latestByInstance") or {}).get(instance)
        if latest_key and latest_key in envs:
            return envs[latest_key]
        return None

    def record_environment_verification(self, version, instance, status, report, finished=None):
        """Associate installation state with exact (version, instance, env_fingerprint). Never auto-sets productionReadiness."""
        env_fp = (report or {}).get("environmentFingerprint") or ((report or {}).get("environment") or {}).get("environmentFingerprint") or "unknown"
        key = self._env_key(instance, env_fp)
        with self.lock:
            prof = self.profile(version)
            envs = dict(prof.get("environments") or {})
            previous = envs.get(key) or {}
            # Production readiness is preserved ONLY if the exact same (version, instance, env_fingerprint)
            # was previously verified AND remains installed. Installing alone ALWAYS leaves readiness not_verified.
            readiness = (
                previous.get("productionReadiness", "not_verified")
                if status == "installed" and previous.get("environmentFingerprint") == env_fp
                else "not_verified"
            )
            verified_take = previous.get("verifiedTake") if readiness == "verified" else None
            envs[key] = {
                "version": version,
                "instance": str(instance),
                "environmentFingerprint": env_fp,
                "environment": (report or {}).get("environment"),
                "installationStatus": status,
                "compatible": (report or {}).get("compatible"),
                "incompatibilities": (report or {}).get("incompatibilities") or [],
                "installedAt": finished or time.time(),
                "report": report,
                "productionReadiness": readiness,
                "verifiedTake": verified_take,
            }
            latest = dict(prof.get("latestByInstance") or {})
            latest[str(instance)] = key
            # Preserve legacy 'instances' pointer for backward-compatible readers
            # (never overwrite the last successful dependency scan with a failed/empty install report)
            instances = dict(prof.get("instances") or {})
            if report and status not in ("failed", "cancelled", "timed out"):
                instances[str(instance)] = {"status": status, "report": report, "finished": finished or time.time()}
            self.save_profile(version, environments=envs, latestByInstance=latest, instances=instances)
            return envs[key]

    def record_production_readiness(self, version, instance, take_info, passed=True, env_fingerprint=None):
        """Mark production readiness ONLY after real video generation and output verification succeed."""
        if not version or not re.fullmatch(r"[0-9a-f]{64}", str(version)) or not instance:
            return None
        instance = str(instance)
        with self.lock:
            prof = self.profile(version)
            envs = dict(prof.get("environments") or {})
            latest = dict(prof.get("latestByInstance") or {})
            key = self._env_key(instance, env_fingerprint) if env_fingerprint else latest.get(instance)
            if not key or key not in envs:
                fp = env_fingerprint or "unverified-env"
                key = self._env_key(instance, fp)
                envs[key] = {
                    "version": version,
                    "instance": instance,
                    "environmentFingerprint": fp,
                    "environment": None,
                    "installationStatus": "unknown",
                    "productionReadiness": "not_verified",
                    "verifiedTake": None,
                }
            entry = dict(envs[key])
            has_verified_output = bool(
                passed
                and isinstance(take_info, dict)
                and take_info.get("sha256")
                and take_info.get("technical") == "passed"
            )
            entry["productionReadiness"] = "verified" if has_verified_output else "failed"
            entry["verifiedTake"] = (
                {
                    "takeId": take_info.get("takeId"),
                    "shotId": take_info.get("shotId"),
                    "sha256": take_info.get("sha256"),
                    "duration": take_info.get("duration"),
                    "width": take_info.get("width"),
                    "height": take_info.get("height"),
                    "frames": take_info.get("frames"),
                    "verifiedAt": time.time(),
                }
                if has_verified_output else None
            )
            envs[key] = entry
            latest[instance] = key
            self.save_profile(version, environments=envs, latestByInstance=latest)
            return entry

    def is_production_ready(self, version, instance=None, env_fingerprint=None):
        entry = self.environment_state(version, instance=instance, env_fingerprint=env_fingerprint)
        return bool(entry and entry.get("productionReadiness") == "verified")

    def installation_gate(self, version, wf, instance=None):
        """Single source of truth gating production start & regenerate on package installation state."""
        if not wf:
            return {"ready": False, "status": "no_workflow", "reason": "Select a valid Workflow Package first.", "instance": None}
        if not wf.get("packaged"):
            return {"ready": True, "status": "not_packaged", "reason": "", "instance": str(instance) if instance not in (None, "") else None}
        inst = str(instance) if instance not in (None, "") else self.selected_instance()
        if not inst:
            return {
                "ready": False,
                "status": "disconnected",
                "reason": "Connect a Vast instance and install this Workflow Package before starting production.",
                "instance": None,
            }
        with self.lock:
            rec = self.records.get(inst)
        if rec and rec.get("status") not in TERMINAL:
            return {
                "ready": False,
                "status": str(rec.get("status") or "installing"),
                "reason": "A workflow scan or installation is currently running on this instance.",
                "instance": inst,
            }
        if rec and rec.get("version") == version and rec.get("status") in ("failed", "cancelled", "timed out", "incompatible", "needs attention"):
            detail = ""
            stages = rec.get("stages") or {}
            for stage_name in ("Final verification", "Custom node installation", "Model verification", "Environment preflight"):
                st = stages.get(stage_name) or {}
                if st.get("status") == "Failed" and st.get("message"):
                    detail = st["message"]
                    break
            if not detail:
                detail = rec.get("message") or ""
            suffix = f" ({detail})" if detail else ""
            return {
                "ready": False,
                "status": str(rec["status"]),
                "reason": f"Workflow Package installation {rec['status']} on instance #{inst}{suffix}. Fix and install the package before starting production.",
                "instance": inst,
            }
        env_state = self.environment_state(version, instance=inst)
        if not env_state:
            return {
                "ready": False,
                "status": "not_installed",
                "reason": f"Install and verify this Workflow Package on instance #{inst} before starting production.",
                "instance": inst,
            }
        if env_state.get("compatible") is False or env_state.get("installationStatus") == "incompatible":
            reasons = " · ".join(env_state.get("incompatibilities") or ["Environment incompatible with this package."])
            return {
                "ready": False,
                "status": "incompatible",
                "reason": f"Workflow Package is incompatible with instance #{inst}: {reasons}",
                "instance": inst,
            }
        inst_status = str(env_state.get("installationStatus") or "not_installed")
        if inst_status not in ("installed", "ready"):
            return {
                "ready": False,
                "status": inst_status,
                "reason": f"Workflow Package is not installed on instance #{inst} (status: {inst_status}). Install it before starting production.",
                "instance": inst,
            }
        # Installed & compatible on this exact instance -> allow production (including first production test before readiness is verified).
        return {
            "ready": True,
            "status": inst_status,
            "productionReadiness": env_state.get("productionReadiness", "not_verified"),
            "reason": "",
            "instance": inst,
        }

    def verify_live_environment(self, version, target, wf):
        """Re-probe live host environment before production and enforce exact fingerprint match."""
        if not wf.get("packaged") or not target or self.ssh_ops is None:
            return None
        instance = self._identity(target)
        expected_state = self.environment_state(version, instance=instance)
        if not expected_state or expected_state.get("installationStatus") != "installed":
            raise ValueError("Install and verify this Workflow Package on this instance before starting production.")
        expected_fp = expected_state.get("environmentFingerprint")
        ops = self._require_ssh()
        folder = INSTALL_ROOT + "/preflight-" + version[:16]
        base = self.ctx["BASE"]
        plan = {k: wf["graph"][k] for k in _upstream(wf["graph"], [wf["output"]])}
        spec = {
            "packaged": True,
            "packageKind": wf.get("packageKind", "declarative"),
            "version": version,
            "manifest": wf["manifest"],
            "nodes": wf["packageNodes"],
            "models": wf["packageModels"],
            "graph": plan,
            "port": target["comfy_port"],
        }
        ops.ssh(target, f"umask 077; mkdir -p {folder}", timeout=self._ssh_check_seconds)
        ops.deploy_files(target, {folder + "/" + name: base / name for name in FILES})
        ops.put_json(target, folder + "/spec.json", spec)
        raw = ops.ssh(target, f"python3 {folder}/workflow_install_remote.py {folder} verify-env", timeout=self._ssh_check_seconds)
        live = json.loads(raw)
        if not live.get("compatible"):
            reasons = " · ".join(live.get("incompatibilities") or ["Incompatible environment"])
            self.record_environment_verification(version, instance, "incompatible", live)
            raise ValueError(f"Pre-production environment check failed: {reasons}")
        live_fp = live.get("environmentFingerprint")
        if not expected_fp or live_fp != expected_fp:
            self.record_environment_verification(version, instance, "needs attention", live)
            raise ValueError(
                f"Remote environment fingerprint changed since installation "
                f"(installed {str(expected_fp)[:12]}, live {str(live_fp)[:12]}). "
                "Re-run Workflow Package Preflight/Install before production."
            )
        missing_nodes = [r["repo"] for r in live.get("nodeRepos", []) if r.get("status") != "Installed"]
        missing_models = [m["name"] for m in live.get("models", []) if m.get("status") != "Installed"]
        if live.get("nodes") or missing_nodes or missing_models:
            self.record_environment_verification(version, instance, "needs attention", live)
            raise ValueError("Package dependencies are no longer complete on the remote host. Re-verify in Workflow Manager.")
        return live_fp

    def reset_installation(self, requested_instance=None, version=None):
        with self.lock:
            instance = str(requested_instance) if requested_instance not in (None, "") else self.selected_instance()
            if instance and self.busy(instance):
                raise ValueError("Cancel the active scan or installation before resetting workflow downloads.")
            removed_record = None
            if instance:
                removed_record = self.records.pop(instance, None)
                self.watchers.pop(instance, None)
            else:
                for key in [k for k, rec in self.records.items() if rec.get("status") in TERMINAL]:
                    self.records.pop(key, None)
                    self.watchers.pop(key, None)
            production.atomic(self.path, self.records)
            if self.profiles.is_dir():
                for path in self.profiles.glob("*.json"):
                    data = production.read(path, {})
                    changed = False
                    instances_map = data.get("instances")
                    if isinstance(instances_map, dict):
                        if instance and instance in instances_map:
                            instances_map.pop(instance, None)
                            changed = True
                        elif not instance and version and path.stem == version and instances_map:
                            data["instances"] = {}
                            changed = True
                    envs_map = data.get("environments")
                    latest_map = data.get("latestByInstance")
                    if isinstance(envs_map, dict):
                        if instance:
                            for k in [k for k in envs_map if k.startswith(instance + "::")]:
                                envs_map.pop(k, None)
                                changed = True
                            if isinstance(latest_map, dict) and instance in latest_map:
                                latest_map.pop(instance, None)
                                changed = True
                        elif version and path.stem == version:
                            data["environments"] = {}
                            data["latestByInstance"] = {}
                            changed = True
                    if changed:
                        production.atomic(path, data)
        remote_cleaned = False
        if instance and self.ssh_ops is not None:
            try:
                target = self.connection.ssh_target()
                if self._identity(target) == instance:
                    rec_id = (removed_record or {}).get("id")
                    if isinstance(rec_id, str) and re.fullmatch(r"[0-9a-f]{32}", rec_id):
                        self.ssh_ops.ssh(target, f"rm -rf {INSTALL_ROOT}/{rec_id}", timeout=self._ssh_check_seconds)
                    cleanup = (
                        "python3 -c 'import os; from pathlib import Path; "
                        "roots=[Path(os.environ[\"COMFY_ROOT\"])] if os.environ.get(\"COMFY_ROOT\") else []; "
                        "roots+=[Path(\"/workspace/ComfyUI\"),Path(\"/opt/ComfyUI\"),Path.home()/\"ComfyUI\"]; "
                        "[p.unlink(missing_ok=True) for r in roots if (r/\"models\").is_dir() "
                        "for p in (r/\"models\").rglob(\"*.axdar-part\")]'"
                    )
                    self.ssh_ops.ssh(target, cleanup, timeout=self._ssh_check_seconds)
                    remote_cleaned = True
            except Exception:
                remote_cleaned = False
        return {"ok": True, "remoteCleaned": remote_cleaned}

    def action(self, request):
        mode = request.get("action")
        if mode in ("token", "save-token"):
            supplied = request.get("token", "")
            if not isinstance(supplied, str):
                raise ValueError("Enter a Hugging Face access token.")
            supplied = supplied.strip()
            token = supplied or (self._token() if mode == "token" else "")
            if not token or not re.fullmatch(HF_VALUE, token):
                raise ValueError("Enter a Hugging Face access token.")
            if mode == "save-token":
                self._token(token)
                return {"saved": True}
            try:
                with build_opener(ModelRedirect()).open(
                    Request("https://huggingface.co/api/whoami-v2", headers={"Authorization": "Bearer " + token}),
                    timeout=12,
                ) as response:
                    if response.status != 200:
                        raise ValueError("Hugging Face did not accept the token.")
            except HTTPError as exc:
                raise ValueError(f"Hugging Face token check returned HTTP {exc.code}.") from None
            except OSError:
                raise ValueError("Could not reach Hugging Face to verify the token.") from None
            if supplied:
                self._token(token)
            return {"valid": True, "saved": True}
        if mode == "install-preview":
            if request.get("script"):
                raise ValueError("Manual Bash scripts are not permitted in packaged workflow installations.")
            version = request.get("version")
            wf = self.ctx["actions"].store.get(version)
            if not wf.get("packaged"):
                raise ValueError("Import a packaged Workflow ZIP (.zip) for automated installation.")
            return {
                "packaged": True,
                "packageKind": wf.get("packageKind", "declarative"),
                "name": wf["name"],
                "manifest": wf["manifest"],
                "nodesCount": len(wf["packageNodes"]),
                "modelsCount": len(wf["packageModels"]),
                "totalModelBytes": sum(int(m.get("bytes") or 0) for m in wf["packageModels"]),
                "hfTokenSaved": saved_hf_token(self._hf_token),
            }
        if mode == "status":
            return self.status(request.get("version"), request.get("monitor") is True)
        if mode == "cancel":
            with self.lock:
                instance = self.selected_instance()
                if request.get("instance") is not None and str(request["instance"]) != instance:
                    raise ValueError("The selected instance changed. Open its installation monitor to cancel.")
                if not instance or not self.busy(instance):
                    raise ValueError("No active installation to cancel.")
                self.save(self.records[instance], cancel=True, message="Cancellation requested")
                self.watch()
            return {"ok": True}
        if mode == "reset-install":
            return self.reset_installation(request.get("instance"), request.get("version"))
        if mode not in ("scan", "install", "sync-nodes"):
            raise ValueError("Unknown Workflow Manager action.")
        if request.get("script"):
            raise ValueError("Manual Bash execution is removed from production workflow installation. Declare pinned dependencies in the Workflow Package (.zip).")
        version = request.get("version")
        wf = self.ctx["actions"].store.get(version)
        if not wf["executable"]:
            raise ValueError("Select a valid executable Workflow Package (.zip).")
        if not wf["output"] or wf["output"] not in wf["graph"]:
            raise ValueError("Workflow mapping must reference a valid MP4 output node.")
        if mode == "install":
            if not wf.get("packaged"):
                raise ValueError("Legacy standalone JSON profiles cannot run automated installation. Import a Workflow Package (.zip).")
            if request.get("confirmed") is not True:
                raise ValueError("Review and confirm the Workflow Package installation on the selected instance.")
        timeout = int(request.get("timeout", 30))
        if not 1 <= timeout <= 180:
            raise ValueError("Choose an installation timeout of 1–180 minutes.")
        with self.ctx["LOCK"], self.lock:
            target = self.connection.target()
            instance = self._identity(target)
            if self.ctx["running"]:
                raise ValueError("Finish or pause the active production first.")
            if self.busy(instance):
                raise ValueError("A workflow scan or installation is already active on this Vast instance.")
            if mode == "install" and request.get("instance") is not None and str(target.get("id")) != str(request["instance"]):
                raise ValueError("The selected instance changed. Review the installation again.")
            plan = {k: wf["graph"][k] for k in _upstream(wf["graph"], [wf["output"]])}
            record = dict(
                id=uuid.uuid4().hex, version=version, instance=instance,
                rate=target.get("rate"), status="starting", started=time.time(), mode=mode,
                timeout=timeout * 60, stages={}, report={}, log="",
            )
            self.records[instance] = record
            self.save(record)
            spec = dict(
                packaged=bool(wf.get("packaged")),
                version=version,
                graph=plan,
                mode=mode,
                timeout=timeout * 60,
            )
            if wf.get("packaged"):
                spec.update(
                    packageKind=wf.get("packageKind", "declarative"),
                    entrypoint=wf.get("entrypoint", ""),
                    manifest=wf["manifest"],
                    nodes=wf["packageNodes"],
                    models=wf["packageModels"],
                    alignComfyui=bool(mode == "install" and request.get("alignComfyui") is True),
                )
            threading.Thread(target=self.launch, args=(record, target, spec), daemon=True).start()
        return {"ok": True}

    def launch(self, record, target, spec):
        ops = self._require_ssh()
        folder = INSTALL_ROOT + "/" + record["id"]
        self.save(record, message="Uploading workflow package to /workspace")
        try:
            spec["port"] = target["comfy_port"]
            ops.ssh(target, f"umask 077; mkdir -p {folder}")
            base = self.ctx["BASE"]
            planned = {folder + "/" + name: base / name for name in FILES}
            if spec.get("packaged") and spec.get("version"):
                pkg_zip = self.ctx["actions"].store.directory / "packages" / (spec["version"] + ".zip")
                if pkg_zip.is_file():
                    planned[folder + "/package.zip"] = pkg_zip
            ops.deploy_files(target, planned)
            ops.put_json(target, folder + "/spec.json", spec)
            if record.get("cancel"):
                self.save(record, status="cancelled", finished=time.time(), message="Cancelled before launch")
                return
            token = (self._token() or "") if spec["mode"] == "install" else ""
            self.save(record, dispatched=True, message="Waiting for remote installer")
            command = (
                "IFS= read -r HF_TOKEN; export HF_TOKEN; "
                "HUGGING_FACE_HUB_TOKEN=$HF_TOKEN; export HUGGING_FACE_HUB_TOKEN; "
                f"umask 077; nohup python3 {folder}/workflow_install_remote.py {folder}"
                " </dev/null >/dev/null 2>&1 &"
            )
            ops.ssh(target, command, data=(token + "\n").encode(), timeout=self._ssh_check_seconds)
        except Exception:
            if not record.get("dispatched"):
                self.save(record, status="failed", finished=time.time(), message="Remote setup could not be uploaded over SSH.")
                return
            self.save(record, status="reconnecting", message="Launch acknowledgement lost. Checking the original remote operation.")
        self.watch()

    def watch(self):
        with self.lock:
            instance = self.selected_instance()
            record = self.records.get(instance, {})
            previous = self.watchers.get(instance)
            if not instance or not self.busy(instance) or not record.get("dispatched") or previous and previous.is_alive():
                return
            self.watchers[instance] = threading.Thread(target=self.reconcile, args=(record,), daemon=True)
            self.watchers[instance].start()

    def reconcile(self, record):
        ops = self._require_ssh()
        instance = record["instance"]
        folder = INSTALL_ROOT + "/" + record["id"]
        while self.selected_instance() == instance and self.records.get(instance) is record and self.busy(instance):
            try:
                target = self.connection.ssh_target()
                if self._identity(target) != instance:
                    raise ValueError("Select the original instance to reconcile installation.")
                if record.get("cancel"):
                    ops.ssh(target, f"umask 077; touch {folder}/cancel", timeout=self._ssh_check_seconds)
                data = ops.ssh(target, f"python3 {folder}/workflow_install_remote.py {folder} snapshot", timeout=self._ssh_check_seconds)
                state = json.loads(data)
                if not state.get("started"):
                    self.save(record, status="reconnecting", message="Remote process is not yet confirmed. It will not be restarted automatically.")
                    if time.time() > record["started"] + record["timeout"] + 60:
                        self.save(record, status="failed", finished=time.time(), message="Remote launch did not start.")
                else:
                    state.pop("started")
                    state.setdefault("status", "running")
                    with self.lock:
                        message = "Remote installer stopped before recording a result." if state["status"] == "failed" and state.get("exitCode") is None else ""
                        self.save(record, message=message, **state)
                        if state["status"] in ("installed", "ready", "incompatible", "needs attention", "failed", "cancelled", "timed out") and (
                            state.get("report") or state["status"] in ("failed", "cancelled", "timed out")
                        ):
                            self.record_environment_verification(
                                record["version"], instance, state["status"], state.get("report") or {}, record.get("finished"),
                            )
            except (ValueError, OSError, TimeoutError, subprocess.SubprocessError) as exc:
                message = (
                    "Reconnect the original Vast instance to check this installation."
                    if isinstance(exc, ValueError) else "SSH interrupted. Checking the same remote installation."
                )
                self.save(record, status="reconnecting", message=message)
                self.connection.retry_saved(instance)
            time.sleep(2)

    def status(self, version=None, monitor=False):
        self.watch()
        if monitor and self.telemetry_lock.acquire(blocking=False):
            threading.Thread(target=self.refresh_telemetry, daemon=True).start()
        with self.lock:
            target = self.selected_instance()
            record = self.records.get(target, {})
            if target and self.busy(target) and not record.get("dispatched") and time.time() - record["started"] > 300:
                self.save(record, status="failed", finished=time.time(), message="AXDAR closed before it launched the remote operation.")
            view = {
                key: record.get(key)
                for key in ("version", "instance", "status", "rate", "started", "finished", "mode", "stages",
                            "report", "metrics", "log", "message", "exitCode")
            }
            if record:
                view["elapsed"] = (record.get("finished") or time.time()) - record["started"]
            version = version if isinstance(version, str) and re.fullmatch(r"[0-9a-f]{64}", version) else None
            previous = self.profile(version) if version else {}
            env_state = self.environment_state(version, instance=target) if version and target else None
            return dict(
                installation=view,
                phases=PHASES,
                selectedVersion=version,
                connectedInstance=target,
                otherInstallations=sum(key != target and rec.get("status") not in TERMINAL for key, rec in self.records.items()),
                hfTokenSaved=saved_hf_token(self._hf_token),
                verification=(previous.get("instances") or {}).get(target),
                environmentState=env_state,
                installationStatus=env_state.get("installationStatus") if env_state else "not_checked",
                productionReadiness=env_state.get("productionReadiness") if env_state else "not_verified",
                verifiedTake=env_state.get("verifiedTake") if env_state else None,
                telemetry=self.telemetry,
            )

    def refresh_telemetry(self):
        try:
            url = self.connection.snapshot().get("url")
            if not url:
                self.telemetry = {}
                return
            api = self.ctx["json_api"]
            queue = api(url, "/queue")
            stats = api(url, "/system_stats")

            def ids(rows):
                return [row[1] for row in rows if isinstance(row, list) and len(row) >= 2]

            gpus = [{key: d.get(key) for key in ("name", "vram_total", "vram_free", "utilization")} for d in stats.get("devices", [])]
            self.telemetry = dict(running=ids(queue["queue_running"]), queued=ids(queue["queue_pending"]), gpus=gpus)
        except (OSError, ValueError, KeyError):
            self.telemetry = {}
        finally:
            self.telemetry_lock.release()
