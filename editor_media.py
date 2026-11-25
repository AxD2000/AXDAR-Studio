"""FFmpeg and ffprobe executable discovery for technical take verification."""
import os
import shutil
from pathlib import Path

BASE = Path(__file__).resolve().parent


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
