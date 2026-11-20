"""Shared local connection lifetime; rendering remains owned by Production."""
import json
import os
import re
import socket
import subprocess
import threading
from pathlib import Path
from urllib.request import urlopen

SERVICE = 'AXDAR Studio Vast'

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

def api_key(value=None):
    # Explicit backend prevents falling back to a plaintext keyring.
    from keyring.backends.Windows import WinVaultKeyring
    vault = WinVaultKeyring()
    if value is not None:
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 4096:
            raise ValueError('Enter a valid Vast API key')
        vault.set_password(SERVICE, 'api', value.strip())
    return vault.get_password(SERVICE, 'api') or os.environ.get('VAST_API_KEY')

def hf_token(value=None):
    from keyring.backends.Windows import WinVaultKeyring
    vault = WinVaultKeyring()
    if value is not None:
        if not isinstance(value, str) or not value.strip().startswith('hf_') or len(value.strip()) > 4096:
            raise ValueError('Enter a Hugging Face token beginning with hf_')
        vault.set_password(SERVICE, 'huggingface', value.strip())
    return vault.get_password(SERVICE, 'huggingface')

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
        self.state, self.error = 'Disconnected', ''
        self.indicators = dict(api='grey', ssh='grey', comfy='grey', gpu='grey')
        try:
            self.settings = json.loads(self.path.read_text('utf-8'))
        except (OSError, ValueError):
            self.settings = {}

    def snapshot(self):
        with self.lock:
            return dict(self.settings, state=self.state, error=self.error,
                        indicators=dict(self.indicators),
                        url=self.url if self.state == 'Ready' else None)

    def _indicate(self, **values):
        with self.lock:
            self.indicators.update(values)

    def _state(self, state, error=''):
        with self.lock:
            self.state, self.error = state, error

    def target(self):
        with self.lock:
            if self.state != 'Ready' or not self.endpoint:
                raise ValueError('Connect to Vast and wait for Ready first')
            return dict(self.endpoint)

    def resolve(self, instance):
        try:
            rows = self.discover()
        except Exception:
            self._indicate(api='red')
            raise
        self._indicate(api='green')
        row = next((r for r in rows if int(r['id']) == int(instance)), None)
        if not row:
            raise ValueError('Selected instance was not found')
        if row.get('status', 'running') != 'running':
            raise ValueError('Instance is stopped or starting. Start it on Vast, then Retry')
        host, port = str(row.get('host', '')), int(row.get('port') or 0)
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}', host) or not 1 <= port <= 65535:
            raise ValueError('Vast has not provided a valid SSH endpoint yet')
        return dict(row, host=host, port=port)

    def connect(self, instance=None, auto=False):
        instance = int(instance or self.settings.get('instance') or 0)
        if instance <= 0:
            raise ValueError('Select a rented instance first')
        with self.lock:
            if self.thread and self.thread.is_alive():
                if self.settings.get('instance') == instance:
                    return self.snapshot()
                raise ValueError('Disconnect before selecting a different instance')
            self.settings = dict(instance=instance, auto=bool(auto))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.settings), encoding='utf-8')
            tmp.replace(self.path)
            self.stop.clear()
            self._indicate(api='grey', ssh='grey', comfy='grey', gpu='grey')
            self._state('Connecting')
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        return self.snapshot()

    def _cleanup(self):
        process, self.process = self.process, None
        self.url = None
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
        self.stop.set()
        thread = self.thread
        if thread and thread.is_alive():
            thread.join(timeout=45)
            if thread.is_alive():
                raise ValueError('Connection is still closing; retry shortly')
        self._cleanup()
        self._state('Disconnected')
        self._indicate(api='grey', ssh='grey', comfy='grey', gpu='grey')
        return self.snapshot()

    def _remote_port(self, target):
        code = ('import urllib.request,json\n'
                'for port in (18188,8188):\n'
                ' try:\n'
                '  d=json.load(urllib.request.urlopen("http://127.0.0.1:%s/system_stats"%port,timeout=2))\n'
                '  if "system" in d: print(port);break\n'
                ' except Exception: pass\n')
        import shlex
        command = 'python3 -c ' + shlex.quote(code)
        result = self.remote(target, command, timeout=12).strip()
        self._indicate(ssh='green')
        if not result:
            self._state('ComfyUI Starting')
            # Reuse the existing supervisor service, only if its stopped state is verified.
            self.remote(target, 's=$(supervisorctl status comfyui 2>/dev/null); '
                        'case "$s" in *" STOPPED "*) supervisorctl start comfyui;; esac', timeout=12)
            if self.stop.wait(2):
                raise ValueError('Connection cancelled')
            result = self.remote(target, command, timeout=12).strip()
        port = int(result or 0)
        if port not in (18188, 8188):
            self._indicate(comfy='red')
            raise ValueError('ComfyUI is unavailable on the existing configured ports')
        return port

    def _open(self, target, port):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            local = listener.getsockname()[1]
        args = ['ssh', '-N', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ExitOnForwardFailure=yes', '-o', 'ConnectTimeout=8',
                '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
                '-L', f'127.0.0.1:{local}:127.0.0.1:{port}', '-p', str(target['port']),
                'root@' + target['host']]
        self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.url = f'http://127.0.0.1:{local}'

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
            # Three tunnel lifetimes per explicit Connect, including successful reconnects.
            for attempt in range(3):
                if self.stop.is_set():
                    return
                self._state('Reconnecting' if attempt else 'Connecting')
                stage = 'api'
                try:
                    target = self.resolve(self.settings['instance'])
                    stage = 'ssh'
                    port = self._remote_port(target)
                    stage = 'comfy'
                    if self.stop.is_set():
                        return
                    self._open(target, port)
                    for _ in range(8):
                        if self._probe():
                            break
                        if self.stop.wait(1):
                            return
                    else:
                        raise ValueError('SSH tunnel or ComfyUI readiness failed; verify SSH key and trusted host')
                    with self.lock:
                        self.endpoint = target
                        self._state('Ready')
                    checks = 0
                    while not self.stop.wait(5):
                        if not self._probe():
                            break
                        checks += 1
                        if checks % 6 == 0:
                            try:
                                self.resolve(self.settings['instance'])
                            except Exception:
                                pass  # An API outage does not close a working SSH tunnel.
                    if self.stop.is_set():
                        return
                    raise ValueError('SSH or ComfyUI connection interrupted')
                except Exception as exc:
                    # Do not expose remote output or API response bodies containing credentials.
                    message = connection_error(exc)
                    if stage == 'ssh' and self.indicators['ssh'] != 'green':
                        self._indicate(ssh='red')
                    self._state('Reconnecting', message)
                finally:
                    self._cleanup()
                if attempt < 2 and self.stop.wait(2 ** attempt):
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
            target = self.target()
            # A bounded output pipe and timeout; command is never automatically replayed.
            import shlex
            token = hf_token() or ''
            command = ('HF_TOKEN=$(cat); export HF_TOKEN; HUGGING_FACE_HUB_TOKEN=$HF_TOKEN; '
                       'export HUGGING_FACE_HUB_TOKEN; timeout 25s bash -lc ' +
                       shlex.quote(text) + ' 2>&1 | head -c 65536')
            output = self.remote(target, command, timeout=30, data=token.encode()).decode('utf-8', errors='replace')
            return output.replace(token, '[REDACTED]') if token else output
        finally:
            self.command_lock.release()
