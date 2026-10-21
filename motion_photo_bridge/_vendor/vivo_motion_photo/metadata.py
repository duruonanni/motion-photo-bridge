from __future__ import annotations

import datetime as dt
import re
import struct
from dataclasses import dataclass
from pathlib import Path

from .paths import public_path, read_bytes


EXIF_HEADER = b"Exif\x00\x00"
VIVO_ID_RE = re.compile(
    rb"com\.android\.camera\.livephoto[^0-9A-Fa-f]{0,96}([0-9A-Fa-f]{20,64})",
    re.DOTALL,
)


class MetadataError(ValueError):
    pass


@dataclass(slots=True)
class BasicJpegInfo:
    width: int
    height: int
    eoi_end: int
    exif_datetime: dt.datetime | None
    live_photo_id: str | None


def _jpeg_segments(data: bytes):
    if not data.startswith(b"\xff\xd8"):
        raise MetadataError("不是有效的 JPEG（缺少 SOI）")
    pos = 2
    in_scan = False
    size = len(data)
    while pos < size:
        if in_scan:
            marker_pos = data.find(b"\xff", pos)
            if marker_pos < 0 or marker_pos + 1 >= size:
                raise MetadataError("JPEG 扫描数据未正常结束")
            code_pos = marker_pos + 1
            while code_pos < size and data[code_pos] == 0xFF:
                code_pos += 1
            if code_pos >= size:
                raise MetadataError("JPEG marker 被截断")
            code = data[code_pos]
            if code == 0x00 or 0xD0 <= code <= 0xD7:
                pos = code_pos + 1
                continue
            pos = marker_pos
            in_scan = False
            continue

        if data[pos] != 0xFF:
            raise MetadataError(f"JPEG marker 对齐错误：offset {pos}")
        marker_start = pos
        pos += 1
        while pos < size and data[pos] == 0xFF:
            pos += 1
        if pos >= size:
            raise MetadataError("JPEG marker 被截断")
        marker = data[pos]
        pos += 1
        if marker == 0xD9:
            yield marker, marker_start, pos, b""
            return
        if marker in {0xD8, 0x01} or 0xD0 <= marker <= 0xD7:
            yield marker, marker_start, pos, b""
            continue
        if pos + 2 > size:
            raise MetadataError("JPEG segment 长度被截断")
        segment_length = struct.unpack_from(">H", data, pos)[0]
        if segment_length < 2 or pos + segment_length > size:
            raise MetadataError("JPEG segment 长度非法")
        payload_start = pos + 2
        segment_end = pos + segment_length
        payload = data[payload_start:segment_end]
        yield marker, marker_start, segment_end, payload
        pos = segment_end
        if marker == 0xDA:
            in_scan = True
    raise MetadataError("JPEG 缺少 EOI")


def _read_tiff_value(
    tiff: bytes,
    endian: str,
    value_type: int,
    count: int,
    value_field_offset: int,
) -> bytes | None:
    unit_sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}
    unit = unit_sizes.get(value_type)
    if unit is None or count < 0:
        return None
    total = unit * count
    if total <= 4:
        end = value_field_offset + total
        return tiff[value_field_offset:end] if end <= len(tiff) else None
    if value_field_offset + 4 > len(tiff):
        return None
    offset = struct.unpack_from(endian + "I", tiff, value_field_offset)[0]
    end = offset + total
    return tiff[offset:end] if 0 <= offset <= end <= len(tiff) else None


def _parse_exif_datetime(payload: bytes) -> dt.datetime | None:
    if not payload.startswith(EXIF_HEADER):
        return None
    tiff = payload[len(EXIF_HEADER) :]
    if len(tiff) < 8:
        return None
    if tiff[:2] == b"II":
        endian = "<"
    elif tiff[:2] == b"MM":
        endian = ">"
    else:
        return None
    if struct.unpack_from(endian + "H", tiff, 2)[0] != 42:
        return None
    ifd0_offset = struct.unpack_from(endian + "I", tiff, 4)[0]

    def read_ifd(offset: int) -> dict[int, bytes]:
        values: dict[int, bytes] = {}
        if offset < 0 or offset + 2 > len(tiff):
            return values
        count = struct.unpack_from(endian + "H", tiff, offset)[0]
        for index in range(count):
            entry = offset + 2 + index * 12
            if entry + 12 > len(tiff):
                break
            tag, value_type = struct.unpack_from(endian + "HH", tiff, entry)
            item_count = struct.unpack_from(endian + "I", tiff, entry + 4)[0]
            value = _read_tiff_value(tiff, endian, value_type, item_count, entry + 8)
            if value is not None:
                values[tag] = value
        return values

    ifd0 = read_ifd(ifd0_offset)
    exif_ifd_offset = None
    raw_pointer = ifd0.get(0x8769)
    if raw_pointer and len(raw_pointer) >= 4:
        exif_ifd_offset = struct.unpack_from(endian + "I", raw_pointer, 0)[0]
    exif_ifd = read_ifd(exif_ifd_offset) if exif_ifd_offset is not None else {}

    def ascii_tag(tag: int, *sources: dict[int, bytes]) -> str | None:
        for source in sources:
            raw = source.get(tag)
            if raw:
                try:
                    value = raw.split(b"\x00", 1)[0].decode("ascii", "strict").strip()
                except UnicodeDecodeError:
                    continue
                if value:
                    return value
        return None

    text = ascii_tag(0x9003, exif_ifd, ifd0) or ascii_tag(0x0132, ifd0)
    if not text:
        return None
    try:
        parsed = dt.datetime.strptime(text, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    subseconds = ascii_tag(0x9291, exif_ifd)
    if subseconds and subseconds.isdigit():
        parsed = parsed.replace(microsecond=int((subseconds + "000000")[:6]))
    offset_text = ascii_tag(0x9011, exif_ifd) or ascii_tag(0x9010, exif_ifd)
    if offset_text and re.fullmatch(r"[+-]\d{2}:\d{2}", offset_text):
        sign = 1 if offset_text[0] == "+" else -1
        hours, minutes = map(int, offset_text[1:].split(":"))
        parsed = parsed.replace(tzinfo=dt.timezone(sign * dt.timedelta(hours=hours, minutes=minutes)))
    return parsed


def inspect_jpeg_basic(path: Path) -> BasicJpegInfo:
    path = public_path(path)
    data = read_bytes(path)
    width = height = None
    exif_datetime = None
    eoi_end = None
    for marker, _start, end, payload in _jpeg_segments(data):
        if marker == 0xE1 and exif_datetime is None:
            exif_datetime = _parse_exif_datetime(payload)
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            if len(payload) >= 5:
                height, width = struct.unpack_from(">HH", payload, 1)
        if marker == 0xD9:
            eoi_end = end
    if width is None or height is None or eoi_end is None:
        raise MetadataError("JPEG 缺少尺寸或 EOI")
    # vivo compound JPEGs can append additional JPEG streams after the first
    # top-level EOI.  The camera's own trailer begins after the final EOI and
    # is the padding value expected by this converter.
    last_eoi = data.rfind(b"\xff\xd9")
    if last_eoi >= eoi_end - 2:
        eoi_end = last_eoi + 2
    trailing = data[eoi_end:]
    match = VIVO_ID_RE.search(trailing)
    live_id = match.group(1).decode("ascii") if match else None
    return BasicJpegInfo(width, height, eoi_end, exif_datetime, live_id)
