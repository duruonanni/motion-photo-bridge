#!/usr/bin/env python3
"""Print date-related EXIF tags from JPEG files."""
import struct
import sys
from pathlib import Path

NAMES = {
    0x010F: "Make", 0x0110: "Model", 0x0131: "Software", 0x0132: "ModifyDate",
    0x9003: "DateTimeOriginal", 0x9004: "CreateDate",
    0x9010: "OffsetTime", 0x9011: "OffsetTimeOriginal", 0x9012: "OffsetTimeDigitized",
    0x9290: "SubSecTime", 0x9291: "SubSecTimeOriginal",
}
TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}


def exif_tiff(data: bytes):
    i = 2
    while i < len(data) - 4:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xDA:
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


def read_ifd(tiff: bytes, offset: int, bo: str, out: dict, depth: int = 0) -> None:
    if offset <= 0 or offset >= len(tiff) - 2 or depth > 3:
        return
    count = struct.unpack(bo + "H", tiff[offset:offset + 2])[0]
    for k in range(count):
        entry = offset + 2 + k * 12
        if entry + 12 > len(tiff):
            return
        tag, typ, cnt = struct.unpack(bo + "HHI", tiff[entry:entry + 8])
        size = TYPE_SIZE.get(typ, 1) * cnt
        if size <= 4:
            value = tiff[entry + 8:entry + 8 + size]
        else:
            ptr = struct.unpack(bo + "I", tiff[entry + 8:entry + 12])[0]
            value = tiff[ptr:ptr + size]
        if tag in NAMES:
            out[NAMES[tag]] = value.rstrip(b"\x00").decode("latin-1", "replace")
        if tag == 0x8769:
            read_ifd(tiff, struct.unpack(bo + "I", tiff[entry + 8:entry + 12])[0], bo, out, depth + 1)


for path in sys.argv[1:]:
    raw = Path(path).read_bytes()
    tiff = exif_tiff(raw)
    tags: dict[str, str] = {}
    if tiff:
        read_ifd(tiff, struct.unpack("<I" if tiff[:2] == b"II" else ">I", tiff[4:8])[0],
                 "<" if tiff[:2] == b"II" else ">", tags)
    print(f"=== {Path(path).name} ===")
    for key in ("Make", "Model", "Software", "DateTimeOriginal", "CreateDate",
                "ModifyDate", "OffsetTimeOriginal", "SubSecTimeOriginal"):
        if key in tags:
            print(f"  {key:20}: {tags[key]}")
    print()
