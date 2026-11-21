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

When opening an existing project, review the migration and choose **Create backup and migrate**. AXDAR saves a copy of the previous project JSON beside it, preserves episode and shot IDs, and links existing takes. Generated media stays in its original folder.

1. In **Asset Vault**, add references and register each character with a name, aliases, a visual reference, and any voices.
2. In **Story & Scenes → Script**, paste text or choose **Import story file**. AXDAR accepts TXT, Markdown, Fountain, DOCX, and PDF with extractable text. Edit the proposed scenes and shots and choose **Add reviewed scenes**. The original text is retained.
3. In **Settings** or **Production**, import a ComfyUI API JSON graph, then review its input and video output mapping. Use **Read /object_info** with a running ComfyUI server for its installed node contracts. Save edits as a new mapping revision and choose **Use this version** to activate it. AXDAR saves the original file and builds separate runtime graphs.
4. In **Shot Lab**, edit and preview the cast, prompts, references, and timing. Resolve ambiguous names, missing references, and any proposed splits. Approve individual shots or choose **Approve all ready shots**.
5. In **Production**, connect to ComfyUI or the existing Vast instance and submit the approved batch. The worker archives the derived graph and MP4 with the take.

Optional pre-existing assets can live in `assets/` or a directory set via `AXDAR_ASSETS`. Project state is created under `episodes/`, imported references under `library/`, and workflow revisions under `workflow-profiles/`. Git ignores these directories.

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
python -B -m unittest discover -q
```

`test_production.py` also exercises the queue and mocked remote artifacts. The H3 integration checks use the user's saved `selected-workflow.json` when it is available. The repository does not distribute user workflow exports. These are not paid GPU tests.

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
