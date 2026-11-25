"""Verify the first production take of a workflow imported from the ComfyUI editor."""
import re

import editor_media
import production


LORA_MISMATCH = re.compile(r"\blora\b[^\r\n]*(?:invalid for input|size mismatch|incompatible shape|shape mismatch)", re.I)
LORA_FAILURE = "ComfyUI reported a LoRA weight mismatch during the first shot. Review the LoRA and base model combination."
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def verify_first_shot(ctx, url, deadline, batch, wf, row, retry_failed=False):
    episode, shot = row[0], row[1]
    root=ctx["DATA"]/"production"/"workflow-checks"
    folder=root/batch
    folder.mkdir(parents=True,exist_ok=True)
    report_path=folder/"first-shot.json"
    def latest_attempt():
        attempts=(ctx["DATA"]/episode/shot["id"]).glob("*/job.json")
        return next(((path.parent, record) for path in sorted(attempts,reverse=True)
                     if (record:=production.read(path,{})).get("revision")==shot["revision"]),(folder,{}))

    report=production.read(report_path,{})
    if report.get("status")=="passed":
        return "done"
    if report.get("status")=="failed":
        message=report["message"]
        attempt_folder, attempt=latest_attempt()
        rejected=(not report.get("prompt") and message.startswith("First shot verification failed. ComfyUI 400:")
                  and '"prompt_outputs_failed_validation"' in message)
        pending=(report.get("prompt") and report["prompt"]==attempt.get("ticket")
                 and "cursor" in report and production.pending_prompt(attempt,attempt_folder))
        if not (rejected or pending):
            raise ValueError(message)
        if rejected:
            # A confirmed rejection created no ComfyUI prompt. Rebuild while keeping the old attempt.
            report={}
            retry_failed=True

    def api(route):
        return ctx["json_api"](url,route)

    def logs():
        data=api("/internal/logs/raw")
        entries=data.get("entries") if isinstance(data,dict) else None
        if not isinstance(entries,list) or not entries or any(
            not isinstance(row,dict) or not isinstance(row.get("t"),str)
            or not isinstance(row.get("m"),str) for row in entries
        ):
            raise ValueError("ComfyUI terminal logs are unavailable for this verification.")
        return entries

    if "cursor" not in report:
        editor_media.tool("ffprobe")
        editor_media.tool("ffmpeg")
        q=api("/queue")
        if q["queue_running"] or q["queue_pending"]:
            raise ValueError("Wait for the ComfyUI queue to be empty before verifying this workflow.")
        # Older Studio builds stored an independent test prompt here. Check that it ended before starting the real shot.
        legacy=next(iter(sorted((folder/"original").glob("*/job.json"),reverse=True)),None)
        previous=production.read(legacy,{}) if legacy else {}
        if previous.get("status") in ("submitting","queued","waiting","uncertain"):
            ticket=previous.get("ticket") or production.lookup(ctx["json_api"],url,previous.get("marker") or "legacy-no-marker")
            history=api("/history/"+ticket).get(ticket,{}) if ticket else {}
            state=history.get("status",{})
            if state.get("completed") is not True and state.get("status_str")!="error":
                raise ValueError("The earlier workflow check is unresolved. Check its history on the original ComfyUI server before resuming.")
        report.update(cursor=logs()[-1]["t"],mismatches=0,gap=False)
        production.atomic(report_path,report)

    def observe():
        try:
            rows=logs()
        except (ValueError,OSError):
            return False
        cursor=report["cursor"]
        if rows[0]["t"]>cursor or rows[-1]["t"]<cursor:
            report["gap"]=True
        new=[row for row in rows if row["t"]>cursor]
        report["mismatches"]+=sum(bool(LORA_MISMATCH.search(ANSI.sub("",row["m"]))) for row in new)
        report["cursor"]=max(cursor,rows[-1]["t"])
        production.atomic(report_path,report)
        return True

    def verify_quality(path, job):
        quality=ctx["technical_qc"](path,job)
        if quality.get("technical")!="passed":
            return quality
        complete_logs=observe()
        if report["mismatches"]:
            return dict(quality,technical="failed",reason=LORA_FAILURE)
        if not complete_logs or report["gap"]:
            return dict(quality,technical="failed",reason="ComfyUI did not retain a continuous terminal log for this test. Review its terminal before starting the batch.")
        if job.get("audioRequired") and not quality.get("audioStreams"):
            return dict(quality,technical="failed",reason="The mapped final MP4 is missing its expected audio.")
        return quality

    settings=dict(ctx,technical_qc=verify_quality)
    result=production.render(settings,url,deadline,episode,shot,wf,2,observe,retry_failed)
    if result in ("waiting","uncertain","cancelled"):
        return result
    _,attempt=latest_attempt()
    message=None
    if result!="done" or attempt.get("quality",{}).get("technical")!="passed":
        observe()
        message=LORA_FAILURE if report["mismatches"] else "First shot verification failed. "+(
            attempt.get("error") or "The mapped MP4 or required media could not be verified.")
    report.update(status="failed" if message else "passed",message=message,prompt=attempt.get("ticket"))
    production.atomic(report_path,report)
    if message:
        raise ValueError(message)
    return "done"
