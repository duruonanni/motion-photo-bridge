#!/usr/bin/env python3
"""Insert a minimal EXIF block with a capture time into a JPEG that has none.

Why this exists: `android-motion-photo-merger` pairs a JPEG with its clip by
comparing capture times. A JPEG that carries no EXIF date (frames exported by
hand, some gallery re-exports, anything made with a plain image tool) cannot be
paired at all — the merger reports `Ambiguous: missing comparable capture time`.

This writes *only* the three date tags, in a fresh EXIF block, and refuses to
touch a file that already has EXIF so it can never destroy real metadata.

    python3 tools/stamp_capture_time.py photo.jpg "2026:01:01 12:00:00"
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

EXIF_HEADER = b"Exif\x00\x00"
SOS = 0xDA


def find_exif(data: bytes) -> tuple[int, int] | None:
    """Return (start, end) of an existing EXIF APP1 payload, or None."""
    i = 2
    while i < len(data) - 4:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == SOS:
            return None
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2 : i + 4])[0]
        if marker == 0xE1 and data[i + 4 : i + 10] == EXIF_HEADER:
            return i, i + 2 + length
        i += 2 + length
    return None


def _entry(tag: int, typ: int, count: int, value: int) -> bytes:
    return struct.pack("<HHI", tag, typ, count) + struct.pack("<I", value)


def build_tiff(stamp: str) -> bytes:
    """Little-endian TIFF with IFD0 -> Exif SubIFD holding the three date tags."""
    value = stamp.encode("ascii") + b"\x00"
    if len(value) != 20:
        raise ValueError("stamp must be exactly 'YYYY:MM:DD HH:MM:SS'")
    ifd0_off, ifd0_count = 8, 2
    sub_off = ifd0_off + 2 + ifd0_count * 12 + 4
    sub_count = 2
    data_off = sub_off + 2 + sub_count * 12 + 4
    date_off, original_off, digitized_off = data_off, data_off + 20, data_off + 40

    out = b"II*\x00" + struct.pack("<I", ifd0_off)
    out += struct.pack("<H", ifd0_count)
    out += _entry(0x8769, 4, 1, sub_off)      # ExifIFDPointer
    out += _entry(0x0132, 2, 20, date_off)    # DateTime
    out += struct.pack("<I", 0)               # no IFD1
    out += struct.pack("<H", sub_count)
    out += _entry(0x9003, 2, 20, original_off)   # DateTimeOriginal
    out += _entry(0x9004, 2, 20, digitized_off)  # DateTimeDigitized
    out += struct.pack("<I", 0)
    return out + value * 3


def stamp_file(path: Path, timestamp: str, in_place: bool = False) -> Path:
    data = path.read_bytes()
    if not data.startswith(b"\xff\xd8"):
        raise ValueError(f"{path} is not a JPEG")
    if find_exif(data) is not None:
        raise SystemExit(f"{path} already has EXIF — refusing to touch it")
    payload = EXIF_HEADER + build_tiff(timestamp)
    app1 = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    out_path = path if in_place else path.with_name(path.stem + "-stamped.jpg")
    out_path.write_bytes(data[:2] + app1 + data[2:])
    return out_path


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        raise SystemExit(__doc__)
    src = Path(sys.argv[1])
    dest = stamp_file(src, sys.argv[2], in_place=len(sys.argv) == 4)
    print(dest)
