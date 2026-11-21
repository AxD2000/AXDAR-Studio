"""FFmpeg processing for the local episode editor."""
import json
from html import escape
import math
import os
import re
import shutil
import subprocess
import threading

from production import digest
from functools import lru_cache
from pathlib import Path

BASE = Path(__file__).resolve().parent
CREATION = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def tool(name):
    suffix = ".exe" if os.name == "nt" else ""
    override = os.environ.get("AXDAR_FFMPEG_BIN")
    folders = [Path(override)] if override else []
    folders.append(BASE / "tools" / "ffmpeg" / "bin")
    for directory in folders:
        candidate = directory / (name + suffix)
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if not found:
        raise ValueError("Install FFmpeg and ffprobe or set AXDAR_FFMPEG_BIN to their bin directory.")
    return found


def run(args, *, cwd=None, timeout=300):
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True, timeout=timeout, creationflags=CREATION)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("The media operation timed out.") from exc
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip().splitlines()[-6:]
        raise ValueError("\n".join(detail)[:1200] or "FFmpeg could not process this media.")
    return result


def probe(path):
    stat = path.stat()
    return _probe(str(path),stat.st_size,stat.st_mtime_ns)


@lru_cache(maxsize=1024)
def _probe(path,size,modified):
    args = [tool("ffprobe"),"-v","error","-protocol_whitelist","file,pipe","-show_streams","-show_format","-of","json","-i",path]
    data = json.loads(run(args,timeout=20).stdout)
    video = next((s for s in data.get("streams",[]) if s["codec_type"]=="video" and not s.get("disposition",{}).get("attached_pic")),{})
    if not video:
        raise ValueError("The take contains no video stream.")
    audio = next((s for s in data["streams"] if s["codec_type"]=="audio"),{})
    duration = float(video.get("duration","N/A") if video.get("duration","N/A")!="N/A" else data["format"]["duration"])
    if not math.isfinite(duration) or duration<=0:
        raise ValueError("Invalid take duration.")
    rotated=any(int(row.get("rotation",0))%180 for row in video.get("side_data_list",[]))
    width,height=int(video["width"]),int(video["height"])
    if rotated:
        width,height=height,width
    return dict(width=width,height=height,duration=duration,formatDuration=float(data["format"]["duration"]),
                fps=video.get("avg_frame_rate"),codec=video.get("codec_name"),audio=bool(audio),
                audioCodec=audio.get("codec_name"),audioDuration=float(audio.get("duration",0) if audio.get("duration")!="N/A" else 0),
                videoIndex=video["index"],audioIndex=audio.get("index"))


@lru_cache(maxsize=2048)
def _sound_level(path,mtime,index,start,length):
    args=[tool("ffmpeg"),"-hide_banner","-nostdin","-ss",str(round(start,4)),"-i",path,
          "-t",str(round(length,4)),"-map",f"0:{index}","-af","volumedetect","-f","null","-"]
    log=run(args,timeout=12).stderr.decode(errors="replace")
    match=re.search(r"mean_volume:\s*(-?[\d.]+) dB",log)
    return float(match[1]) if match else None


def sound_level(path,info,start,length):
    if not info["audio"]:
        return None
    return _sound_level(str(path),path.stat().st_mtime_ns,info["audioIndex"],start,length)


def detected_intervals(log,kind,duration):
    start=None
    intervals=[]
    for mark,number in re.findall(rf"\b{kind}_(start|end)\s*:\s*(-?[\d.]+(?:[eE][+-]?\d+)?)",log):
        value=max(0,float(number))
        if mark=="start":
            start=value
        elif start is not None:
            intervals.append([start,min(value,duration)])
            start=None
    if start is not None:
        intervals.append([start,duration])
    return intervals


