"""Small, dependency-free ISO BMFF reader for vivo live-photo videos.

The module deliberately parses only metadata.  In particular, ``mdat`` payloads
are never loaded into memory.  All box sizes are checked against their parent
before a payload is read, so malformed files fail with :class:`MP4ParseError`
instead of causing an unbounded read.
"""

from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Mapping

from .paths import open_path, public_path


__all__ = [
    "BoxInfo",
    "CompositionOffsetEntry",
    "EditListEntry",
    "MP4Info",
    "MP4ParseError",
    "TimeToSampleEntry",
    "VideoTrackInfo",
    "VivoMetadata",
    "get_presentation_timestamp_us",
    "parse_mp4",
    "probe_mp4",
]


_QUICKTIME_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)
_VIVO_USER_TYPE = b"vivoMediaExtInfo"
_MAX_TABLE_ENTRIES = 1_000_000
_MAX_TIMELINE_SAMPLES = 1_000_000
_MAX_VIVO_PAYLOAD = 2 * 1024 * 1024
_MAX_BOXES_PER_CONTAINER = 100_000
_MATRIX_16_16_SCALE = 1 << 16
_MATRIX_2_30_SCALE = 1 << 30
# Standard rotation matrices should normally be exact.  A small tolerance keeps
# the reader compatible with encoders that round fixed-point values while still
# rejecting visible scale, shear, or perspective transforms.
_DISPLAY_MATRIX_TOLERANCE = 1 / 1024


class MP4ParseError(ValueError):
    """Raised when an ISO BMFF structure required by this reader is invalid."""


@dataclass(frozen=True, slots=True)
class BoxInfo:
    """Location and shape of a top-level ISO BMFF box."""

    type: str
    start: int
    size: int
    header_size: int
    user_type: bytes | None = None

    @property
    def end(self) -> int:
        return self.start + self.size

    @property
    def payload_start(self) -> int:
        return self.start + self.header_size

    @property
    def payload_size(self) -> int:
        return self.size - self.header_size


@dataclass(frozen=True, slots=True)
class TimeToSampleEntry:
    sample_count: int
    sample_delta: int


@dataclass(frozen=True, slots=True)
class CompositionOffsetEntry:
    sample_count: int
    sample_offset: int


@dataclass(frozen=True, slots=True)
class EditListEntry:
    segment_duration: int
    media_time: int
    media_rate_integer: int
    media_rate_fraction: int


@dataclass(frozen=True, slots=True)
class VivoMetadata:
    live_photo_id: str | None = None
    image_time: int | None = None
    version: int | str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)
    box: BoxInfo | None = None


@dataclass(frozen=True, slots=True)
class VideoTrackInfo:
    track_id: int | None
    width: int | None
    height: int | None
    codec: str | None
    timescale: int
    duration_ticks: int | None
    duration_seconds: float | None
    creation_time: datetime | None
    frame_count: int
    stts: tuple[TimeToSampleEntry, ...]
    ctts: tuple[CompositionOffsetEntry, ...]
    ctts_present: bool
    edit_list: tuple[EditListEntry, ...]
    enabled: bool = True
    in_movie: bool = True
    # Clockwise display rotation described by the tkhd matrix.  ``None`` means
    # that the matrix is missing or is not a near-standard orthogonal rotation.
    rotation: int | None = None
    display_width: int | None = None
    display_height: int | None = None


@dataclass(frozen=True, slots=True)
class MP4Info:
    path: Path | None
    file_size: int
    major_brand: str | None
    minor_version: int | None
    compatible_brands: tuple[str, ...]
    duration_seconds: float | None
    creation_time: datetime | None
    movie_timescale: int | None
    movie_duration_ticks: int | None
    video: VideoTrackInfo | None
    vivo: VivoMetadata
    ftyp: BoxInfo | None
    moov: BoxInfo | None
    mdat: tuple[BoxInfo, ...]
    top_level_boxes: tuple[BoxInfo, ...]

    @property
    def duration(self) -> float | None:
        """Compatibility alias for callers that use ``duration``."""

        return self.duration_seconds

    @property
    def presentation_timestamp_us(self) -> int:
        return get_presentation_timestamp_us(self)


