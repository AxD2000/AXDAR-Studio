# AXDAR Studio plugins V1

## Studio bundle

This overlay targets the reviewed desktop build. It includes the two Studio integration files, three plugin host modules, the panel, tests, this guide and both fixture releases. It contains no project database, workflow, active plugin state or generated media. For a fresh copy of the matching baseline, close Studio, back up studio.pyw and studio.html and copy the overlay into the Studio directory with its relative paths intact. Verify the baseline SHA-256 of studio.pyw is `000ec0d94c0c9fbff272a2db48bcffba61d61aecc0424b477bb6a05d6ccaecd9` and studio.html is `b00a338126c3551289ce90c680fad39dac4508b4e8878a45734caa6982fdef31` before replacing them. On a different Studio revision, merge the authenticated route and script attachment separately to preserve local changes.

Run the deterministic plugin suite from the Studio directory with `python -B -m unittest -v test_plugins`. The tests use temporary registries. V1 was verified on Windows with Python 3.14.0.

## Install and use the CPU fixture

Restart Studio from its existing shortcut after installing this update. Open **Plugin Manager**. The local catalog contains two releases of the same disposable action. Install version 1.0.0, review its package fingerprint and approve activation. Select the action, enter text and run it. Install and activate 2.0.0 to switch the active version. Roll back to restore the previous version. Disable hides its actions; enable makes them available again. Disable an active version before removing it. Removal affects the installed snapshot. A package still present in the catalog remains available to install again.

Studio discovers packages in this directory layout:

```
plugins/
  catalog/
    <plugin-id>/
      <version>/
        manifest.json
        action.py
  state.json
```

Place reviewed package files in the catalog locally. Use an immutable version for each release. Install snapshots the manifest and script and pins their combined SHA-256. Editing catalog contents does not update an installed version. Installed versions are independent of project data, the library and generated takes. Retain installed versions you want to roll back to.

**Approval and isolation.** Installing a package does not run it. Enabling authorizes the displayed version to execute local code. Read both files before trusting a package. A fingerprint identifies particular bytes; it does not vouch for the publisher. A plugin process runs with the Studio user's OS privileges. A working directory and isolated Python interpreter are not an OS sandbox. V1 accepts standard-library-only packages with an empty dependency list. Plugins that need third-party dependencies require a later adapter with a separately locked environment.

## Manifest and adapter wire contract

Use the included manifests as working examples. V1 checks the manifest version, host API version, plugin ID, release version, adapter, permissions, timeout, dependency list, and declared actions. Labels are display text. Action IDs are stable within a plugin. The initial action type accepts one text value and produces one text result.

The implemented CLI profile uses a fixed package-local Python script as a CLI executable. The host starts it without a shell in a separate process. Input and output travel as UTF-8 JSON. The request, response and error shapes appear below in the protocol example. The action ID must be declared by the selected release. A UI action cannot supply an executable, URL, module path or environment.

Example stdin for action.py:

```json
{"api_version":1,"action":"echo","input":{"text":"hello"}}
```

Expected stdout from the first fixture:

```json
{"text":"v1: hello"}
```

The authenticated Studio bridge is `POST /plugins`. Its success envelope is `{"ok":true,"data":...}`. Errors use `{"ok":false,"error":{"code":"timeout","message":"Execution timed out"}}`. CLI failures distinguish a rejected request, timeout, nonzero exit and invalid result. The host accepts at most 4,096 text characters, 64 KiB of returned JSON, and a manifest timeout from 1 to 10 seconds. Each package source file is limited to 64 KiB. Plugins should emit one result and avoid spawning background processes.

The CLI runtime uses Studio’s Python executable with `-I -S -B -X utf8`, a private temporary directory and no inherited site packages. V1 pins plugin code, manifest bytes and the empty dependency set. The system Python interpreter is shared. Runtime timeouts apply to the direct child; disk quotas and descendant-process containment require an OS sandbox.

Two reserved adapter interfaces use the same action envelope: an HTTP adapter would POST the request to an endpoint fixed by approved package configuration and decode its JSON response. A Python-module adapter would call a declared function with the request object in a private locked helper process and return its result object. These profiles require a future host implementation. The V1 validator rejects their manifests at installation.

## Future GPU actions

A future GPU-capable adapter should prepare an immutable AXDAR-compatible job request and pass it to the existing Studio admission path. Preserve approval, shot/revision identity, submission marker, reconciliation, budget checks, media validation and Vast stop policy. Return the existing job reference. The Worker and Queue remain the owners of execution and recovery. A plugin has no direct GPU submission, SSH or instance-management permission in V1. An unsupported graph or action fails before submission. Define and test the handoff against the existing admission contract before enabling a GPU adapter. No H3 recipe changes are required for the CPU fixture.
