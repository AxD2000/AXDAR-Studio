"""Decode and verify technical quality of rendered production video takes."""
import json
import math
import os
import subprocess

from editor_media import tool


def check(path, job, timeout=180):
    try:
        ffmpeg, ffprobe = tool("ffmpeg"), tool("ffprobe")
    except ValueError:
        return {"technical": "unavailable", "reason": "Install ffmpeg and ffprobe on the production host, then recheck this take."}
    options = {"capture_output": True, "timeout": timeout, "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    try:
        result = subprocess.run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", os.fspath(path)], **options)
        result.check_returncode()
        info = json.loads(result.stdout)
        videos = [s for s in info.get("streams", []) if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")]
        if not videos:
            raise ValueError("No video stream.")
        video = videos[0]
        audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
        if job.get("audioRequired") and not audio:
            raise ValueError("Required audio stream is missing.")
        settings = job["settings"]
        width, height = int(video["width"]), int(video["height"])
        expected = job.get("resolution")
        if min(width, height) <= 0 or expected and [width, height] != expected:
            raise ValueError(f"Unexpected resolution {width}×{height}.")
        duration = float(video.get("duration", info["format"]["duration"]))
        if not math.isfinite(duration) or abs(duration - job.get("playback_seconds", settings["seconds"])) > 1:
            raise ValueError(f"Unexpected video duration {duration:.2f}s.")
        decoded = subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-xerror", "-progress", "pipe:1",
                                  "-i", os.fspath(path), "-map", f"0:{video['index']}", "-map", "0:a?",
                                  "-f", "null", "-"], **options)
        decoded.check_returncode()
        frames = [int(line.split(b"=", 1)[1]) for line in decoded.stdout.splitlines() if line.startswith(b"frame=")]
        if not frames or not frames[-1]:
            raise ValueError("No video frames decoded.")
        return {"technical": "passed", "width": width, "height": height,
                "frames": frames[-1], "duration": duration, "audioStreams": len(audio)}
    except subprocess.TimeoutExpired:
        return {"technical": "unavailable", "reason": "The decode timed out. Recheck the preserved take."}
    except subprocess.CalledProcessError as exc:
        diagnostic_tool = os.path.basename(exc.cmd[0])
        reason = (exc.stderr or b"")[-500:].decode(errors="replace").replace(str(path), path.name)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        diagnostic_tool, reason = "Video probe", str(exc)
    return {"technical": "failed", "reason": f"{diagnostic_tool}: {reason.strip()}"}
