"""Minimal, dependency-free EXIF date reader (JPEG only).

The merger needs a *comparable* capture time before it will pair a JPEG with a
clip.  Photos copied out of a gallery, or synced by a cloud client, often lose
the clip's timestamps while the JPEG keeps ``DateTimeOriginal`` — so we read it
ourselves and stamp the clip when the merger refuses.
"""
from __future__ import annotations

import struct
from pathlib import Path

TAG_DATETIME_ORIGINAL = 0x9003
TAG_OFFSET_TIME_ORIGINAL = 0x9011
TAG_EXIF_IFD = 0x8769
TYPE_ASCII = 2


def _app1(data: bytes) -> bytes | None:
    i = 2
    while i < len(data) - 4:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xDA:  # start of scan: no more metadata
            return None
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        payload = data[i + 4:i + 2 + length]
        if payload.startswith(b"Exif\x00\x00"):
            return payload[6:]
        i += 2 + length
    return None


def _read_ifd(tiff: bytes, offset: int, bo: str, out: dict, tag_filter: set[int]) -> int:
    if offset <= 0 or offset + 2 > len(tiff):
        return 0
    count = struct.unpack(bo + "H", tiff[offset:offset + 2])[0]
    next_ifd = 0
    for k in range(count):
        entry = offset + 2 + k * 12
        if entry + 12 > len(tiff):
            break
        tag, typ, num = struct.unpack(bo + "HHI", tiff[entry:entry + 8])
        if tag == TAG_EXIF_IFD:
            next_ifd = struct.unpack(bo + "I", tiff[entry + 8:entry + 12])[0]
        if tag in tag_filter and typ == TYPE_ASCII:
            size = num
            if size <= 4:
                raw = tiff[entry + 8:entry + 8 + size]
            else:
                ptr = struct.unpack(bo + "I", tiff[entry + 8:entry + 12])[0]
                raw = tiff[ptr:ptr + size]
            out[tag] = raw.split(b"\x00")[0].decode("ascii", "ignore").strip()
    return next_ifd


def read_datetime_utc(path: str | Path) -> str | None:
    """``DateTimeOriginal`` as an ffmpeg ``creation_time`` (UTC, trailing Z)."""
    from datetime import datetime, timedelta

    got = read_datetime_original(path)
    if not got:
        return None
    iso, offset = got
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if not offset:
        # No OffsetTimeOriginal (common for gallery re-exports): the EXIF clock is
        # the camera's local wall time, so interpret it in this machine's zone.
        moment = moment.astimezone().astimezone(__import__("datetime").timezone.utc).replace(tzinfo=None)
        return moment.strftime("%Y-%m-%dT%H:%M:%S") + ".000Z"
    if offset and offset[0] in "+-":
        digits = offset[1:].replace(":", "")
        if len(digits) == 4 and digits.isdigit():
            sign = 1 if offset[0] == "+" else -1
            try:
                moment -= sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
            except ValueError:
                pass
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + ".000Z"


def read_datetime_original(path: str | Path) -> tuple[str, str] | None:
    """Return ``("YYYY-MM-DDTHH:MM:SS", "+08:00")`` or ``None``."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(256 * 1024)
    except OSError:
        return None
    tiff = _app1(head)
    if not tiff or len(tiff) < 8:
        return None
    bo = "<" if tiff[:2] == b"II" else ">"
    try:
        ifd0 = struct.unpack(bo + "I", tiff[4:8])[0]
        tags: dict[int, str] = {}
        exif_ifd = _read_ifd(tiff, ifd0, bo, tags, {TAG_DATETIME_ORIGINAL})
        if exif_ifd:
            _read_ifd(tiff, exif_ifd, bo, tags, {TAG_DATETIME_ORIGINAL, TAG_OFFSET_TIME_ORIGINAL})
    except (struct.error, IndexError):
        return None
    raw = tags.get(TAG_DATETIME_ORIGINAL)
    if not raw or len(raw) < 19:
        return None
    iso = raw[:10].replace(":", "-") + "T" + raw[11:19]
    return iso, tags.get(TAG_OFFSET_TIME_ORIGINAL, "")
