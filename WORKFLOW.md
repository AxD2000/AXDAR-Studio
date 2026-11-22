# Workflow Manager

Open **Workflow** in AXDAR Studio and use its four tabs.

- **Setup** imports versioned ComfyUI workflows, tests and saves a Hugging Face token in Windows Credential Manager, and previews optional Bash before remote execution. A standard UI JSON remains a read-only original until you attach an API export of the same workflow. Review its input mapping and select the final MP4 node before using it in Production.
- **Dependencies** checks the selected profile against the connected instance. Classes are checked through /object_info, and model statuses use the node's advertised choices and matching remote files. Unsupported classes appear by exact name. Automatic installation uses the reviewed repositories and pinned revisions in provision.py and the verified model URLs, hashes and lengths in model-lock.json. Supply other dependencies with the approved Bash script.
- **Installation Monitor** reconciles the same remote process after reopening AXDAR or reconnecting the instance. It shows measured item counts, indeterminate active work, terminal output, exit code and estimated rental cost. Cancel Installation signals the actual installer process group. A disconnected cancellation takes effect when SSH can reach the original instance.
- **Production Monitor** uses the existing AXDAR take records, ComfyUI queue/history, and Video Assembly exports. Start, pause, resume, stop and retry operate on the same direct production queue as the Production workspace. Stop requests a prompt-scoped cancel on servers that support it. Older servers finish the active prompt and stop before the next shot. An unknown submission must reconcile on its original instance before another is sent.

## Integration

WorkflowStore keeps content-addressed originals and separately stores validated API captures. WorkflowManager sends a reviewed plan to the existing Vast connection. A detached supervisor on the instance keeps stage, exit, and redacted log files so Windows or SSH can reconnect without relaunching. The installed classes must appear in /object_info after any restart. Existing node directories and model files are retained. The installer protects installed Torch and CUDA package versions.

DirectSession persists the current queue and a cursor. It reuses the existing production renderer and attempt marker. On tunnel renewal, history, queue and file requests follow the new URL for the same instance. The renderer obtains the MP4 only from its mapped output. The MiniMax H3 fixture maps node 141, preserves the full upstream sampling branch and excludes preview 135.

## Verification and limits

Tests use a disposable local project, simulated SSH and ComfyUI endpoints, and real FFmpeg media. They cover byte-for-byte import preservation, invalid-capture rejection, node and model checks, known-node installation with a simulated restart and registry verification, explicit Bash authorization, credential delivery, cancellation, reopening after disconnection, resumable model reads, and three consecutive shot downloads after a lost submission acknowledgement. Chrome exercises actual workspace navigation, token masking, graph attachment and the production gate.

A disposable live Vast instance was unavailable for this branch. GPU inference, the remote supervisor restart, and a real custom-node installation have not been accepted on Vast. Missing classes outside the reviewed source list and model files outside the verified model manifest require manual review. ComfyUI model registration cannot detect every runtime LoRA tensor incompatibility. Live node-by-node progress and GPU utilization may be unavailable on servers that only expose queue/history and VRAM statistics.
