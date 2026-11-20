# AXDAR Studio

Local Studio for organizing shots, importing ComfyUI API workflows, managing references, and coordinating rendering on a user's Vast.ai instance.

This repository contains the application source and a disposable CPU-only plugin fixture. Personal projects, generated media, credentials and third-party workflow exports are excluded.

## Run on Windows

The current implementation is tested with Python 3.14 and Windows OpenSSH. It opens a local web interface, not a packaged EXE.

```powershell
python -m pip install -r requirements-connection.txt
python studio.pyw
```

Open http://127.0.0.1:18753/ if the browser does not open automatically. The application listens on localhost.

Upload your own compatible MiniMax H3 ComfyUI workflow in API JSON format. Workflow import is currently H3-specific; arbitrary ComfyUI graphs and GitHub projects are not automatically compatible. A fresh checkout starts without an active workflow.

Import media in Asset Vault. Optional pre-existing assets can be placed under `assets/`, or selected through the `AXDAR_ASSETS` environment variable. Project state is created under `episodes/`, and imported references under `library/`; both are ignored by Git.

## Vast connection

In Production, save the Vast API key, discover your instance, then connect. Credentials use the explicit Windows Credential Locker backend of `keyring`. SSH private keys remain local and host-key verification stays enabled. Register your SSH public key with Vast and independently verify each new server's fingerprint during initial setup.

The shared manager supplies Production, ComfyUI forwarding and remote command execution. It discovers direct SSH mappings, re-fetches endpoint details on reconnect, binds tunnels to localhost, and uses bounded retries. Stopped instances are not started automatically.

Connection indicators report Vast API, SSH, ComfyUI and GPU checks: grey means not checked/disconnected, red means a failed check, and green means passed. GPU detection uses ComfyUI's reported device type; it is not a render benchmark.

Next to Upload workflow, **Terminal & HF Token** opens a Bash command runner on the connected instance. HF credentials are stored in Windows Credential Locker and passed to commands through stdin/environment. Commands are limited to 25 seconds and 64 KiB output. Long downloads and interactive commands require the instance's Jupyter Terminal. Do not paste private credentials into command text.

Disconnect closes the local tunnel, not the remote worker or the rented instance. Vast billing is managed on Vast. Archive disconnected session affects local tracking only.

## Plugins

See [PLUGINS.md](PLUGINS.md) for the manifest, registry and action contract. `plugins/catalog/demo.echo/` provides two CPU-only fixture versions. Arbitrary downloaded code requires explicit approval. No third-party GPU plugin is bundled.

## Verification

Local deterministic checks:

```powershell
python -B -m unittest -q test_connection test_connection_http test_plugins
```

`test_production.py` also exercises the queue and mocked remote artifacts. Two of its cases require a compatible `workflow.json` supplied locally; no third-party workflow is distributed here. These are not paid GPU tests.

The project is a working prototype. Real rendering still depends on compatible ComfyUI nodes, models and server setup. The source includes a small reviewed provisioning allowlist; it is not a general installer for every missing custom node.

## Main files

| File | Responsibility |
| --- | --- |
| `studio.pyw`, `studio.html`, `workspaces.js` | Local server and Studio interface |
| `production.py`, `worker.py`, `vast_worker.py` | Existing rendering queue and workers |
| `vast_deploy.py`, `vast_connection.py` | Deployment, shared connection and command execution |
| `plugin_manifest.py`, `plugin_registry.py`, `plugin_host.py` | Plugin validation, lifecycle and action dispatch |
| `requirements-connection.txt` | Pinned Windows credential-store dependencies |

No license is assigned by this publication. Referenced third-party projects retain their own licenses.
