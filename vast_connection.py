"""Shared local connection lifetime; rendering remains owned by Production."""
import json
import re
import shlex
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from vast_credentials import (
    SERVICE,
    SSH_CHECK_SECONDS,
    SSH_CONNECT_SECONDS,
    VAST_IDENTITY,
    api_key,
    hf_token,
    identity,
    identity_options,
)

def connection_error(exc):
    """Classify diagnostics without displaying raw SSH/API output or credentials."""
    from urllib.error import HTTPError
    if isinstance(exc, HTTPError):
        return 'Vast API rejected the key or permissions' if exc.code in (401,403) else 'Vast API request failed (HTTP %s)' % exc.code
    message=str(exc).lower()
    if 'host key verification failed' in message or 'remote host identification has changed' in message:
        return 'SSH host key is not verified or has changed. Verify this instance fingerprint before adding it to known_hosts.'
    if 'permission denied' in message:
        return 'SSH authentication failed. Register this computer public key in Vast and verify the instance has it.'
    if 'connection closed' in message or 'connection reset' in message:
        return 'Vast SSH endpoint closed the connection before login. Check the direct SSH endpoint and instance startup.'
    if isinstance(exc, subprocess.TimeoutExpired) or 'timed out' in message:
        return 'SSH connection timed out. Check instance network and SSH port.'
    if 'connection refused' in message:
        return 'SSH port refused the connection. The instance SSH service may still be starting.'
    if isinstance(exc, ValueError):
        return str(exc)
    return 'Connection failed; check Vast, network and verified SSH setup'


def ssh_address(value):
    """Extract the endpoint from a Vast SSH command without executing pasted text."""
    help_text="Paste the Vast SSH command (ssh -p PORT root@HOST) or an ssh://root@HOST:PORT address."
    if not isinstance(value,str):
        raise ValueError(help_text)
    value=value.strip()
    if not 0<len(value)<=2500 or any(ch in value for ch in "\r\n"):
        raise ValueError(help_text)
    if value.lower().startswith("ssh://"):
        try:
            address=urlsplit(value.strip())
            if (address.username not in ("root",None) or address.password is not None or
                    address.path not in ("","/") or address.query or address.fragment):
                raise ValueError(help_text)
            host,port=address.hostname,address.port if address.port is not None else 22
        except ValueError as exc:
            raise ValueError(help_text) from exc
    else:
        try:
            parts=iter(shlex.split(value,posix=True))
        except ValueError as exc:
            raise ValueError(help_text) from exc
        if next(parts,None) not in ("ssh","ssh.exe"):
            raise ValueError(help_text)
        port,host=22,None
        saw_port=False
        for part in parts:
            if part=="-p" and not saw_port:
                specified=next(parts,"")
                if not re.fullmatch(r"[0-9]{1,5}",specified):
                    raise ValueError(help_text)
                port=int(specified)
                saw_port=True
            elif part=="-L":
                forward=next(parts,"")
                if not re.fullmatch(r"(?:(?:localhost|127\.0\.0\.1):)?[0-9]{1,5}:(?:localhost|127\.0\.0\.1):[0-9]{1,5}",forward):
                    raise ValueError(help_text)
            elif part in ("-N","-T"):
                continue
            elif host is None and part.startswith("root@"):
                host=part[5:]
            else:
                raise ValueError(help_text)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}",host or "") or not 1<=port<=65535:
        raise ValueError(help_text)
    return {"host":host.lower(),"port":port}