@dataclass(frozen=True, slots=True)
class _Box:
    type_bytes: bytes
    start: int
    size: int
    header_size: int
    user_type: bytes | None = None

    @property
    def end(self) -> int:
        return self.start + self.size

    @property
    def payload_start(self) -> int:
        return self.start + self.header_size

    @property
    def payload_size(self) -> int:
        return self.size - self.header_size

    def public(self) -> BoxInfo:
        return BoxInfo(
            type=_fourcc(self.type_bytes),
            start=self.start,
            size=self.size,
            header_size=self.header_size,
            user_type=self.user_type,
        )


@dataclass(frozen=True, slots=True)
class _MovieHeader:
    timescale: int
    duration_ticks: int | None
    creation_time: datetime | None


@dataclass(frozen=True, slots=True)
class _TrackHeader:
    track_id: int | None
    width: int | None
    height: int | None
    rotation: int | None
    enabled: bool
    in_movie: bool


def _fourcc(value: bytes) -> str:
    return value.decode("latin-1")


def _read_exact(stream: BinaryIO, offset: int, size: int) -> bytes:
    if offset < 0 or size < 0:
        raise MP4ParseError("negative read range")
    try:
        stream.seek(offset)
        data = stream.read(size)
    except (OSError, ValueError) as error:
        raise MP4ParseError(f"cannot read MP4 at offset {offset}: {error}") from error
    if len(data) != size:
        raise MP4ParseError(
            f"truncated MP4 at offset {offset}: expected {size} bytes, got {len(data)}"
        )
    return data


def _stream_size(stream: BinaryIO) -> int:
    try:
        current = stream.tell()
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(current)
    except (OSError, ValueError) as error:
        raise MP4ParseError("MP4 input must be a seekable binary stream") from error
    if size < 0:
        raise MP4ParseError("invalid MP4 size")
    return size


def _iter_boxes(stream: BinaryIO, start: int, end: int) -> Iterator[_Box]:
    if start < 0 or end < start:
        raise MP4ParseError("invalid parent box range")
    position = start
    box_count = 0
    while position < end:
        box_count += 1
        if box_count > _MAX_BOXES_PER_CONTAINER:
            raise MP4ParseError("too many boxes in one ISO BMFF container")
        remaining = end - position
        if remaining < 8:
            raise MP4ParseError(
                f"trailing {remaining} bytes cannot form a box at offset {position}"
            )
        header = _read_exact(stream, position, 8)
        size32, type_bytes = struct.unpack(">I4s", header)
        header_size = 8
        if size32 == 1:
            if remaining < 16:
                raise MP4ParseError(f"truncated extended box header at offset {position}")
            size = struct.unpack(">Q", _read_exact(stream, position + 8, 8))[0]
            header_size = 16
        elif size32 == 0:
            size = remaining
        else:
            size = size32

        user_type = None
        if type_bytes == b"uuid":
            if remaining < header_size + 16:
                raise MP4ParseError(f"truncated uuid user type at offset {position}")
            user_type = _read_exact(stream, position + header_size, 16)
            header_size += 16

        if size < header_size:
            raise MP4ParseError(
                f"box {_fourcc(type_bytes)!r} at offset {position} has invalid size {size}"
            )
        if size > remaining:
            raise MP4ParseError(
                f"box {_fourcc(type_bytes)!r} at offset {position} exceeds its parent"
            )

        yield _Box(type_bytes, position, size, header_size, user_type)
        position += size
        if size32 == 0:
            break


def _children(stream: BinaryIO, box: _Box, offset: int = 0) -> tuple[_Box, ...]:
    start = box.payload_start + offset
    if start > box.end:
        raise MP4ParseError(f"invalid child offset in {_fourcc(box.type_bytes)!r}")
    return tuple(_iter_boxes(stream, start, box.end))


def _unique_child(children: tuple[_Box, ...], box_type: bytes) -> _Box | None:
    matches = [box for box in children if box.type_bytes == box_type]
    if len(matches) > 1:
        raise MP4ParseError(f"duplicate {_fourcc(box_type)!r} box")
    return matches[0] if matches else None


def _full_box_version(stream: BinaryIO, box: _Box) -> int:
    if box.payload_size < 4:
        raise MP4ParseError(f"truncated {_fourcc(box.type_bytes)!r} full-box header")
    return _read_exact(stream, box.payload_start, 1)[0]


