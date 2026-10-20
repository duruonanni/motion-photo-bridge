"""Transcode phone motion clips into a stream that plays everywhere.

Why this exists
---------------
Many Android phones record the short clip attached to a live photo in
full-range YUV (``yuvj420p`` / ``color_range=pc``).  VLC and ffmpeg handle
that fine, but the Windows hardware decoder (DXVA) renders it as a black
frame with audio only.  Immich hands the clip straight to the browser when
no transcode is required, so the symptom shows up as "the photo plays on my
phone but is black on my PC".

Re-encoding to limited range 4:2:0 BT.709 fixes it.  The clip is 2-3 s long
and stands in for a still image, so a modest CRF is visually lossless.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

FULL_RANGE_PIX_FMTS = {"yuvj420p", "yuvj422p", "yuvj444p"}


@dataclass(frozen=True)
class VideoInfo:
    codec: str
    pix_fmt: str
    color_range: str
    width: int
    height: int
    duration: float
    creation_time: str | None

    @property
    def needs_range_fix(self) -> bool:
        return self.pix_fmt in FULL_RANGE_PIX_FMTS or self.color_range == "pc"


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=True, **kw)


def probe(path: Path) -> VideoInfo:
    out = _run([
        FFPROBE, "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(path),
    ]).stdout
    data = json.loads(out)
    video = next(s for s in data["streams"] if s.get("codec_type") == "video")
    tags = {**data.get("format", {}).get("tags", {}), **video.get("tags", {})}
    return VideoInfo(
        codec=video.get("codec_name", ""),
        pix_fmt=video.get("pix_fmt", ""),
        color_range=video.get("color_range", ""),
        width=int(video.get("width", 0)),
        height=int(video.get("height", 0)),
        duration=float(data.get("format", {}).get("duration", 0) or 0),
        creation_time=tags.get("creation_time"),
    )


def transcode(src: Path, dst: Path, crf: int = 20, creation_time: str | None = None) -> VideoInfo:
    """Re-encode ``src`` to limited-range H.264 and return its new info.

    ``creation_time`` overrides the timestamp carried into the output.  Some
    phones ship a clip whose creation time is the transfer time rather than
    the capture time, which makes pairing against the photo's EXIF fail; the
    caller can pass the photo's capture time instead.
    """
    info = probe(src)
    cmd = [
        FFMPEG, "-y", "-loglevel", "error", "-i", str(src),
        "-vf", "scale=in_range=full:out_range=limited,format=yuv420p",
        "-c:v", "libx264", "-profile:v", "high", "-level", "4.1",
        "-crf", str(crf), "-preset", "medium",
        "-color_range", "tv", "-colorspace", "bt709",
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-c:a", "aac", "-b:a", "96k", "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart",
    ]
    # Re-encoding drops the container timestamp, and the merger needs it to
    # pair the clip with the photo.  Carry it across explicitly.
    stamp = creation_time or info.creation_time
    if stamp:
        cmd += ["-metadata", f"creation_time={stamp}"]
    cmd.append(str(dst))
    _run(cmd)
    return probe(dst)
