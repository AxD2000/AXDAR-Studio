"""Run a ComfyUI UI workflow's captured graph without changing its inputs."""
import re

import editor_media
import production
from workflow_install_remote import scan
from workflow_profiles import _upstream


LORA_MISMATCH = re.compile(r"\blora\b[^\r\n]*(?:invalid for input|size mismatch|incompatible shape|shape mismatch)", re.I)
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def verify_authoring_workflow(ctx, url, deadline, batch, wf):
    root=ctx["DATA"]/"production"/"workflow-checks"
    folder=root/batch
    folder.mkdir(parents=True,exist_ok=True)
    report_path=folder/"verification.json"
    report=production.read(report_path,{})
    if report.get("status")=="passed":
        return "passed"
    if report.get("status")=="failed":
        raise ValueError(report["message"])

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
        graph={key:wf["graph"][key] for key in _upstream(wf["graph"],[wf["output"]])}
        deps=scan(graph,api("/object_info"))
        missing=[m["name"] for m in deps["models"] if m["status"]!="Installed"]
        if deps["nodes"] or missing:
            names=deps["nodes"]+missing
            raise ValueError("Resolve workflow dependencies before submitting a prompt: "+", ".join(names))
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

    output=str(wf["output"])
    shot={"id":"original","revision":wf["version"],"seconds":0,"prompt":""}
    def download(server, dest, history, job):
        ctx["download_output"](server,dest,history,job)
        info=editor_media.probe(dest/"video.part")
        job["playback_seconds"]=info["duration"]

    settings=dict(ctx,DATA=root,prepare_shot=lambda *_:wf["graph"],
                  download_output=download,prompt=lambda *_:"",direct=True)
    result=production.render(settings,url,deadline,batch,shot,[],{"name":wf["name"],"output":output},
                             2,observe)
    if result in ("waiting","uncertain","cancelled"):
        return result
    last=next(iter(sorted((folder/"original").glob("*/job.json"),reverse=True)),None)
    attempt=production.read(last,{}) if last else {}
    complete_logs=observe()
    message=None
    if report["mismatches"]:
        message="ComfyUI reported a LoRA weight mismatch during the original workflow test. Review the LoRA and base model combination."
    elif result!="done" or attempt.get("quality",{}).get("technical")!="passed":
        message="Original workflow verification failed. "+(attempt.get("error") or "The mapped MP4 or required media could not be verified.")
    elif not complete_logs or report["gap"]:
        message="ComfyUI did not retain a continuous terminal log for this test. Review its terminal before starting the batch."
    elif attempt.get("audioRequired") and not attempt["quality"].get("audioStreams"):
        message="The mapped final MP4 is missing its expected audio."
    report.update(status="failed" if message else "passed",message=message,prompt=attempt.get("ticket"))
    production.atomic(report_path,report)
    if message:
        raise ValueError(message)
    return "passed"