def _quicktime_datetime(seconds: int) -> datetime | None:
    # Zero is conventionally used for an unknown creation time.
    if seconds == 0:
        return None
    try:
        return _QUICKTIME_EPOCH + timedelta(seconds=seconds)
    except OverflowError:
        return None


def _parse_movie_header(stream: BinaryIO, box: _Box) -> _MovieHeader:
    version = _full_box_version(stream, box)
    if version == 0:
        if box.payload_size < 20:
            raise MP4ParseError("truncated mvhd version 0")
        data = _read_exact(stream, box.payload_start + 4, 16)
        creation, _modification, timescale, duration = struct.unpack(">IIII", data)
        unknown_duration = 0xFFFFFFFF
    elif version == 1:
        if box.payload_size < 32:
            raise MP4ParseError("truncated mvhd version 1")
        data = _read_exact(stream, box.payload_start + 4, 28)
        creation, _modification, timescale, duration = struct.unpack(">QQIQ", data)
        unknown_duration = 0xFFFFFFFFFFFFFFFF
    else:
        raise MP4ParseError(f"unsupported mvhd version {version}")
    if timescale == 0:
        raise MP4ParseError("mvhd timescale is zero")
    return _MovieHeader(
        timescale=timescale,
        duration_ticks=None if duration == unknown_duration else duration,
        creation_time=_quicktime_datetime(creation),
    )


def _parse_media_header(stream: BinaryIO, box: _Box) -> _MovieHeader:
    version = _full_box_version(stream, box)
    if version == 0:
        if box.payload_size < 20:
            raise MP4ParseError("truncated mdhd version 0")
        data = _read_exact(stream, box.payload_start + 4, 16)
        creation, _modification, timescale, duration = struct.unpack(">IIII", data)
        unknown_duration = 0xFFFFFFFF
    elif version == 1:
        if box.payload_size < 32:
            raise MP4ParseError("truncated mdhd version 1")
        data = _read_exact(stream, box.payload_start + 4, 28)
        creation, _modification, timescale, duration = struct.unpack(">QQIQ", data)
        unknown_duration = 0xFFFFFFFFFFFFFFFF
    else:
        raise MP4ParseError(f"unsupported mdhd version {version}")
    if timescale == 0:
        raise MP4ParseError("mdhd timescale is zero")
    return _MovieHeader(
        timescale=timescale,
        duration_ticks=None if duration == unknown_duration else duration,
        creation_time=_quicktime_datetime(creation),
    )


def _fixed_point_close(actual: int, expected: int, scale: int) -> bool:
    tolerance = max(1, round(scale * _DISPLAY_MATRIX_TOLERANCE))
    return abs(actual - expected) <= tolerance


def _display_rotation(matrix: tuple[int, ...]) -> int | None:
    """Return the clockwise standard rotation represented by a tkhd matrix.

    ISO BMFF stores ``a, b, u, c, d, v, x, y, w`` in row order.  The affine
    terms use signed 16.16 fixed point, while the perspective terms use signed
    2.30 fixed point.  Translation (``x``/``y``) does not change orientation,
    but perspective, scale, and shear make the display dimensions ambiguous and
    are therefore rejected.
    """

    a, b, u, c, d, v, _x, _y, w = matrix
    if not (
        _fixed_point_close(u, 0, _MATRIX_2_30_SCALE)
        and _fixed_point_close(v, 0, _MATRIX_2_30_SCALE)
        and _fixed_point_close(w, _MATRIX_2_30_SCALE, _MATRIX_2_30_SCALE)
    ):
        return None

    unit = _MATRIX_16_16_SCALE
    candidates = (
        (0, (unit, 0, 0, unit)),
        (90, (0, unit, -unit, 0)),
        (-90, (0, -unit, unit, 0)),
        (180, (-unit, 0, 0, -unit)),
    )
    linear = (a, b, c, d)
    for rotation, expected in candidates:
        if all(
            _fixed_point_close(actual, wanted, _MATRIX_16_16_SCALE)
            for actual, wanted in zip(linear, expected)
        ):
            return rotation
    return None


