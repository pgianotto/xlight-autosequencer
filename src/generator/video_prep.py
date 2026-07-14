"""Downscale a source video to 480p before use in a matrix Video effect.

xLights' Video effect happily plays full-resolution source video, but a
matrix model has far fewer pixels than the source frame — rendering at full
resolution wastes decode/render time for no visible gain. This scales the
video down to 480p (height 480, width auto-scaled to preserve aspect ratio)
once per source file and caches the result next to it, so repeated
generations against the same song skip re-encoding.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

TARGET_HEIGHT = 480


def _find_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]:
        if Path(candidate).exists():
            return candidate
    raise FileNotFoundError("ffmpeg not found. Install via: brew install ffmpeg")


def scaled_video_path(source: Path) -> Path:
    return source.with_name(f"{source.stem}_480p{source.suffix}")


def ensure_scaled_video(source: Path) -> Path:
    """Return a cached, 480p-downscaled copy of ``source``.

    Re-encodes only when no cached 480p copy exists yet.
    """
    target = scaled_video_path(source)
    if target.exists():
        return target

    subprocess.run(
        [
            _find_ffmpeg(), "-y",
            "-i", str(source),
            "-vf", f"scale=-2:{TARGET_HEIGHT}",
            "-an",
            str(target),
        ],
        check=True,
        capture_output=True,
    )
    return target
