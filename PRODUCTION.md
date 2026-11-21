# AXDAR Studio production

## Run a batch on Vast

1. Rent a ComfyUI instance in Vast. Connect to it in **Production** with the existing Vast and SSH settings.
2. Review the workflow, select approved shots, and set a session length and spending limits. Set the number of network retries separately from end-of-batch shot retries.
3. Choose **Start Production**. The detached worker checks the ComfyUI nodes and models on the selected output paths. If a dependency is unavailable, use **Terminal & HF Token** or Jupyter to install it and choose **Resume Production**. Keep `ffmpeg` and `ffprobe` on the worker PATH so AXDAR can decode and inspect finished videos.
4. Reopen AXDAR to reconcile the saved run. A missing acknowledgement preserves the original submission for reconciliation. Any automatic shot retries run after the first pass and use the same budget limits. Pause and stop requests prevent new shots after the current take.

The targets and budgets apply to a saved run. A spending limit stops further submissions based on the rate and projected render time. Select the existing stop-on-limit policy if you want AXDAR to request a Vast stop after reconciliation. A stopped instance can still incur storage charges. Use **Read confirmed Vast charges** to distinguish provider charges from AXDAR estimates. Episode and monthly tracking build on locally reconciled runs. A monthly cap uses the larger of the local estimate and confirmed Vast account charges when the billing endpoint is accessible. Times during which a rental remains active outside AXDAR can be billed by Vast.

## Review takes

Use **Load review preview** on a completed take. Remote results stay on the instance until transfer. A review preview is separate from the working video. **Accept take** requires successful technical QC. **Reject take** preserves the result. Choose **Regenerate this shot from archived inputs** to make a separate take using the saved workflow and references. Recheck QC after installing missing FFmpeg tools. Legacy takes retain the previous behavior.

The optional visual reviewer accepts HTTPS POST JSON containing `sampledFramesJpeg` (three Base64-encoded JPEGs), a shot ID and requested checks. It may return `{"verdict":"pass"}`, `reject`, or `inconclusive`. The UI records that result separately from technical QC and the manual decision. Audio/video corruption and required tracks are checked with FFmpeg. A few frames can flag visible problems and leave motion between samples unexamined.

## Google Drive and notifications

Enable the Google Drive API in your Google Cloud project and create a **Desktop app** OAuth client. Select that client JSON under **Archive, review and notifications**. Google displays an explicit consent screen for the `drive.file` scope. Return to AXDAR, check the connection, and enable backup for the run. This authorizes the selected Vast worker to use the scoped Drive credential for the archive. The desktop stores credentials in Windows Credential Locker. The worker's credential file is private to the run, separate from archived manifests and logs.

AXDAR creates an **AXDAR Archive** folder and organizes take snapshots by project, episode, scene, shot and take. The archive includes failed artifacts, review records and run metadata. Uploads are resumable. AXDAR verifies Drive file identity, size and MD5, retries with the same Drive ID, and preserves distinct revisions. Choose **Retry backup and notification delivery** after restoring authorization or if a transfer was interrupted.

For external notifications, configure and enable your authorized HTTPS webhook. AXDAR POSTs the event ID, type, episode ID and timestamp, and also shows pending events in Production. The receiver can use the event ID or `Idempotency-Key` header to deduplicate deliveries. A webhook can route to an authorized messaging service. Direct WhatsApp delivery depends on a configured provider and tested permissions.

## Lifecycle rehearsal

**Rehearse ten-minute termination countdown** requires a verified archive, no outstanding retries or jobs, and a delivered warning. AXDAR records the deadline on the instance. Its detached watchdog rechecks those conditions at the deadline. **Cancel countdown** writes a persistent cancellation on the instance. This release runs the lifecycle in rehearsal mode. Destruction remains disabled pending separate authorization and testing.

## Verification

Run `python -B -m unittest discover -q`. FFmpeg tests run when both executables are on PATH. Real Vast GPU and Google Drive acceptance require an authorized instance, a spending limit and Google consent.