def _parse_track_header(stream: BinaryIO, box: _Box | None) -> _TrackHeader:
    if box is None:
        return _TrackHeader(None, None, None, None, True, True)
    version = _full_box_version(stream, box)
    flags = int.from_bytes(_read_exact(stream, box.payload_start + 1, 3), "big")
    if version == 0:
        if box.payload_size < 84:
            raise MP4ParseError("truncated tkhd version 0")
        track_id = struct.unpack(">I", _read_exact(stream, box.payload_start + 12, 4))[0]
    elif version == 1:
        if box.payload_size < 96:
            raise MP4ParseError("truncated tkhd version 1")
        track_id = struct.unpack(">I", _read_exact(stream, box.payload_start + 20, 4))[0]
    else:
        raise MP4ParseError(f"unsupported tkhd version {version}")
    width_fixed, height_fixed = struct.unpack(
        ">II", _read_exact(stream, box.end - 8, 8)
    )
    matrix = struct.unpack(">9i", _read_exact(stream, box.end - 44, 36))
    width = (width_fixed + 0x8000) >> 16 if width_fixed else None
    height = (height_fixed + 0x8000) >> 16 if height_fixed else None
    return _TrackHeader(
        track_id,
        width,
        height,
        _display_rotation(matrix),
        bool(flags & 0x1),
        bool(flags & 0x2),
    )


def _parse_handler_type(stream: BinaryIO, box: _Box) -> bytes:
    if box.payload_size < 12:
        raise MP4ParseError("truncated hdlr box")
    return _read_exact(stream, box.payload_start + 8, 4)


def _parse_stsd(
    stream: BinaryIO, box: _Box | None
) -> tuple[str | None, int | None, int | None]:
    if box is None:
        return None, None, None
    if box.payload_size < 8:
        raise MP4ParseError("truncated stsd box")
    entry_count = struct.unpack(">I", _read_exact(stream, box.payload_start + 4, 4))[0]
    entries = tuple(_iter_boxes(stream, box.payload_start + 8, box.end))
    if entry_count != len(entries):
        raise MP4ParseError("stsd entry count does not match its payload")
    if not entries:
        return None, None, None
    entry = entries[0]
    width = height = None
    if entry.payload_size >= 28:
        width, height = struct.unpack(">HH", _read_exact(stream, entry.payload_start + 24, 4))
        width = width or None
        height = height or None
    return _fourcc(entry.type_bytes), width, height


def _parse_stts(stream: BinaryIO, box: _Box | None) -> tuple[TimeToSampleEntry, ...]:
    if box is None:
        return ()
    version = _full_box_version(stream, box)
    if version != 0 or box.payload_size < 8:
        raise MP4ParseError("invalid stts box")
    count = struct.unpack(">I", _read_exact(stream, box.payload_start + 4, 4))[0]
    if count > _MAX_TABLE_ENTRIES:
        raise MP4ParseError("stts has too many entries")
    required = 8 + count * 8
    if required > box.payload_size:
        raise MP4ParseError("truncated stts table")
    raw = _read_exact(stream, box.payload_start + 8, count * 8)
    entries = tuple(
        TimeToSampleEntry(*struct.unpack_from(">II", raw, index * 8))
        for index in range(count)
    )
    if any(entry.sample_count == 0 for entry in entries):
        raise MP4ParseError("stts contains a zero sample count")
    return entries


def _parse_ctts(
    stream: BinaryIO, box: _Box | None
) -> tuple[tuple[CompositionOffsetEntry, ...], bool]:
    if box is None:
        return (), False
    version = _full_box_version(stream, box)
    if version not in (0, 1) or box.payload_size < 8:
        raise MP4ParseError("invalid ctts box")
    count = struct.unpack(">I", _read_exact(stream, box.payload_start + 4, 4))[0]
    if count > _MAX_TABLE_ENTRIES:
        raise MP4ParseError("ctts has too many entries")
    required = 8 + count * 8
    if required > box.payload_size:
        raise MP4ParseError("truncated ctts table")
    raw = _read_exact(stream, box.payload_start + 8, count * 8)
    offset_format = ">I" if version == 0 else ">i"
    entries = []
    for index in range(count):
        sample_count = struct.unpack_from(">I", raw, index * 8)[0]
        sample_offset = struct.unpack_from(offset_format, raw, index * 8 + 4)[0]
        if sample_count == 0:
            raise MP4ParseError("ctts contains a zero sample count")
        entries.append(CompositionOffsetEntry(sample_count, sample_offset))
    return tuple(entries), True