class Connection:
    def __init__(self, path, discover, remote):
        self.path, self.discover, self.remote = Path(path), discover, remote
        self.lock = threading.RLock()
        self.command_lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.process = None
        self.endpoint = None
        self.url = None
        self.comfy_port = None
        self.state, self.error = 'Disconnected', ''
        self.indicators = dict(api='grey', ssh='grey', comfy='grey', gpu='grey')
        self._event_log = []
        try:
            self.settings = json.loads(self.path.read_text('utf-8'))
        except (OSError, ValueError):
            self.settings = {}

    def snapshot(self):
        with self.lock:
            return dict(self.settings, state=self.state, error=self.error,
                        indicators=dict(self.indicators),
                        gpu=(self.endpoint or {}).get('gpu'),
                        rate=(self.endpoint or {}).get('rate'),
                        uptime=(self.endpoint or {}).get('uptime'),
                        url=self.url if self.state == 'Ready' else None)

    def _indicate(self, **values):
        with self.lock:
            self.indicators.update(values)

    def _state(self, state, error=''):
        with self.lock:
            changed = (state != self.state or error != self.error)
            self.state, self.error = state, error
            if changed:
                stamp = time.strftime('%H:%M:%S')
                line = f"[{stamp}] {state}" + (f" — {error}" if error else "")
                self._event_log.append(line)
                if len(self._event_log) > 120:
                    self._event_log = self._event_log[-120:]

    def recent_logs(self, limit=120):
        with self.lock:
            return list(self._event_log[-max(1, int(limit)):])

    def target(self):
        with self.lock:
            if self.state != 'Ready' or not self.endpoint:
                raise ValueError('Connect to Vast and wait for Ready first')
            return dict(self.endpoint, comfy_port=self.comfy_port)

    def ssh_target(self):
        with self.lock:
            if self.stop.is_set() or self.state=='Disconnected' or not self.endpoint:
                raise ValueError("Connect to Vast and verify the SSH session first.")
            return dict(self.endpoint)

    def discover_instances(self):
        try:
            rows = self.discover()
        except Exception:
            self._indicate(api='red')
            raise
        self._indicate(api='green')
        return rows

    def resolve(self, instance, rows=None):
        if rows is None:
            rows = self.discover_instances()
        row = next((r for r in rows if int(r['id']) == int(instance)), None)
        if not row:
            raise ValueError('Selected instance was not found')
        if row.get('status', 'running') != 'running':
            raise ValueError('Instance is stopped or starting. Start it on Vast, then Retry')
        host, port = str(row.get('host', '')), int(row.get('port') or 0)
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}', host) or not 1 <= port <= 65535:
            raise ValueError('Vast has not provided a valid SSH endpoint yet')
        return dict(row, host=host, port=port)

    def refresh(self, keep_active=False):
        try:
            rows = self.discover_instances()
        except Exception as exc:
            if self.state != 'Ready':
                self._state('Unavailable', connection_error(exc))
            return self.snapshot()
        if keep_active:
            return self.snapshot()
        candidates = []
        for row in rows:
            try:
                candidates.append(self.resolve(row['id'], [row]))
            except (KeyError, TypeError, ValueError):
                continue
        if not candidates:
            if self.state != 'Ready':
                starting = [row for row in rows if row.get('status') in ('loading', 'creating', 'created', 'starting')]
                if starting:
                    newest = max(starting, key=lambda row: int(row['id']))
                    self._state('Vast starting', f"Instance #{newest['id']} is starting on Vast. Waiting for SSH and ComfyUI.")
                else:
                    self._state('Waiting for Vast', 'No running Vast instance with an SSH endpoint.')
            return self.snapshot()
        target = max(candidates, key=lambda row: int(row['id']))
        if self.thread and self.thread.is_alive():
            if self.settings.get('instance') == int(target['id']) and (
                    not self.endpoint or all(self.endpoint.get(k) == target[k] for k in ('host', 'port'))):
                return self.snapshot()
            self.disconnect()
        return self.connect(target['id'], auto=True)

    def connect(self, instance=None, auto=False):
        if not instance and self.settings.get("ssh"):
            return self._start(dict(ssh=self.settings["ssh"],auto=bool(auto)))
        instance = int(instance or self.settings.get('instance') or 0)
        if instance <= 0:
            raise ValueError('Select a rented instance first')
        return self._start(dict(instance=instance,auto=bool(auto)))

    def connect_ssh(self, command, auto=False):
        return self._start(dict(ssh=ssh_address(command),auto=bool(auto)))

    def _persist_settings(self, settings):
        if settings != self.settings or not self.path.is_file():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp=self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(settings),encoding='utf-8')
            tmp.replace(self.path)
        self.settings=settings

    def _resolve_active(self):
        if self.settings.get('ssh'):
            return self.settings['ssh']
        try:
            return self.resolve(self.settings['instance'])
        except ValueError:
            if not self.settings.get('auto'):
                raise
            rows = self.discover_instances()
            candidates = []
            for row in rows:
                try:
                    candidates.append(self.resolve(row['id'], [row]))
                except (KeyError, TypeError, ValueError):
                    continue
            if not candidates:
                raise
            target = max(candidates, key=lambda row: int(row['id']))
            with self.lock:
                self._persist_settings(dict(instance=int(target['id']), auto=True))
            return target

    def _start(self, settings):
        with self.lock:
            if self.thread and self.thread.is_alive():
                if all(self.settings.get(key)==settings.get(key) for key in ("instance","ssh")):
                    return self.snapshot()
                raise ValueError('Disconnect before selecting a different instance')
            self._persist_settings(settings)
            self.endpoint=None
            self.stop.clear()
            self._indicate(api='grey',ssh='grey',comfy='grey',gpu='grey')
            self._state('Connecting')
            self.thread=threading.Thread(target=self._run,daemon=True)
            self.thread.start()
        return self.snapshot()

    def _cleanup(self):
        process, self.process = self.process, None
        self.url = None
        self.comfy_port = None
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            except OSError:
                pass

    def disconnect(self):
        with self.lock:
            self.stop.set()
            thread = self.thread
        if thread and thread.is_alive():
            thread.join(timeout=SSH_CHECK_SECONDS + 10)
            if thread.is_alive():
                raise ValueError('Connection is still closing; retry shortly')
        with self.lock:
            if self.thread is thread:
                self._cleanup()
                self.endpoint = None
                self._state('Disconnected')
                self._indicate(api='grey', ssh='grey', comfy='grey', gpu='grey')
            return self.snapshot()

    def reconnect(self, discover_new=False):
        with self.lock:
            saved = self.settings.copy()
        self.disconnect()
        auto = saved.get('auto', True)
        if saved.get('ssh') or saved.get('instance') and not (discover_new and auto):
            return self._start(saved)
        if auto:
            return self.refresh()
        return self.snapshot()

    def retry_saved(self, instance):
        with self.lock:
            if self.state != 'Failed' or self.stop.is_set() or self.thread and self.thread.is_alive():
                return
            selected = self.settings.get('instance')
            if self.settings.get('ssh'):
                selected = identity(self.settings['ssh'])
            if selected is not None and str(selected) == instance:
                self._start(self.settings)

    def _remote_port(self, target):
        code = ('import urllib.request,json,os\n'
                'candidate=os.environ.get("COMFY_PORT","")\n'
                'ports=[int(candidate)] if candidate.isdecimal() and 1024<=int(candidate)<=65535 else []\n'
                'for port in ports+[18188,8188]:\n'
                ' try:\n'
                '  d=json.load(urllib.request.urlopen("http://127.0.0.1:%s/system_stats"%port,timeout=2))\n'
                '  if "system" in d: print(port);break\n'
                ' except Exception: pass\n')
        import shlex
        command = 'python3 -c ' + shlex.quote(code)
        def check_port():
            if self.stop.is_set():
                raise ValueError('Connection cancelled')
            return self.remote(target, command, timeout=SSH_CHECK_SECONDS).strip()

        def retry_after_attach():
            attach_key(target)
            for attempt in range(4):
                try:
                    return check_port()
                except OSError as retry_exc:
                    if attempt == 3 or 'permission denied (publickey' not in str(retry_exc).lower():
                        raise
                    if self.stop.wait(2):
                        raise ValueError('Connection cancelled')

        try:
            result = check_port()
        except OSError as exc:
            if not target.get('id'):
                raise
            from vast_ssh_setup import attach_key, trust_host
            message = str(exc).lower()
            if 'host key verification failed' in message and 'remote host identification has changed' not in message:
                trust_host(target)
                try:
                    result = check_port()
                except OSError as retry:
                    if 'permission denied (publickey' not in str(retry).lower():
                        raise
                    result = retry_after_attach()
            elif 'permission denied (publickey' in message:
                result = retry_after_attach()
            else:
                raise
        with self.lock:
            self.endpoint=target
        self._indicate(ssh='green')
        if not result:
            self._state('ComfyUI Starting', 'SSH is connected. Waiting for ComfyUI to answer /system_stats.')
            # Reuse the existing supervisor service, only if its stopped state is verified.
            service = self.remote(target, 's=$(supervisorctl status comfyui 2>/dev/null); '
                'case "$s" in *" STOPPED "*) supervisorctl start comfyui >/dev/null && printf pending;; '
                '*" RUNNING "*|*" STARTING "*) printf pending;; esac', timeout=SSH_CHECK_SECONDS).strip()
            deadline = time.monotonic() + 60
            while True:
                if self.stop.wait(2):
                    raise ValueError('Connection cancelled')
                result = check_port()
                if result or service != b'pending' or time.monotonic() >= deadline:
                    break
        port = int(result or 0)
        if not 1024 <= port <= 65535:
            self._indicate(comfy='red')
            raise ValueError('ComfyUI is unavailable on the existing configured ports')
        return port

    def _open(self, target, port):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            local = listener.getsockname()[1]
        args = ['ssh', '-N', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ExitOnForwardFailure=yes', '-o', f'ConnectTimeout={SSH_CONNECT_SECONDS}',
                '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
                *identity_options(target),
                '-L', f'127.0.0.1:{local}:127.0.0.1:{port}', '-p', str(target['port']),
                'root@' + target['host']]
        self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.url = f'http://127.0.0.1:{local}'
        self.comfy_port = port

    def _probe(self):
        if not self.process or self.process.poll() is not None:
            self._indicate(ssh='red', comfy='red', gpu='red')
            return False
        try:
            with urlopen(self.url + '/system_stats', timeout=3) as response:
                data = json.load(response)
            if 'system' not in data:
                self._indicate(comfy='red', gpu='red')
                return False
            gpu = any(str(device.get('type', '')).lower() in ('cuda', 'hip', 'mps', 'xpu')
                      for device in data.get('devices', []) if isinstance(device, dict))
            self._indicate(ssh='green', comfy='green', gpu='green' if gpu else 'red')
            return True
        except (OSError, ValueError):
            self._indicate(comfy='red', gpu='red')
            return False

    def _run(self):
        try:
            failures = 0
            state = 'Connecting'
            while failures < 3 and not self.stop.is_set():
                self._state(state, 'Checking the selected Vast endpoint.')
                stage = 'api'
                connected = False
                try:
                    target = self._resolve_active()
                    stage = 'ssh'
                    self._state(state, 'Waiting for SSH login.')
                    port = self._remote_port(target)
                    if self.stop.is_set():
                        return
                    self._state('ComfyUI Starting', 'Opening the SSH tunnel to ComfyUI.')
                    self._open(target, port)
                    ready_by = time.monotonic() + SSH_CHECK_SECONDS
                    while not self._probe():
                        if not self.process or self.process.poll() is not None:
                            raise ValueError('SSH tunnel closed before forwarding to ComfyUI.')
                        if time.monotonic() >= ready_by:
                            raise ValueError('ComfyUI did not answer /system_stats through the SSH tunnel.')
                        if self.stop.wait(1):
                            return
                    with self.lock:
                        self.endpoint = target
                        self._state('Ready')
                    connected = True
                    checks = 0
                    missed = 0
                    while not self.stop.wait(5):
                        if self._probe():
                            missed = 0
                            self._state('Ready')
                        else:
                            if not self.process or self.process.poll() is not None:
                                raise ValueError('SSH tunnel ended during the session.')
                            missed += 1
                            if missed >= 3:
                                raise ValueError('ComfyUI did not answer three checks during the session.')
                            self._state('ComfyUI Starting', 'Checking ComfyUI again on the existing SSH tunnel.')
                            continue
                        checks += 1
                        if self.settings.get('instance') and checks % 6 == 0:
                            try:
                                self.resolve(self.settings['instance'])
                            except Exception:
                                pass  # An API outage does not close a working SSH tunnel.
                    return
                except Exception as exc:
                    # Do not expose remote output or API response bodies containing credentials.
                    message = connection_error(exc)
                    if stage == 'ssh' and self.indicators['ssh'] != 'green':
                        self._indicate(ssh='red')
                    self._state('Reconnecting', message)
                finally:
                    self._cleanup()
                    with self.lock:
                        self.endpoint = None
                state = 'Reconnecting'
                failures = 0 if connected else failures + 1
                if failures < 3 and self.stop.wait(2 ** max(0, failures - 1)):
                    return
            self._state('Failed', self.error)
        finally:
            self._cleanup()
            if self.stop.is_set():
                self._state('Disconnected')

    def command(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 4096:
            raise ValueError('Enter a command of 1–4096 characters')
        if not self.command_lock.acquire(blocking=False):
            raise ValueError('A terminal command is already running')
        try:
            target = self.ssh_target()
            # A bounded output pipe and timeout; command is never automatically replayed.
            import shlex
            token = hf_token() or ''
            command = ('HF_TOKEN=$(cat); export HF_TOKEN; HUGGING_FACE_HUB_TOKEN=$HF_TOKEN; '
                       'export HUGGING_FACE_HUB_TOKEN; timeout 25s bash -lc ' +
                       shlex.quote(text) + ' 2>&1 | head -c 65536')
            output = self.remote(target, command, timeout=SSH_CONNECT_SECONDS + 30, data=token.encode()).decode('utf-8', errors='replace')
            return output.replace(token, '[REDACTED]') if token else output
        finally:
            self.command_lock.release()
