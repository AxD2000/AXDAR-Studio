"""Decode production video and retain a structured, independent review decision."""
import json
import math
import os
import subprocess


from editor_media import tool


def check(path, job, timeout=180):
    try:
        ffmpeg,ffprobe=tool("ffmpeg"),tool("ffprobe")
    except ValueError:
        return {"technical":"unavailable", "visual":"review required", "reason":"Install ffmpeg and ffprobe on the production host, then recheck this take."}
    options = {"capture_output":True,"timeout":timeout,"creationflags":getattr(subprocess,"CREATE_NO_WINDOW",0)}
    try:
        result = subprocess.run([ffprobe,"-v","error","-show_streams","-show_format","-of","json",os.fspath(path)], **options)
        result.check_returncode()
        info = json.loads(result.stdout)
        videos = [s for s in info.get("streams",[]) if s["codec_type"]=="video" and not s.get("disposition",{}).get("attached_pic")]
        if not videos:
            raise ValueError("No video stream.")
        video = videos[0]
        audio = [s for s in info["streams"] if s["codec_type"]=="audio"]
        if job.get("audioRequired") and not audio:
            raise ValueError("Required audio stream is missing.")
        settings = job["settings"]
        width,height = int(video["width"]),int(video["height"])
        expected = job.get("resolution")
        if min(width,height) <= 0 or expected and [width,height] != expected:
            raise ValueError(f"Unexpected resolution {width}×{height}.")
        duration = float(video.get("duration",info["format"]["duration"]))
        if not math.isfinite(duration) or abs(duration-job.get("playback_seconds",settings["seconds"]))>1:
            raise ValueError(f"Unexpected video duration {duration:.2f}s.")
        decoded = subprocess.run([ffmpeg,"-nostdin","-v","error","-xerror","-progress","pipe:1",
                                  "-i",os.fspath(path),"-map",f"0:{video['index']}","-map","0:a?",
                                  "-f","null","-"], **options)
        decoded.check_returncode()
        frames = [int(line.split(b"=",1)[1]) for line in decoded.stdout.splitlines() if line.startswith(b"frame=")]
        if not frames or not frames[-1]:
            raise ValueError("No video frames decoded.")
        return {"technical":"passed", "visual":"review required", "width":width,"height":height,
                "frames":frames[-1],"duration":duration,"audioStreams":len(audio)}
    except subprocess.TimeoutExpired:
        return {"technical":"unavailable","visual":"review required","reason":"The decode timed out. Recheck the preserved take."}
    except subprocess.CalledProcessError as exc:
        diagnostic_tool = os.path.basename(exc.cmd[0])
        reason = (exc.stderr or b"")[-500:].decode(errors="replace").replace(str(path),path.name)
    except (ValueError,KeyError,TypeError,OSError) as exc:
        diagnostic_tool, reason = "Video probe", str(exc)
    return {"technical":"failed","visual":"review required","reason":f"{diagnostic_tool}: {reason.strip()}"}



def review(path,job,endpoint=None):
    result=check(path,job)
    if not endpoint or result["technical"]!="passed":
        return result
    from base64 import b64encode
    from urllib.request import Request, build_opener

    from drive_archive import NoRedirect
    from production_services import webhook_url

    images=[]
    try:
        for position in (.15,.5,.85):
            frame=subprocess.run([tool("ffmpeg"),"-nostdin","-v","error","-ss",str(position*result["duration"]),
                                  "-i",str(path),"-frames:v","1","-vf","scale=512:-2","-f","image2pipe","-vcodec","mjpeg","-"],
                                 capture_output=True,check=True,timeout=40)
            if not frame.stdout or len(frame.stdout)>1024*1024:
                return result
            images.append(b64encode(frame.stdout).decode())
        payload=json.dumps({"sampledFramesJpeg":images,"shotId":job["shot"],"checks":["motion","anatomy","identity","image"]}).encode()
        request=Request(webhook_url(endpoint),data=payload,headers={"Content-Type":"application/json"},method="POST")
        with build_opener(NoRedirect()).open(request,timeout=40) as response:
            review=json.loads(response.read(8192))
        if review.get("verdict") in ("pass","reject","inconclusive"):
            result["visual"]="review required" if review["verdict"]=="inconclusive" else review["verdict"]
            result["reviewer_note"]="External reviewer assessed three still frames."
    except (OSError,ValueError,subprocess.SubprocessError):
        result["reviewer_note"]="External reviewer unavailable. Inspect the take manually."
    return result



if __name__=="__main__":
    import sys
    from pathlib import Path
    from production import atomic, read

    take=Path(sys.argv[1])
    record=take/"job.json"
    job=read(record)
    if job["status"]!="done":
        raise SystemExit("Choose a completed take.")
    job["quality"]=check(take/"video.mp4",job)
    atomic(record,job)
    print(json.dumps(job["quality"]))