def _parse_elst(stream: BinaryIO, box: _Box | None) -> tuple[EditListEntry, ...]:
    if box is None:
        return ()
    version = _full_box_version(stream, box)
    if version not in (0, 1) or box.payload_size < 8:
        raise MP4ParseError("invalid elst box")
    count = struct.unpack(">I", _read_exact(stream, box.payload_start + 4, 4))[0]
    if count > _MAX_TABLE_ENTRIES:
        raise MP4ParseError("elst has too many entries")
    entry_size = 12 if version == 0 else 20
    required = 8 + count * entry_size
    if required > box.payload_size:
        raise MP4ParseError("truncated elst table")
    raw = _read_exact(stream, box.payload_start + 8, count * entry_size)
    entries: list[EditListEntry] = []
    for index in range(count):
        offset = index * entry_size
        if version == 0:
            segment_duration, media_time, rate_integer, rate_fraction = struct.unpack_from(
                ">Ii hh", raw, offset
            )
        else:
            segment_duration, media_time, rate_integer, rate_fraction = struct.unpack_from(
                ">Qq hh", raw, offset
            )
        entries.append(
            EditListEntry(segment_duration, media_time, rate_integer, rate_fraction)
        )
    return tuple(entries)


def _parse_track(stream: BinaryIO, track_box: _Box) -> VideoTrackInfo | None:
    track_children = _children(stream, track_box)
    media_box = _unique_child(track_children, b"mdia")
    if media_box is None:
        return None
    media_children = _children(stream, media_box)
    handler_box = _unique_child(media_children, b"hdlr")
    if handler_box is None or _parse_handler_type(stream, handler_box) != b"vide":
        return None
    mdhd_box = _unique_child(media_children, b"mdhd")
    if mdhd_box is None:
        raise MP4ParseError("video track is missing mdhd")
    media_header = _parse_media_header(stream, mdhd_box)

    track_header = _parse_track_header(
        stream, _unique_child(track_children, b"tkhd")
    )
    track_id = track_header.track_id
    width = track_header.width
    height = track_header.height
    minf_box = _unique_child(media_children, b"minf")
    if minf_box is None:
        raise MP4ParseError("video track is missing minf")
    minf_children = _children(stream, minf_box)
    stbl_box = _unique_child(minf_children, b"stbl")
    if stbl_box is None:
        raise MP4ParseError("video track is missing stbl")
    sample_children = _children(stream, stbl_box)
    codec, sample_width, sample_height = _parse_stsd(
        stream, _unique_child(sample_children, b"stsd")
    )
    width = width or sample_width
    height = height or sample_height
    if track_header.rotation is None:
        display_width = display_height = None
    elif abs(track_header.rotation) == 90:
        display_width, display_height = height, width
    else:
        display_width, display_height = width, height
    stts = _parse_stts(stream, _unique_child(sample_children, b"stts"))
    ctts, ctts_present = _parse_ctts(
        stream, _unique_child(sample_children, b"ctts")
    )

    edit_list: tuple[EditListEntry, ...] = ()
    edts_box = _unique_child(track_children, b"edts")
    if edts_box is not None:
        edit_list = _parse_elst(
            stream, _unique_child(_children(stream, edts_box), b"elst")
        )

    frame_count = sum(entry.sample_count for entry in stts)
    duration_seconds = (
        None
        if media_header.duration_ticks is None
        else media_header.duration_ticks / media_header.timescale
    )
    return VideoTrackInfo(
        track_id=track_id,
        width=width,
        height=height,
        codec=codec,
        timescale=media_header.timescale,
        duration_ticks=media_header.duration_ticks,
        duration_seconds=duration_seconds,
        creation_time=media_header.creation_time,
        frame_count=frame_count,
        stts=stts,
        ctts=ctts,
        ctts_present=ctts_present,
        edit_list=edit_list,
        enabled=track_header.enabled,
        in_movie=track_header.in_movie,
        rotation=track_header.rotation,
        display_width=display_width,
        display_height=display_height,
    )