@lru_cache(maxsize=512)
def _analyze(path,size,modified,duration,video_index,audio_index):
    args=[tool("ffmpeg"),"-hide_banner","-nostdin","-i",path,"-map",f"0:{video_index}",
          "-vf","setpts=PTS-STARTPTS,freezedetect=n=-55dB:d=0.38"]
    if audio_index is not None:
        args += ["-map",f"0:{audio_index}","-af","asetpts=PTS-STARTPTS,silencedetect=n=-55dB:d=0.38"]
    result=run([*args,"-f","null","-"],timeout=50)
    log=result.stderr.decode(errors="replace")
    return dict(silent=detected_intervals(log,"silence",duration) if audio_index is not None else [[0,duration]],
                frozen=detected_intervals(log,"freeze",duration))


def analyze(path,info):
    stat=path.stat()
    return _analyze(str(path),stat.st_size,stat.st_mtime_ns,info["duration"],info["videoIndex"],info["audioIndex"])


def time_text(seconds):
    ms=round(seconds*1000)
    whole,ms=divmod(ms,1000)
    minutes,seconds=divmod(whole,60)
    hours,minutes=divmod(minutes,60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"


def write_subtitles(path,cues):
    chunks=[f"{i}\n{time_text(cue['start'])} --> {time_text(cue['end'])}\n{cue['text']}\n"
            for i,cue in enumerate(cues,1)]
    path.write_text("\n".join(chunks),encoding="utf-8")
    vtt = ["WEBVTT", ""]
    for cue in cues:
        start,end=(time_text(cue[edge]).replace(",",".") for edge in ("start","end"))
        vtt.extend([f"{start} --> {end}",escape(cue["text"],quote=False),""])
    path.with_suffix(".vtt").write_text("\n".join(vtt)+"\n",encoding="utf-8")


def style(opts):
    colors = "&H00" + "".join(opts["color"][i:i+2] for i in (4,2,0))
    alignment = 8 if opts["placement"]=="top" else 2
    return f"FontName={opts['font']},FontSize={opts['fontSize']},PrimaryColour={colors},Outline={opts['outline']},Alignment={alignment},MarginV={opts['margin']}"


def shape(opts):
    w,h = opts["width"],opts["height"]
    frames=[r"scale=w=max(2\,trunc(iw*sar/2)*2):h=ih","setsar=1",
            f"scale=w={w}:h={h}:force_original_aspect_ratio={'increase' if opts['fit']=='crop' else 'decrease'}:force_divisible_by=2",
            "setsar=1"]
    if opts["fit"]=="crop":
        frames.append(f"crop={w}:{h}:(iw-ow)*{opts['cropX']}:(ih-oh)*{opts['cropY']}")
    else:
        frames.append(f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black")
    frames.append(f"fps={opts['fps']}")
    return frames


def process(args, cwd, duration, notify):
    """Stream FFmpeg progress without accumulating logs in memory."""
    log = cwd/"ffmpeg.log"
    argv=[tool("ffmpeg"),"-v","error","-xerror","-nostdin","-y","-progress","pipe:1","-nostats",*args]
    with log.open("wb") as error, subprocess.Popen(argv,cwd=cwd,stdout=subprocess.PIPE,stderr=error,
                                                  creationflags=CREATION) as proc:
        deadline=threading.Timer(min(5400,max(180,duration*15)),proc.kill)
        deadline.daemon=True
        deadline.start()
        try:
            for line in proc.stdout:
                if line.startswith(b"out_time_us="):
                    value=line.strip().split(b"=",1)[1]
                    if value.lstrip(b"-").isdigit():
                        notify(min(1,max(0,int(value)/1_000_000/duration)))
            code=proc.wait()
        finally:
            deadline.cancel()
            if proc.stdout:
                proc.stdout.close()
        if code:
            detail=log.read_bytes()[-2500:].decode(errors="replace").strip()
            raise ValueError(detail or "FFmpeg could not render this edit.")


def render(clips,cues,opts,folder,notify):
    """Frame-quantize the cut, normalize sources, then join a constant-format timeline."""
    duration=sum(clip["length"] for clip in clips)
    if not clips or duration<=0:
        raise ValueError("Add at least one ready take to the timeline.")
    names=[]
    offset=0
    for index,clip in enumerate(clips):
        seconds=clip["length"]
        if clip.get("sha") and digest(clip["path"])!=clip["sha"]:
            raise ValueError("A selected source Take changed before rendering.")
        name=f"segment-{index:05}.nut"
        video=["setpts=PTS-STARTPTS",*shape(opts),"format=yuv420p"]
        audio=["aresample=48000:async=1:first_pts=0","apad"]
        # Overlap-free fades keep dialogue and subtitle timings attached to source frames.
        fade=clip["fade"]
        incoming=clips[index-1]["fade"] if index else 0
        if incoming:
            video.append(f"fade=t=in:d={incoming:.5f}")
            audio.append(f"afade=t=in:d={incoming:.5f}")
        if fade:
            frame_start=seconds-fade
            video.append(f"fade=t=out:st={frame_start:.5f}:d={fade:.5f}")
            audio.append(f"afade=t=out:st={frame_start:.5f}:d={fade:.5f}")
        # Give both axes exactly the same edit duration, even for short source tracks.
        video += ["tpad=stop_mode=clone:stop_duration=0.2"]
        args=["-ss",f"{clip['start']:.8f}","-i",str(clip["path"])]
        if clip["media"]["audio"]:
            audio_map=f"0:{clip['media']['audioIndex']}"
        else:
            args+=["-f","lavfi","-i","anullsrc=channel_layout=stereo:sample_rate=48000"]
            audio_map="1:a:0"
        args+=["-map",f"0:{clip['media']['videoIndex']}","-map",audio_map,"-vf",",".join(video),"-af",",".join(audio),
               "-t",f"{seconds:.8f}","-c:v","libx264","-preset","veryfast","-crf","17","-pix_fmt","yuv420p",
               "-c:a","pcm_s16le","-ac","2","-ar","48000","-map_metadata","-1",name]
        process(args,folder,seconds,lambda fraction:notify(6+72*(offset+seconds*fraction)/duration,f"Preparing clip {index+1} of {len(clips)}"))
        names.append(name)
        offset+=seconds
    (folder/"timeline.ffconcat").write_text("ffconcat version 1.0\n"+"".join(
        f"file {name}\nduration {clip['length']:.8f}\n" for name,clip in zip(names,clips,strict=True)),encoding="ascii")
    write_subtitles(folder/"subtitles.srt",cues)
    args=["-f","concat","-safe","1","-i","timeline.ffconcat","-map","0:v:0","-map","0:a:0"]
    if opts["burn"] and cues:
        args+=["-vf",f"subtitles=filename=subtitles.srt:charenc=UTF-8:force_style='{style(opts)}'",
               "-c:v","libx264","-preset","veryfast","-crf","18"]
    else:
        args+=["-c:v","copy"]
    audio="aresample=48000:async=1:first_pts=0"
    if opts["normalize"]:
        audio+=",loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000"
    args+=["-af",audio,"-t",f"{duration:.8f}","-c:a","aac","-b:a","192k","-movflags","+faststart","video.part.mp4"]
    process(args,folder,duration,lambda fraction:notify(78+18*fraction,"Assembling video, audio and subtitles"))
    path=folder/"video.part.mp4"
    info=probe(path)
    if ((info["width"],info["height"])!=(opts["width"],opts["height"]) or not info["audio"]
            or abs(info["duration"]-duration)>max(.15,3/opts["fps"])
            or info["audioDuration"] and abs(info["audioDuration"]-info["duration"])>.2):
        raise ValueError("Export verification failed. Check dimensions, clip timing and audio synchronization.")
    process(["-i","video.part.mp4","-map","0:v:0","-map","0:a:0","-f","null","-"],
            folder,duration,lambda fraction:notify(96+3*fraction,"Decoding the completed video for verification"))
    if any(c.get("sha") and digest(c["path"])!=c["sha"] for c in clips):
        raise ValueError("A source Take changed during the render. Refresh the edit and try again.")
    path.replace(folder/"video.mp4")
    for name in names:
        (folder/name).unlink()
    return info