def _looks_like_box(stream: BinaryIO, start: int, end: int) -> bool:
    if end - start < 8:
        return False
    size, box_type = struct.unpack(">I4s", _read_exact(stream, start, 8))
    if not all(0x20 <= byte <= 0x7E for byte in box_type):
        return False
    if size == 0:
        return True
    if size == 1:
        if end - start < 16:
            return False
        size = struct.unpack(">Q", _read_exact(stream, start + 8, 8))[0]
    return 8 <= size <= end - start


_CONTAINER_TYPES = {
    b"moov",
    b"trak",
    b"mdia",
    b"minf",
    b"stbl",
    b"edts",
    b"dinf",
    b"udta",
    b"meta",
    b"ilst",
    b"moof",
    b"traf",
    b"mfra",
}


def _descendants(stream: BinaryIO, box: _Box, depth: int = 0) -> Iterator[_Box]:
    if depth > 16:
        raise MP4ParseError("ISO BMFF box nesting is too deep")
    yield box
    if box.type_bytes not in _CONTAINER_TYPES:
        return
    child_start = box.payload_start
    if box.type_bytes == b"meta" and not _looks_like_box(stream, child_start, box.end):
        # ISO meta is a FullBox; older QuickTime meta omits these four bytes.
        child_start += 4
    for child in _iter_boxes(stream, child_start, box.end):
        yield from _descendants(stream, child, depth + 1)


def _json_object_bytes(data: bytes) -> bytes | None:
    start = data.find(b"{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(data)):
        byte = data[index]
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # quote
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte == 0x7B:
            depth += 1
        elif byte == 0x7D:
            depth -= 1
            if depth == 0:
                return data[start : index + 1]
            if depth < 0:
                return None
    return None


def _coerce_int(value: Any, *, nonnegative: bool = False) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        try:
            result = int(value.strip(), 10)
        except ValueError:
            return None
    else:
        return None
    if nonnegative and result < 0:
        return None
    return result


def _parse_vivo_metadata(stream: BinaryIO, boxes: tuple[_Box, ...]) -> VivoMetadata:
    candidates: list[VivoMetadata] = []
    for top_box in boxes:
        for box in _descendants(stream, top_box):
            if box.type_bytes != b"uuid":
                continue
            marked = box.user_type == _VIVO_USER_TYPE
            if not marked:
                small_prefix = _read_exact(
                    stream, box.payload_start, min(box.payload_size, 128)
                )
                if _VIVO_USER_TYPE not in small_prefix:
                    continue
            prefix_size = min(box.payload_size, _MAX_VIVO_PAYLOAD)
            payload = _read_exact(stream, box.payload_start, prefix_size)
            json_bytes = _json_object_bytes(payload)
            if json_bytes is None:
                raise MP4ParseError("vivoMediaExtInfo uuid does not contain complete JSON")
            try:
                parsed = json.loads(json_bytes.decode("utf-8", "strict"))
            except (UnicodeDecodeError, ValueError, RecursionError) as error:
                raise MP4ParseError("invalid vivoMediaExtInfo JSON") from error
            if not isinstance(parsed, dict):
                raise MP4ParseError("vivoMediaExtInfo JSON root is not an object")

            raw_live_id = parsed.get("com.android.camera.livephoto")
            if isinstance(raw_live_id, bool):
                live_id = None
            elif isinstance(raw_live_id, (str, int)):
                live_id = str(raw_live_id).strip() or None
            else:
                live_id = None
            image_time = _coerce_int(
                parsed.get("com.android.camera.imageTime"), nonnegative=True
            )
            raw_version = parsed.get("version")
            version_int = _coerce_int(raw_version, nonnegative=True)
            version: int | str | None
            if version_int is not None:
                version = version_int
            elif isinstance(raw_version, str) and raw_version.strip():
                version = raw_version.strip()
            else:
                version = None
            candidates.append(
                VivoMetadata(live_id, image_time, version, parsed, box.public())
            )

    if not candidates:
        return VivoMetadata()
    first = candidates[0]
    identity = (first.live_photo_id, first.image_time, first.version)
    if any(
        (item.live_photo_id, item.image_time, item.version) != identity
        for item in candidates[1:]
    ):
        raise MP4ParseError("conflicting vivoMediaExtInfo uuid boxes")
    return first


def _parse_ftyp(
    stream: BinaryIO, box: _Box | None
) -> tuple[str | None, int | None, tuple[str, ...]]:
    if box is None:
        return None, None, ()
    if box.payload_size < 8 or (box.payload_size - 8) % 4:
        raise MP4ParseError("invalid ftyp payload")
    if box.payload_size > 1024 * 1024:
        raise MP4ParseError("ftyp payload is unreasonably large")
    data = _read_exact(stream, box.payload_start, box.payload_size)
    major_brand = _fourcc(data[:4])
    minor_version = struct.unpack_from(">I", data, 4)[0]
    compatible = tuple(
        _fourcc(data[offset : offset + 4]) for offset in range(8, len(data), 4)
    )
    return major_brand, minor_version, compatible


def _primary_video(tracks: list[VideoTrackInfo]) -> VideoTrackInfo | None:
    if not tracks:
        return None

    def score(track: VideoTrackInfo) -> tuple[int, int, int, int]:
        area = (track.width or 0) * (track.height or 0)
        return (
            int(track.enabled and track.in_movie),
            int(track.enabled),
            area,
            track.frame_count,
        )

    return max(tracks, key=score)


def _parse_stream(stream: BinaryIO, path: Path | None) -> MP4Info:
    file_size = _stream_size(stream)
    boxes = tuple(_iter_boxes(stream, 0, file_size))
    ftyp_boxes = [box for box in boxes if box.type_bytes == b"ftyp"]
    moov_boxes = [box for box in boxes if box.type_bytes == b"moov"]
    if len(ftyp_boxes) > 1:
        raise MP4ParseError("duplicate top-level ftyp box")
    if len(moov_boxes) > 1:
        raise MP4ParseError("duplicate top-level moov box")
    ftyp_box = ftyp_boxes[0] if ftyp_boxes else None
    moov_box = moov_boxes[0] if moov_boxes else None
    major_brand, minor_version, compatible_brands = _parse_ftyp(stream, ftyp_box)

    movie_header: _MovieHeader | None = None
    video_tracks: list[VideoTrackInfo] = []
    if moov_box is not None:
        moov_children = _children(stream, moov_box)
        mvhd_box = _unique_child(moov_children, b"mvhd")
        if mvhd_box is not None:
            movie_header = _parse_movie_header(stream, mvhd_box)
        for track_box in (box for box in moov_children if box.type_bytes == b"trak"):
            track = _parse_track(stream, track_box)
            if track is not None:
                video_tracks.append(track)
    video = _primary_video(video_tracks)
    vivo = _parse_vivo_metadata(stream, boxes)

    movie_duration = movie_header.duration_ticks if movie_header else None
    movie_timescale = movie_header.timescale if movie_header else None
    if movie_duration is not None and movie_timescale is not None:
        duration_seconds = movie_duration / movie_timescale
    else:
        duration_seconds = video.duration_seconds if video else None
    creation_time = (
        movie_header.creation_time if movie_header and movie_header.creation_time else None
    )
    if creation_time is None and video is not None:
        creation_time = video.creation_time

    mdat_boxes = tuple(box.public() for box in boxes if box.type_bytes == b"mdat")
    return MP4Info(
        path=path,
        file_size=file_size,
        major_brand=major_brand,
        minor_version=minor_version,
        compatible_brands=compatible_brands,
        duration_seconds=duration_seconds,
        creation_time=creation_time,
        movie_timescale=movie_timescale,
        movie_duration_ticks=movie_duration,
        video=video,
        vivo=vivo,
        ftyp=ftyp_box.public() if ftyp_box else None,
        moov=moov_box.public() if moov_box else None,
        mdat=mdat_boxes,
        top_level_boxes=tuple(box.public() for box in boxes),
    )


def parse_mp4(source: str | os.PathLike[str] | BinaryIO) -> MP4Info:
    """Parse MP4 metadata from a path or seekable binary stream.

    A caller-owned stream is left open and its original position is restored.
    Paths are recorded on the resulting :class:`MP4Info`; streams use ``None``.
    """

    if isinstance(source, (str, os.PathLike)):
        path = public_path(source)
        with open_path(path, "rb") as stream:
            return _parse_stream(stream, path)

    stream = source
    try:
        original_position = stream.tell()
    except (AttributeError, OSError, ValueError) as error:
        raise MP4ParseError("MP4 input must be a seekable binary stream") from error
    try:
        return _parse_stream(stream, None)
    finally:
        try:
            stream.seek(original_position)
        except (OSError, ValueError):
            pass


def probe_mp4(path: str | os.PathLike[str]) -> MP4Info:
    """Parse an MP4 file without decoding or reading its media-data payload."""

    info = parse_mp4(path)
    missing = []
    if info.ftyp is None:
        missing.append("ftyp")
    if info.moov is None:
        missing.append("moov")
    if not info.mdat:
        missing.append("mdat")
    if info.video is None:
        missing.append("video track")
    if missing:
        raise MP4ParseError("MP4 is missing " + ", ".join(missing))
    return info


def _media_presentation_times(track: VideoTrackInfo) -> list[tuple[int, int]] | None:
    if track.frame_count <= 0 or track.frame_count > _MAX_TIMELINE_SAMPLES:
        return None
    if sum(entry.sample_count for entry in track.stts) != track.frame_count:
        return None
    if any(entry.sample_delta <= 0 for entry in track.stts):
        return None

    offsets: list[int]
    if track.ctts_present:
        if sum(entry.sample_count for entry in track.ctts) != track.frame_count:
            return None
        offsets = []
        for entry in track.ctts:
            offsets.extend([entry.sample_offset] * entry.sample_count)
    else:
        offsets = [0] * track.frame_count

    result: list[tuple[int, int]] = []
    decode_time = 0
    sample_index = 0
    for entry in track.stts:
        for _ in range(entry.sample_count):
            result.append((decode_time + offsets[sample_index], sample_index))
            decode_time += entry.sample_delta
            sample_index += 1
    result.sort(key=lambda item: (item[0], item[1]))
    return result


def _edited_presentation_times(
    frames: list[tuple[int, int]],
    track: VideoTrackInfo,
    movie_timescale: int | None,
) -> list[tuple[Fraction, int]] | None:
    if not track.edit_list:
        return [(Fraction(timestamp, track.timescale), index) for timestamp, index in frames]
    if movie_timescale is None or movie_timescale <= 0:
        return None

    movie_cursor = Fraction(0, 1)
    mapped: list[tuple[Fraction, int]] = []
    for edit in track.edit_list:
        if edit.media_rate_integer != 1 or edit.media_rate_fraction != 0:
            return None
        segment_duration = Fraction(edit.segment_duration, movie_timescale)
        if edit.media_time == -1:
            movie_cursor += segment_duration
            continue
        if edit.media_time < 0:
            return None
        for timestamp, sample_index in frames:
            relative = Fraction(timestamp - edit.media_time, track.timescale)
            if 0 <= relative < segment_duration:
                mapped.append((movie_cursor + relative, sample_index))
        movie_cursor += segment_duration
    mapped.sort(key=lambda item: (item[0], item[1]))
    return mapped


def _round_microseconds(value: Fraction) -> int:
    scaled = value * 1_000_000
    if scaled < 0:
        return -1
    quotient, remainder = divmod(scaled.numerator, scaled.denominator)
    return quotient + int(remainder * 2 >= scaled.denominator)


def get_presentation_timestamp_us(
    source: str | os.PathLike[str] | MP4Info,
) -> int:
    """Return the still frame's presentation timestamp, or ``-1`` on failure.

    vivo's ``imageTime`` is interpreted as a zero-based *presentation-order*
    frame index.  Decode timestamps from ``stts`` are combined with signed or
    unsigned ``ctts`` offsets, sorted into display order, and then mapped through
    an optional rate-1 ``elst`` edit list.  The final rational timestamp is
    rounded to the nearest microsecond.
    """

    try:
        info = source if isinstance(source, MP4Info) else probe_mp4(source)
        image_time = info.vivo.image_time
        track = info.video
        if track is None or image_time is None or image_time < 0:
            return -1
        frames = _media_presentation_times(track)
        if frames is None:
            return -1
        edited = _edited_presentation_times(frames, track, info.movie_timescale)
        if edited is None or image_time >= len(edited):
            return -1
        return _round_microseconds(edited[image_time][0])
    except Exception:
        # This is intentionally a best-effort convenience API.  Structural
        # diagnostics remain available by calling probe_mp4/parse_mp4 directly.
        return -1
