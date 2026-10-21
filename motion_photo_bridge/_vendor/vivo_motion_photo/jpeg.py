"""JPEG/XMP support for Android Motion Photo 1.0 files.

The module deliberately uses only the Python standard library.  It treats the
input image as an opaque byte stream except for JPEG marker parsing and the one
standard XMP APP1 segment that must be replaced.  In particular, EXIF,
MakerNote, ICC, MPF/QTI segments, entropy coded image data, concatenated gainmap
JPEGs, and vendor bytes following the last JPEG EOI are copied byte-for-byte.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import mmap
import os
from pathlib import Path
import struct
from typing import BinaryIO, Union
import xml.etree.ElementTree as ET

from . import paths as pathio


PathOrBytes = Union[str, os.PathLike[str], bytes, bytearray, memoryview]

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"
XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
MAX_APP1_PAYLOAD = 65_533  # JPEG length is uint16 and includes its two bytes.
COPY_CHUNK_SIZE = 1024 * 1024

NS_X = "adobe:ns:meta/"
NS_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
NS_CAMERA = "http://ns.google.com/photos/1.0/camera/"
NS_CONTAINER = "http://ns.google.com/photos/1.0/container/"
NS_ITEM = "http://ns.google.com/photos/1.0/container/item/"
NS_XMP_NOTE = "http://ns.adobe.com/xmp/note/"

for _prefix, _uri in (
    ("x", NS_X),
    ("rdf", NS_RDF),
    ("Camera", NS_CAMERA),
    ("Container", NS_CONTAINER),
    ("Item", NS_ITEM),
    ("xmpNote", NS_XMP_NOTE),
):
    ET.register_namespace(_prefix, _uri)


def _qname(namespace: str, local_name: str) -> str:
    return f"{{{namespace}}}{local_name}"


class MotionPhotoError(ValueError):
    """Base class for predictable media-format failures."""


class InvalidJpegError(MotionPhotoError):
    """The supplied bytes are not a complete, structurally valid JPEG."""


class XmpError(MotionPhotoError):
    """Base class for standard XMP failures."""


class XmpParseError(XmpError):
    """A standard XMP APP1 packet is malformed or unsafe to parse."""


class XmpConflictError(XmpError):
    """Existing XMP cannot be merged without making an unsafe assumption."""


class AlreadyMotionPhotoError(XmpConflictError):
    """The image already contains modern or legacy Motion Photo metadata."""


class XmpTooLargeError(XmpError):
    """The merged standard XMP packet cannot fit in one JPEG APP1 segment."""


class MotionPhotoVerificationError(MotionPhotoError):
    """A newly written file failed structural verification."""


@dataclass(frozen=True)
class JpegSegment:
    """One JPEG marker segment, with absolute half-open byte offsets."""

    marker: int
    offset: int
    end: int
    payload_offset: int
    payload_length: int
    image_index: int = 0

    @property
    def marker_name(self) -> str:
        if self.marker == 0xD8:
            return "SOI"
        if self.marker == 0xD9:
            return "EOI"
        if self.marker == 0xDA:
            return "SOS"
        if 0xE0 <= self.marker <= 0xEF:
            return f"APP{self.marker - 0xE0}"
        return f"0xFF{self.marker:02X}"


@dataclass(frozen=True)
class ContainerItem:
    """A parsed entry in ``Container:Directory``."""

    mime: str | None
    semantic: str | None
    length: int | None
    padding: int | None


@dataclass(frozen=True)
class XmpMetadata:
    """Motion Photo fields extracted from a standard XMP packet."""

    motion_photo: int | None = None
    motion_photo_version: int | None = None
    presentation_timestamp_us: int | None = None
    container_items: tuple[ContainerItem, ...] = ()
    micro_video: int | None = None
    micro_video_version: int | None = None
    micro_video_offset: int | None = None
    micro_video_presentation_timestamp_us: int | None = None
    has_container_directory: bool = False

    @property
    def has_motion_photo(self) -> bool:
        return self.motion_photo == 1

    @property
    def has_legacy_motion_photo(self) -> bool:
        return self.micro_video == 1

    @property
    def primary_item(self) -> ContainerItem | None:
        return next((item for item in self.container_items if item.semantic == "Primary"), None)

    @property
    def motion_photo_item(self) -> ContainerItem | None:
        return next((item for item in self.container_items if item.semantic == "MotionPhoto"), None)


class XmpReconciliationState(str, Enum):
    """Semantic state of an existing standard XMP packet.

    The names intentionally describe the input state, not a camera vendor or a
    particular XML spelling.  This keeps conversion policy independent from
    whether properties are represented as RDF attributes or child elements.
    """

    ABSENT = "ABSENT"
    ORDINARY = "ORDINARY"
    SCAFFOLD = "SCAFFOLD"
    ACTIVE = "ACTIVE"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class XmpReconciliationPlan:
    """Read-only plan used by both scanners and the XMP writer.

    ``can_convert`` is true for inputs that can be deterministically reconciled
    to one Android Motion Photo directory.  ACTIVE means the XMP is a complete
    candidate; file-level callers must additionally validate its EOF layout and
    MP4 suffix before exposing ``AlreadyMotionPhoto``.  CONFLICT always contains
    a human-readable reason.
    """

    state: XmpReconciliationState
    can_convert: bool
    reason: str
    container_items: tuple[ContainerItem, ...] = ()


@dataclass(frozen=True)
class JpegInfo:
    """Structural information about a JPEG or JPEG-based Motion Photo."""

    size: int
    eoi_end: int
    trailing_size: int
    first_sos_offset: int
    width: int
    height: int
    segments: tuple[JpegSegment, ...]
    standard_xmp_segments: tuple[JpegSegment, ...]
    xmp_packet: bytes | None
    xmp_metadata: XmpMetadata | None
    encoded_image_count: int
    path: Path | None = None

    @property
    def has_motion_photo(self) -> bool:
        return bool(self.xmp_metadata and self.xmp_metadata.has_motion_photo)

    @property
    def motion_photo(self) -> bool:
        """Compatibility alias used by higher-level scan records."""

        return self.has_motion_photo

    @property
    def padding(self) -> int:
        """Alias for raw bytes following the final recognized JPEG EOI."""

        return self.trailing_size


@dataclass(frozen=True)
class VerificationResult:
    """Non-throwing result returned by :func:`verify_motion_photo`."""

    valid: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    jpeg_info: JpegInfo | None
    video_offset: int | None = None
    video_length: int | None = None
    primary_padding: int | None = None
    presentation_timestamp_us: int | None = None
    video_sha256: str | None = None
    source_video_sha256: str | None = None
    source_video_matches: bool | None = None
    image_payload_sha256: str | None = None
    source_image_payload_sha256: str | None = None
    source_image_matches: bool | None = None

    @property
    def has_motion_photo(self) -> bool:
        return bool(self.jpeg_info and self.jpeg_info.has_motion_photo)

    @property
    def motion_photo(self) -> bool:
        return self.has_motion_photo

    @property
    def padding(self) -> int | None:
        return self.primary_padding

    @property
    def source_image_preserved(self) -> bool | None:
        return self.source_image_matches


@dataclass(frozen=True)
class MotionPhotoInfo:
    """Details of a successfully and atomically written Motion Photo."""

    output_path: Path
    source_image_size: int
    output_image_size: int
    video_length: int
    output_size: int
    video_offset: int
    jpeg_eoi_end: int
    primary_padding: int
    presentation_timestamp_us: int
    xmp_app1_size: int
    replaced_existing_xmp: bool
    verification: VerificationResult


@dataclass(frozen=True)
class _ParsedImage:
    start: int
    eoi_end: int
    first_sos_offset: int
    width: int
    height: int
    segments: tuple[JpegSegment, ...]


_SOF_MARKERS = frozenset(
    marker
    for marker in range(0xC0, 0xD0)
    if marker not in (0xC4, 0xC8, 0xCC)
)
_STANDALONE_MARKERS = frozenset((0x01, 0xD8, 0xD9, *range(0xD0, 0xD8)))


def _parse_one_jpeg(data: object, start: int, image_index: int, limit: int) -> _ParsedImage:
    if start < 0 or start + 2 > limit or data[start : start + 2] != SOI:  # type: ignore[index]
        raise InvalidJpegError(f"JPEG image {image_index} does not start with SOI")

    segments: list[JpegSegment] = [JpegSegment(0xD8, start, start + 2, start + 2, 0, image_index)]
    pos = start + 2
    first_sos = -1
    width = 0
    height = 0
    saw_sof = False
    saw_sos = False
    in_scan = False

    while pos < limit:
        if in_scan:
            # Entropy data can contain 0xFF00 stuffing and restart markers.  A
            # different marker terminates this scan; progressive JPEGs then
            # continue through more table/SOS segments before their true EOI.
            while True:
                marker_prefix = data.find(b"\xff", pos, limit)  # type: ignore[attr-defined]
                if marker_prefix < 0:
                    raise InvalidJpegError("JPEG scan has no terminating marker")
                code_index = marker_prefix + 1
                while code_index < limit and data[code_index] == 0xFF:  # type: ignore[index]
                    code_index += 1
                if code_index >= limit:
                    raise InvalidJpegError("truncated JPEG marker in scan data")
                code = data[code_index]  # type: ignore[index]
                if code == 0x00 or 0xD0 <= code <= 0xD7:
                    pos = code_index + 1
                    continue
                # Retain fill 0xFF bytes by considering only the final prefix
                # byte part of the actual marker segment.
                pos = code_index - 1
                in_scan = False
                break

        if pos >= limit or data[pos] != 0xFF:  # type: ignore[index]
            raise InvalidJpegError(f"expected JPEG marker at byte {pos}")
        while pos < limit and data[pos] == 0xFF:  # type: ignore[index]
            pos += 1
        if pos >= limit:
            raise InvalidJpegError("truncated JPEG marker")
        marker = data[pos]  # type: ignore[index]
        marker_offset = pos - 1
        code_index = pos
        pos += 1
        if marker == 0x00:
            raise InvalidJpegError(f"unexpected stuffed byte outside JPEG scan at {code_index}")

        if marker in _STANDALONE_MARKERS:
            segment = JpegSegment(marker, marker_offset, code_index + 1, code_index + 1, 0, image_index)
            segments.append(segment)
            if marker == 0xD9:
                if not saw_sof or not saw_sos:
                    raise InvalidJpegError("JPEG is missing an SOF or SOS segment")
                return _ParsedImage(
                    start=start,
                    eoi_end=code_index + 1,
                    first_sos_offset=first_sos,
                    width=width,
                    height=height,
                    segments=tuple(segments),
                )
            # Preserve any marker fill bytes by advancing from the code byte;
            continue

        if pos + 2 > limit:
            raise InvalidJpegError(f"truncated length for JPEG marker 0xFF{marker:02X}")
        segment_length = struct.unpack(">H", data[pos : pos + 2])[0]  # type: ignore[index]
        if segment_length < 2:
            raise InvalidJpegError(f"invalid JPEG segment length {segment_length} at byte {marker_offset}")
        payload_offset = pos + 2
        end = pos + segment_length
        if end > limit:
            raise InvalidJpegError(f"JPEG segment at byte {marker_offset} exceeds file bounds")
        segment = JpegSegment(
            marker=marker,
            offset=marker_offset,
            end=end,
            payload_offset=payload_offset,
            payload_length=end - payload_offset,
            image_index=image_index,
        )
        segments.append(segment)

        if marker in _SOF_MARKERS:
            if segment.payload_length < 6:
                raise InvalidJpegError("SOF segment is too short")
            parsed_height = struct.unpack(">H", data[payload_offset + 1 : payload_offset + 3])[0]  # type: ignore[index]
            parsed_width = struct.unpack(">H", data[payload_offset + 3 : payload_offset + 5])[0]  # type: ignore[index]
            if parsed_width <= 0 or parsed_height <= 0:
                raise InvalidJpegError("JPEG has invalid zero dimensions")
            if not saw_sof:
                width, height = parsed_width, parsed_height
            saw_sof = True

        pos = end
        if marker == 0xDA:
            if first_sos < 0:
                first_sos = marker_offset
            saw_sos = True
            in_scan = True

    raise InvalidJpegError("JPEG has no EOI marker")


def _parse_integer(value: str, property_name: str, *, allow_negative_one: bool = False) -> int:
    try:
        parsed = int(value.strip(), 10)
    except (TypeError, ValueError) as exc:
        raise XmpParseError(f"{property_name} is not an integer") from exc
    minimum = -1 if allow_negative_one else 0
    if parsed < minimum:
        raise XmpParseError(f"{property_name} must be at least {minimum}")
    return parsed


def _property_value(root: ET.Element, namespace: str, local_name: str) -> str | None:
    qualified = _qname(namespace, local_name)
    values: list[str] = []
    for element in root.iter():
        if qualified in element.attrib:
            values.append(element.attrib[qualified].strip())
        if element.tag == qualified:
            text = (element.text or "").strip()
            if not text:
                text = element.attrib.get(_qname(NS_RDF, "resource"), "").strip()
            values.append(text)
    if not values:
        return None
    if len(set(values)) != 1:
        raise XmpConflictError(f"conflicting values for {local_name}")
    return values[0]


def _top_level_descriptions(root: ET.Element) -> tuple[ET.Element, ...]:
    return tuple(
        child
        for child in _rdf_element(root)
        if child.tag == _qname(NS_RDF, "Description")
    )


def _top_level_property_value(
    root: ET.Element,
    namespace: str,
    local_name: str,
) -> str | None:
    """Read one property only from direct top-level RDF descriptions.

    Motion Photo properties nested inside another structured value are not the
    same RDF property.  Treat such spellings as a conflict instead of silently
    editing the wrong node or creating a second effective value.
    """

    qualified = _qname(namespace, local_name)
    descriptions = _top_level_descriptions(root)
    allowed_elements = {
        id(child)
        for description in descriptions
        for child in description
        if child.tag == qualified
    }
    for element in root.iter():
        if qualified in element.attrib and all(element is not item for item in descriptions):
            raise XmpConflictError(f"{local_name} is outside a top-level rdf:Description")
        if element.tag == qualified and id(element) not in allowed_elements:
            raise XmpConflictError(f"{local_name} is outside a top-level rdf:Description")

    values: list[str] = []
    for description in descriptions:
        if qualified in description.attrib:
            values.append(description.attrib[qualified].strip())
        for child in description:
            if child.tag == qualified:
                text = (child.text or "").strip()
                if not text:
                    text = child.attrib.get(_qname(NS_RDF, "resource"), "").strip()
                values.append(text)
    if not values:
        return None
    if len(set(values)) != 1:
        raise XmpConflictError(f"conflicting values for {local_name}")
    return values[0]


def _item_property(element: ET.Element, local_name: str) -> str | None:
    qualified = _qname(NS_ITEM, local_name)
    values: list[str] = []
    if qualified in element.attrib:
        values.append(element.attrib[qualified].strip())
    for child in element:
        if child.tag == qualified:
            values.append((child.text or "").strip())
    if not values:
        return None
    if len(set(values)) != 1:
        raise XmpConflictError(f"conflicting Item:{local_name} values")
    return values[0]


def _parse_container_items(root: ET.Element) -> tuple[bool, tuple[ContainerItem, ...]]:
    descriptions = _top_level_descriptions(root)
    directories = [
        child
        for description in descriptions
        for child in description
        if child.tag == _qname(NS_CONTAINER, "Directory")
    ]
    all_directories = list(root.iter(_qname(NS_CONTAINER, "Directory")))
    if len(all_directories) != len(directories):
        raise XmpConflictError(
            "Container:Directory must be a direct property of a top-level rdf:Description"
        )
    if not directories:
        return False, ()
    if len(directories) != 1:
        raise XmpConflictError("multiple Container:Directory properties are present")
    directory = directories[0]
    sequences = [child for child in directory if child.tag == _qname(NS_RDF, "Seq")]
    if len(sequences) != 1 or len(directory) != 1:
        raise XmpParseError("Container:Directory must contain exactly one rdf:Seq")

    items: list[ContainerItem] = []
    for list_item in sequences[0]:
        if list_item.tag != _qname(NS_RDF, "li"):
            raise XmpParseError("Container:Directory rdf:Seq contains a non-rdf:li element")
        nested = [child for child in list_item if child.tag == _qname(NS_CONTAINER, "Item")]
        direct_item_properties = any(
            key.startswith(f"{{{NS_ITEM}}}") for key in list_item.attrib
        ) or any(
            isinstance(child.tag, str) and child.tag.startswith(f"{{{NS_ITEM}}}")
            for child in list_item
        )
        if len(nested) != 1:
            raise XmpConflictError(
                "each Container directory entry must have exactly one nested Container:Item"
            )
        if direct_item_properties:
            raise XmpConflictError(
                "mixed direct Item properties and nested Container:Item are not allowed"
            )
        item_element = nested[0]

        mime = _item_property(item_element, "Mime")
        semantic = _item_property(item_element, "Semantic")
        length_text = _item_property(item_element, "Length")
        padding_text = _item_property(item_element, "Padding")
        length = _parse_integer(length_text, "Item:Length") if length_text is not None else None
        padding = _parse_integer(padding_text, "Item:Padding") if padding_text is not None else None
        items.append(ContainerItem(mime=mime, semantic=semantic, length=length, padding=padding))
    return True, tuple(items)


def _parse_xmp_tree(packet: bytes) -> ET.Element:
    if packet.startswith(XMP_HEADER):
        packet = packet[len(XMP_HEADER) :]
    lowered = packet.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise XmpParseError("DTD/entity declarations are not allowed in XMP")
    try:
        root = ET.fromstring(packet)
    except (ET.ParseError, ValueError) as exc:
        raise XmpParseError(f"malformed standard XMP packet: {exc}") from exc

    rdf_name = _qname(NS_RDF, "RDF")
    if root.tag == rdf_name:
        return root
    if root.tag != _qname(NS_X, "xmpmeta"):
        raise XmpParseError("standard XMP root must be x:xmpmeta or rdf:RDF")
    rdf_nodes = [child for child in root if child.tag == rdf_name]
    if len(rdf_nodes) != 1:
        raise XmpParseError("x:xmpmeta must contain exactly one rdf:RDF element")
    return root


def _rdf_element(root: ET.Element) -> ET.Element:
    if root.tag == _qname(NS_RDF, "RDF"):
        return root
    rdf_nodes = [child for child in root if child.tag == _qname(NS_RDF, "RDF")]
    if len(rdf_nodes) != 1:
        raise XmpParseError("XMP packet does not contain exactly one rdf:RDF element")
    return rdf_nodes[0]


def parse_xmp_packet(packet: bytes) -> XmpMetadata:
    """Parse the Motion Photo-related fields in one standard XMP packet.

    ``packet`` may include the Adobe standard-XMP identifier or may start with
    the XML packet itself.  Unknown XMP properties are ignored by this reader
    (and retained by :func:`build_motion_photo_xmp` during a merge).
    """

    root = _parse_xmp_tree(bytes(packet))

    def camera_integer(name: str, *, allow_negative_one: bool = False) -> int | None:
        value = _top_level_property_value(root, NS_CAMERA, name)
        if value is None:
            return None
        return _parse_integer(value, f"Camera:{name}", allow_negative_one=allow_negative_one)

    has_directory, items = _parse_container_items(root)
    return XmpMetadata(
        motion_photo=camera_integer("MotionPhoto"),
        motion_photo_version=camera_integer("MotionPhotoVersion"),
        presentation_timestamp_us=camera_integer(
            "MotionPhotoPresentationTimestampUs", allow_negative_one=True
        ),
        container_items=items,
        micro_video=camera_integer("MicroVideo"),
        micro_video_version=camera_integer("MicroVideoVersion"),
        micro_video_offset=camera_integer("MicroVideoOffset"),
        micro_video_presentation_timestamp_us=camera_integer(
            "MicroVideoPresentationTimestampUs", allow_negative_one=True
        ),
        has_container_directory=has_directory,
    )


def _inspect_buffer(data: object, *, path: Path | None = None) -> JpegInfo:
    size = len(data)  # type: ignore[arg-type]
    if size < 4:
        raise InvalidJpegError("file is too short to be a JPEG")
    primary = _parse_one_jpeg(data, 0, 0, size)

    primary_xmp_segments = tuple(
        segment
        for segment in primary.segments
        if segment.marker == 0xE1
        and data[segment.payload_offset : segment.payload_offset + len(XMP_HEADER)] == XMP_HEADER  # type: ignore[index]
    )
    if len(primary_xmp_segments) > 1:
        raise XmpConflictError("multiple standard XMP APP1 segments are present")

    xmp_packet: bytes | None = None
    xmp_metadata: XmpMetadata | None = None
    if primary_xmp_segments:
        segment = primary_xmp_segments[0]
        payload = bytes(data[segment.payload_offset : segment.end])  # type: ignore[index]
        xmp_packet = payload[len(XMP_HEADER) :]
        xmp_metadata = parse_xmp_packet(xmp_packet)

    # If this is already a Motion Photo, do not mistake an SOI-looking byte
    # sequence in its MP4 for a concatenated JPEG.  Otherwise search after each
    # valid top-level EOI for another complete JPEG (vivo/MPF gainmap images use
    # this layout).  Embedded JPEG thumbnails are skipped naturally because
    # their SOI occurs inside a length-delimited segment of the current image.
    chain_limit = size
    if xmp_metadata:
        motion_item = xmp_metadata.motion_photo_item
        if (
            xmp_metadata.has_motion_photo
            and motion_item is not None
            and motion_item.length is not None
            and 0 < motion_item.length <= size
        ):
            chain_limit = size - motion_item.length

    parsed_images = [primary]
    search_offset = primary.eoi_end
    while search_offset + 2 <= chain_limit:
        candidate = data.find(SOI, search_offset, chain_limit)  # type: ignore[attr-defined]
        if candidate < 0:
            break
        try:
            parsed = _parse_one_jpeg(data, candidate, len(parsed_images), chain_limit)
        except InvalidJpegError:
            search_offset = candidate + 2
            continue
        parsed_images.append(parsed)
        search_offset = parsed.eoi_end

    final_eoi = parsed_images[-1].eoi_end
    all_segments = tuple(segment for parsed in parsed_images for segment in parsed.segments)
    return JpegInfo(
        size=size,
        eoi_end=final_eoi,
        trailing_size=size - final_eoi,
        first_sos_offset=primary.first_sos_offset,
        width=primary.width,
        height=primary.height,
        segments=all_segments,
        standard_xmp_segments=primary_xmp_segments,
        xmp_packet=xmp_packet,
        xmp_metadata=xmp_metadata,
        encoded_image_count=len(parsed_images),
        path=path,
    )


def inspect_jpeg(path_or_bytes: PathOrBytes) -> JpegInfo:
    """Inspect JPEG markers, standard XMP, final EOI, and trailing bytes.

    Paths are memory-mapped read-only so even a completed Motion Photo is not
    copied into RAM.  Byte-like inputs are useful for tests and callers that
    already have the image in memory.
    """

    if isinstance(path_or_bytes, (bytes, bytearray, memoryview)):
        data = bytes(path_or_bytes)
        return _inspect_buffer(data)

    path = pathio.public_path(path_or_bytes)
    with pathio.open_path(path, "rb") as source:
        size = os.fstat(source.fileno()).st_size
        if size == 0:
            raise InvalidJpegError("file is empty")
        with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            return _inspect_buffer(mapped, path=path)


def _validate_motion_values(video_length: int, padding: int, presentation_timestamp_us: int) -> None:
    for value, name in (
        (video_length, "video_length"),
        (padding, "padding"),
        (presentation_timestamp_us, "presentation_timestamp_us"),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an integer")
    if video_length <= 0:
        raise ValueError("video_length must be positive")
    if padding < 0:
        raise ValueError("padding must not be negative")
    if not -1 <= presentation_timestamp_us <= (1 << 63) - 1:
        raise ValueError("presentation_timestamp_us must be -1 or a non-negative signed 64-bit value")


def _directory_structure(
    root: ET.Element,
) -> tuple[ET.Element, ET.Element, ET.Element, tuple[tuple[ET.Element, ET.Element], ...]]:
    """Return the one directory's RDF context and concrete entry nodes.

    Only the official nested ``Container:Item`` spelling is accepted.  Keeping
    the writable subset deliberately small prevents an ambiguous RDF spelling
    from being normalized into a different meaning.
    """

    descriptions = _top_level_descriptions(root)
    directories = [
        child
        for description in descriptions
        for child in description
        if child.tag == _qname(NS_CONTAINER, "Directory")
    ]
    if len(directories) != 1:
        raise XmpConflictError("exactly one Container:Directory is required for reconciliation")
    directory = directories[0]
    sequences = [child for child in directory if child.tag == _qname(NS_RDF, "Seq")]
    if len(sequences) != 1 or len(directory) != 1:
        raise XmpParseError("Container:Directory must contain exactly one rdf:Seq")
    sequence = sequences[0]

    owning_descriptions = [
        description
        for description in descriptions
        if any(child is directory for child in description)
    ]
    if len(owning_descriptions) != 1:
        raise XmpConflictError(
            "Container:Directory must be a direct property of one top-level rdf:Description"
        )
    owner = owning_descriptions[0]
    if owner.attrib.get(_qname(NS_RDF, "about"), "") != "":
        raise XmpConflictError("Container:Directory rdf:Description must have an empty rdf:about")

    entries: list[tuple[ET.Element, ET.Element]] = []
    for list_item in sequence:
        if list_item.tag != _qname(NS_RDF, "li"):
            raise XmpParseError("Container:Directory rdf:Seq contains a non-rdf:li element")
        nested = [child for child in list_item if child.tag == _qname(NS_CONTAINER, "Item")]
        direct_item_properties = any(
            key.startswith(f"{{{NS_ITEM}}}") for key in list_item.attrib
        ) or any(
            isinstance(child.tag, str) and child.tag.startswith(f"{{{NS_ITEM}}}")
            for child in list_item
        )
        if len(nested) != 1:
            raise XmpConflictError(
                "each Container directory entry must have exactly one nested Container:Item"
            )
        if direct_item_properties:
            raise XmpConflictError(
                "mixed direct Item properties and nested Container:Item are not allowed"
            )
        item_element = nested[0]
        entries.append((list_item, item_element))
    return owner, directory, sequence, tuple(entries)


def plan_xmp_reconciliation(existing_xmp: bytes | None) -> XmpReconciliationPlan:
    """Classify an XMP packet without changing it.

    The conversion target owns only the Motion Photo camera properties, the
    Primary item's layout fields, and the final MotionPhoto item.  Ordinary XMP
    stays untouched.  The currently writable scaffold is deliberately limited
    to one official nested Primary item; auxiliary resources fail closed until
    their physical byte layout can be verified end-to-end.
    """

    if existing_xmp is None:
        return XmpReconciliationPlan(
            XmpReconciliationState.ABSENT,
            True,
            "no standard XMP packet; create Motion Photo metadata",
        )

    try:
        root = _parse_xmp_tree(bytes(existing_xmp))
        metadata = parse_xmp_packet(existing_xmp)
        if _top_level_property_value(root, NS_XMP_NOTE, "HasExtendedXMP") is not None:
            raise XmpConflictError(
                "standard XMP references Extended XMP, which cannot yet be reconciled safely"
            )

        has_motion_item = any(item.semantic == "MotionPhoto" for item in metadata.container_items)
        modern_values = (
            metadata.motion_photo,
            metadata.motion_photo_version,
            metadata.presentation_timestamp_us,
        )
        legacy_values = (
            metadata.micro_video,
            metadata.micro_video_version,
            metadata.micro_video_offset,
            metadata.micro_video_presentation_timestamp_us,
        )
        modern_present = has_motion_item or any(value is not None for value in modern_values)
        legacy_present = any(value is not None for value in legacy_values)

        if modern_present:
            # ACTIVE means a complete, internally coherent target state, not
            # merely one flag whose value happens to be 1.  Partial markers are
            # conflicts because neither overwriting nor reporting them as a
            # valid already-converted file would be safe.
            if not (
                metadata.motion_photo == 1
                and metadata.motion_photo_version == 1
                and metadata.presentation_timestamp_us is not None
                and metadata.has_container_directory
            ):
                raise XmpConflictError("modern Motion Photo metadata is incomplete or inconsistent")
            _description, _directory, _sequence, _entries = _directory_structure(root)
            items = metadata.container_items
            if [item.semantic for item in items] != ["Primary", "MotionPhoto"]:
                raise XmpConflictError(
                    "active Container directory must contain exactly Primary and MotionPhoto"
                )
            primary = items[0]
            motion = items[-1]
            if (primary.mime or "").casefold() != "image/jpeg" or primary.length not in (None, 0):
                raise XmpConflictError("active Primary resource has an invalid Mime or Length")
            if primary.padding == 0:
                raise XmpConflictError("zero Primary Item:Padding must be omitted")
            if (
                (motion.mime or "").casefold() != "video/mp4"
                or motion.length is None
                or motion.length <= 0
                or motion.padding is not None
            ):
                raise XmpConflictError(
                    "active MotionPhoto resource has an invalid Mime, Length, or Padding"
                )
            return XmpReconciliationPlan(
                XmpReconciliationState.ACTIVE,
                False,
                "image already contains complete Motion Photo metadata",
                items,
            )

        if legacy_present:
            raise XmpConflictError(
                "legacy MicroVideo metadata is not rewritten or classified as an active Motion Photo"
            )

        if not metadata.has_container_directory:
            return XmpReconciliationPlan(
                XmpReconciliationState.ORDINARY,
                True,
                "ordinary XMP can be preserved while Motion Photo metadata is added",
            )

        # Re-locate the parsed resources in the XML tree.  Besides validating
        # their context, this guarantees the writer can modify the exact Primary
        # entry instead of creating a second directory.
        _description, _directory, _sequence, entries = _directory_structure(root)
        items = metadata.container_items
        if len(entries) != len(items):
            raise XmpConflictError("Container directory cannot be mapped to its parsed resources")
        if len(items) != 1 or items[0].semantic != "Primary":
            raise XmpConflictError(
                "convertible Container scaffold must contain exactly one Primary item"
            )
        primary = items[0]
        if (primary.mime or "").casefold() != "image/jpeg":
            raise XmpConflictError("Primary Item:Mime must be image/jpeg")

        return XmpReconciliationPlan(
            XmpReconciliationState.SCAFFOLD,
            True,
            "compatible Container scaffold can be normalized and extended",
            items,
        )
    except XmpError as exc:
        return XmpReconciliationPlan(
            XmpReconciliationState.CONFLICT,
            False,
            str(exc),
        )


def _require_convertible_xmp(plan: XmpReconciliationPlan) -> None:
    if plan.state is XmpReconciliationState.ACTIVE:
        raise AlreadyMotionPhotoError(plan.reason)
    if not plan.can_convert:
        raise XmpConflictError(plan.reason)


def _active_motion_photo_layout(
    info: JpegInfo,
    source: PathOrBytes | None = None,
) -> tuple[int, int, int]:
    """Validate a Primary-only active file and return its physical layout.

    Complete-looking XMP is only an ACTIVE Motion Photo after the declared MP4
    maps exactly to EOF, Padding maps exactly from the final encoded JPEG EOI,
    and the suffix begins with a structurally valid MP4 ``ftyp`` box.
    """

    plan = plan_xmp_reconciliation(info.xmp_packet)
    if plan.state is not XmpReconciliationState.ACTIVE:
        raise XmpConflictError(plan.reason)
    metadata = info.xmp_metadata
    if metadata is None or [item.semantic for item in metadata.container_items] != [
        "Primary",
        "MotionPhoto",
    ]:
        raise XmpConflictError(
            "active Container directory must contain exactly Primary and MotionPhoto"
        )
    primary, motion = metadata.container_items
    if motion.length is None or motion.length <= 0:
        raise XmpConflictError("MotionPhoto Item:Length must be positive")
    video_length = motion.length
    video_offset = info.size - video_length
    if video_offset < info.eoi_end:
        raise XmpConflictError("MotionPhoto Item:Length overlaps the encoded JPEG")
    padding = primary.padding or 0
    actual_padding = video_offset - info.eoi_end
    if actual_padding != padding:
        raise XmpConflictError(
            f"Primary Item:Padding is {padding}, but the file contains {actual_padding} bytes"
        )

    physical_source: PathOrBytes | None = source if source is not None else info.path
    if physical_source is None:
        raise XmpConflictError("source bytes are required to validate the appended MP4")
    if not _looks_like_mp4_source(physical_source, video_offset, video_length):
        raise XmpConflictError("appended MotionPhoto item does not start with a valid MP4 ftyp box")
    return video_offset, video_length, padding


def _set_item_property(element: ET.Element, local_name: str, value: str) -> None:
    """Canonicalize one managed Item property without touching other fields."""

    qualified = _qname(NS_ITEM, local_name)
    element.attrib.pop(qualified, None)
    for child in list(element):
        if child.tag == qualified:
            element.remove(child)
    element.set(qualified, value)


def _remove_item_property(element: ET.Element, local_name: str) -> None:
    qualified = _qname(NS_ITEM, local_name)
    element.attrib.pop(qualified, None)
    for child in list(element):
        if child.tag == qualified:
            element.remove(child)


def _camera_description(rdf: ET.Element, preferred: ET.Element | None = None) -> ET.Element:
    if preferred is not None:
        return preferred
    for child in rdf:
        if (
            child.tag == _qname(NS_RDF, "Description")
            and child.attrib.get(_qname(NS_RDF, "about"), "") == ""
        ):
            return child
    return ET.SubElement(rdf, _qname(NS_RDF, "Description"), {_qname(NS_RDF, "about"): ""})


def build_motion_photo_xmp(
    existing_xmp: bytes | None,
    video_length: int,
    padding: int,
    presentation_timestamp_us: int,
) -> bytes:
    """Create a merged XMP XML packet (without the Adobe APP1 identifier)."""

    _validate_motion_values(video_length, padding, presentation_timestamp_us)
    plan = plan_xmp_reconciliation(existing_xmp)
    _require_convertible_xmp(plan)

    if existing_xmp is None:
        root = ET.Element(_qname(NS_X, "xmpmeta"), {_qname(NS_X, "xmptk"): "vivo-motion-photo"})
        rdf = ET.SubElement(root, _qname(NS_RDF, "RDF"))
    else:
        root = _parse_xmp_tree(bytes(existing_xmp))
        rdf = _rdf_element(root)

    preferred_description: ET.Element | None = None
    if plan.state is XmpReconciliationState.SCAFFOLD:
        preferred_description, _directory, sequence, entries = _directory_structure(root)
        _primary_li, primary_item = entries[0]
        _set_item_property(primary_item, "Mime", "image/jpeg")
        _set_item_property(primary_item, "Semantic", "Primary")
        _set_item_property(primary_item, "Length", "0")
        if padding:
            _set_item_property(primary_item, "Padding", str(padding))
        else:
            _remove_item_property(primary_item, "Padding")
    else:
        description = _camera_description(rdf)
        directory = ET.SubElement(description, _qname(NS_CONTAINER, "Directory"))
        sequence = ET.SubElement(directory, _qname(NS_RDF, "Seq"))
        primary_li = ET.SubElement(
            sequence,
            _qname(NS_RDF, "li"),
            {_qname(NS_RDF, "parseType"): "Resource"},
        )
        primary_attributes = {
            _qname(NS_ITEM, "Mime"): "image/jpeg",
            _qname(NS_ITEM, "Semantic"): "Primary",
            _qname(NS_ITEM, "Length"): "0",
        }
        if padding:
            primary_attributes[_qname(NS_ITEM, "Padding")] = str(padding)
        ET.SubElement(
            primary_li,
            _qname(NS_CONTAINER, "Item"),
            primary_attributes,
        )

    description = _camera_description(rdf, preferred_description)
    description.set(_qname(NS_CAMERA, "MotionPhoto"), "1")
    description.set(_qname(NS_CAMERA, "MotionPhotoVersion"), "1")
    description.set(
        _qname(NS_CAMERA, "MotionPhotoPresentationTimestampUs"),
        str(presentation_timestamp_us),
    )

    video_li = ET.SubElement(sequence, _qname(NS_RDF, "li"), {_qname(NS_RDF, "parseType"): "Resource"})
    ET.SubElement(
        video_li,
        _qname(NS_CONTAINER, "Item"),
        {
            _qname(NS_ITEM, "Mime"): "video/mp4",
            _qname(NS_ITEM, "Semantic"): "MotionPhoto",
            _qname(NS_ITEM, "Length"): str(video_length),
        },
    )

    serialized = ET.tostring(root, encoding="utf-8", xml_declaration=False, short_empty_elements=True)
    return (
        b'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        + serialized
        + b'\n<?xpacket end="w"?>'
    )


def _xmp_app1_segment(packet: bytes) -> bytes:
    payload = XMP_HEADER + packet
    if len(payload) > MAX_APP1_PAYLOAD:
        raise XmpTooLargeError(
            f"merged XMP payload is {len(payload)} bytes; APP1 maximum is {MAX_APP1_PAYLOAD}"
        )
    return b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload


def _validate_convertible(info: JpegInfo, source: PathOrBytes | None = None) -> None:
    plan = plan_xmp_reconciliation(info.xmp_packet)
    if plan.state is XmpReconciliationState.ACTIVE:
        # Never return AlreadyMotionPhoto from metadata alone.  A corrupt or
        # stale active packet is a conflict, while a physically valid suffix is
        # the idempotent already-converted case.
        _active_motion_photo_layout(info, source)
        raise AlreadyMotionPhotoError(plan.reason)
    _require_convertible_xmp(plan)
    if info.standard_xmp_segments and info.standard_xmp_segments[0].offset >= info.first_sos_offset:
        raise XmpConflictError("standard XMP appears after the first SOS and cannot be safely moved")


def _make_transform(
    info: JpegInfo,
    video_length: int,
    presentation_timestamp_us: int,
    source: PathOrBytes | None = None,
) -> tuple[bytes, int, int]:
    _validate_motion_values(video_length, info.trailing_size, presentation_timestamp_us)
    _validate_convertible(info, source)
    packet = build_motion_photo_xmp(
        info.xmp_packet,
        video_length=video_length,
        padding=info.trailing_size,
        presentation_timestamp_us=presentation_timestamp_us,
    )
    marker = _xmp_app1_segment(packet)
    if info.standard_xmp_segments:
        old = info.standard_xmp_segments[0]
        return marker, old.offset, old.end
    return marker, info.first_sos_offset, info.first_sos_offset


def inject_motion_photo_xmp(
    jpeg_bytes: bytes,
    video_length: int,
    presentation_timestamp_us: int = -1,
) -> bytes:
    """Return the image portion with merged Motion Photo XMP, but no video."""

    source = bytes(jpeg_bytes)
    info = inspect_jpeg(source)
    marker, remove_start, remove_end = _make_transform(
        info,
        video_length,
        presentation_timestamp_us,
        source,
    )
    # Existing XMP is moved to immediately before the first SOS after semantic
    # merging.  All other bytes remain in their original order.
    if remove_start == info.first_sos_offset:
        return source[: info.first_sos_offset] + marker + source[info.first_sos_offset :]
    return (
        source[:remove_start]
        + source[remove_end : info.first_sos_offset]
        + marker
        + source[info.first_sos_offset :]
    )


def build_motion_photo(
    jpeg_bytes: bytes,
    video_bytes: bytes,
    presentation_timestamp_us: int = -1,
) -> bytes:
    """Build an in-memory JPEG Motion Photo; file workflows should use the writer."""

    video = bytes(video_bytes)
    return inject_motion_photo_xmp(jpeg_bytes, len(video), presentation_timestamp_us) + video


def _copy_exact(source: BinaryIO, destination: BinaryIO, count: int) -> None:
    remaining = count
    while remaining:
        chunk = source.read(min(COPY_CHUNK_SIZE, remaining))
        if not chunk:
            raise OSError("source file ended while it was being copied")
        destination.write(chunk)
        remaining -= len(chunk)


def _copy_image_with_xmp(
    source: BinaryIO,
    destination: BinaryIO,
    info: JpegInfo,
    marker: bytes,
    remove_start: int,
    remove_end: int,
) -> None:
    if remove_start == info.first_sos_offset:
        _copy_exact(source, destination, info.first_sos_offset)
    else:
        _copy_exact(source, destination, remove_start)
        source.seek(remove_end)
        _copy_exact(source, destination, info.first_sos_offset - remove_end)
    destination.write(marker)
    source.seek(info.first_sos_offset)
    _copy_exact(source, destination, info.size - info.first_sos_offset)


def _same_path(first: Path, second: Path) -> bool:
    try:
        if pathio.exists(first) and pathio.exists(second):
            return pathio.samefile(first, second)
    except OSError:
        pass
    return pathio.path_key(first) == pathio.path_key(second)


def _commit_temp(temp_path: Path, output_path: Path, overwrite: bool) -> None:
    if overwrite:
        pathio.replace(temp_path, output_path)
        return
    if pathio.exists(output_path):
        raise FileExistsError(output_path)
    try:
        # A hard-link commit is atomic and, unlike POSIX rename(), cannot
        # overwrite a target that appears between the existence check and commit.
        pathio.link(temp_path, output_path)
    except FileExistsError:
        raise
    except OSError:
        if pathio.exists(output_path):
            raise FileExistsError(output_path)
        pathio.rename(temp_path, output_path)
    else:
        pathio.unlink(temp_path)


def write_motion_photo(
    image_path: str | os.PathLike[str],
    video_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    presentation_timestamp_us: int,
    overwrite: bool = False,
) -> MotionPhotoInfo:
    """Stream and atomically write one standards-based JPEG Motion Photo.

    The source files are opened read-only.  The completed temporary file is
    structurally verified against both sources before it is committed in the
    destination directory.
    """

    image = pathio.public_path(image_path)
    video = pathio.public_path(video_path)
    output = pathio.public_path(output_path)
    if not pathio.is_file(image):
        raise FileNotFoundError(image)
    if not pathio.is_file(video):
        raise FileNotFoundError(video)
    if _same_path(image, video):
        raise ValueError("image and video paths refer to the same file")
    if _same_path(output, image) or _same_path(output, video):
        raise ValueError("output path must not overwrite either source file")
    if pathio.exists(output) and not overwrite:
        raise FileExistsError(output)

    # Capture the source timestamps before inspect/copy opens the JPEG.  Some
    # filesystems update atime on reads, so a later stat would no longer
    # represent the source metadata as it existed when conversion began.
    image_stat = pathio.stat(image)
    source_times_ns = (image_stat.st_atime_ns, image_stat.st_mtime_ns)
    info = inspect_jpeg(image)
    video_length = pathio.stat(video).st_size
    marker, remove_start, remove_end = _make_transform(info, video_length, presentation_timestamp_us)
    removed_size = remove_end - remove_start
    output_image_size = info.size - removed_size + len(marker)

    pathio.mkdir(output.parent, parents=True, exist_ok=True)
    descriptor, temp_path = pathio.mkstemp(
        prefix=".vivo-motion-", suffix=".tmp", directory=output.parent
    )
    verification: VerificationResult | None = None
    try:
        with os.fdopen(descriptor, "wb") as destination, pathio.open_path(image, "rb") as image_source:
            _copy_image_with_xmp(
                image_source,
                destination,
                info,
                marker,
                remove_start,
                remove_end,
            )
            with pathio.open_path(video, "rb") as video_source:
                while True:
                    chunk = video_source.read(COPY_CHUNK_SIZE)
                    if not chunk:
                        break
                    destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())

        verification = verify_motion_photo(temp_path, source_image=image, source_video=video)
        if not verification.valid:
            raise MotionPhotoVerificationError("; ".join(verification.errors))
        # Apply timestamps only after the temporary file has passed complete
        # verification, but before its atomic commit.  Every commit strategy
        # (hard link, rename, or replace) therefore publishes the same source
        # JPG atime/mtime without ever changing either source file.
        pathio.utime(temp_path, ns=source_times_ns)
        _commit_temp(temp_path, output, overwrite)
    except BaseException:
        try:
            pathio.unlink(temp_path, missing_ok=True)
        except OSError:
            pass
        raise

    assert verification is not None
    return MotionPhotoInfo(
        output_path=output,
        source_image_size=info.size,
        output_image_size=output_image_size,
        video_length=video_length,
        output_size=output_image_size + video_length,
        video_offset=output_image_size,
        jpeg_eoi_end=info.eoi_end - removed_size + len(marker),
        primary_padding=info.trailing_size,
        presentation_timestamp_us=presentation_timestamp_us,
        xmp_app1_size=len(marker),
        replaced_existing_xmp=bool(info.standard_xmp_segments),
        verification=verification,
    )


def _hash_region(path: Path, offset: int = 0, length: int | None = None) -> str:
    digest = hashlib.sha256()
    with pathio.open_path(path, "rb") as source:
        source.seek(offset)
        remaining = length
        while remaining is None or remaining > 0:
            request = COPY_CHUNK_SIZE if remaining is None else min(COPY_CHUNK_SIZE, remaining)
            chunk = source.read(request)
            if not chunk:
                break
            digest.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
        if remaining not in (None, 0):
            raise OSError("file ended before the requested hash region")
    return digest.hexdigest()


def _hash_without_standard_xmp(path: Path, info: JpegInfo, limit: int) -> str:
    digest = hashlib.sha256()
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for segment in info.standard_xmp_segments:
        if segment.offset >= limit:
            break
        ranges.append((cursor, min(segment.offset, limit)))
        cursor = min(segment.end, limit)
    ranges.append((cursor, limit))
    with pathio.open_path(path, "rb") as source:
        for start, end in ranges:
            if end <= start:
                continue
            source.seek(start)
            remaining = end - start
            while remaining:
                chunk = source.read(min(COPY_CHUNK_SIZE, remaining))
                if not chunk:
                    raise OSError("file ended while hashing its JPEG payload")
                digest.update(chunk)
                remaining -= len(chunk)
    return digest.hexdigest()


def _looks_like_mp4(path: Path, offset: int, length: int) -> bool:
    if length < 8:
        return False
    with pathio.open_path(path, "rb") as source:
        source.seek(offset)
        header = source.read(16)
    if len(header) < 8 or header[4:8] != b"ftyp":
        return False
    box_size = struct.unpack(">I", header[:4])[0]
    if box_size == 1:
        if len(header) < 16:
            return False
        box_size = struct.unpack(">Q", header[8:16])[0]
    elif box_size == 0:
        box_size = length
    return 8 <= box_size <= length


def _looks_like_mp4_source(source: PathOrBytes, offset: int, length: int) -> bool:
    if length < 8 or offset < 0:
        return False
    if isinstance(source, (bytes, bytearray, memoryview)):
        data = source
        if offset + min(length, 16) > len(data):
            return False
        header = bytes(data[offset : offset + min(length, 16)])
        if len(header) < 8 or header[4:8] != b"ftyp":
            return False
        box_size = struct.unpack(">I", header[:4])[0]
        if box_size == 1:
            if len(header) < 16:
                return False
            box_size = struct.unpack(">Q", header[8:16])[0]
        elif box_size == 0:
            box_size = length
        return 8 <= box_size <= length
    return _looks_like_mp4(pathio.public_path(source), offset, length)


def verify_motion_photo(
    path: str | os.PathLike[str],
    source_image: str | os.PathLike[str] | None = None,
    source_video: str | os.PathLike[str] | None = None,
) -> VerificationResult:
    """Verify XMP, layout, MP4 suffix, and optional byte identity to sources.

    Format failures are returned in ``errors`` instead of being raised, making
    this API convenient for both the GUI and ``verify`` CLI command.  Ordinary
    filesystem I/O errors encountered after opening the target are also reported.
    """

    target = pathio.public_path(path)
    errors: list[str] = []
    warnings: list[str] = []
    try:
        info = inspect_jpeg(target)
    except (OSError, MotionPhotoError, ValueError) as exc:
        return VerificationResult(False, (str(exc),), (), None)

    metadata = info.xmp_metadata
    video_offset: int | None = None
    video_length: int | None = None
    padding: int | None = None
    timestamp: int | None = None
    video_hash: str | None = None
    source_video_hash: str | None = None
    source_video_matches: bool | None = None
    image_hash: str | None = None
    source_image_hash: str | None = None
    source_image_matches: bool | None = None

    if metadata is None:
        errors.append("standard XMP metadata is missing")
    else:
        timestamp = metadata.presentation_timestamp_us
        if metadata.motion_photo != 1:
            errors.append("Camera:MotionPhoto must be 1")
        if metadata.motion_photo_version != 1:
            errors.append("Camera:MotionPhotoVersion must be 1")
        if timestamp is None or timestamp < -1:
            errors.append("Camera:MotionPhotoPresentationTimestampUs is missing or invalid")

        primary_items = [item for item in metadata.container_items if item.semantic == "Primary"]
        motion_items = [item for item in metadata.container_items if item.semantic == "MotionPhoto"]
        if [item.semantic for item in metadata.container_items] != ["Primary", "MotionPhoto"]:
            errors.append("Container directory must contain exactly Primary and MotionPhoto")
        if len(primary_items) != 1:
            errors.append("Container directory must contain exactly one Primary item")
        if len(motion_items) != 1:
            errors.append("Container directory must contain exactly one MotionPhoto item")
        if metadata.container_items:
            if metadata.container_items[0].semantic != "Primary":
                errors.append("Primary must be the first Container directory item")
            if metadata.container_items[-1].semantic != "MotionPhoto":
                errors.append("MotionPhoto must be the last Container directory item")

        if len(primary_items) == 1:
            primary = primary_items[0]
            padding = primary.padding or 0
            if primary.mime != "image/jpeg":
                errors.append("Primary Item:Mime must be image/jpeg")
            if primary.length not in (None, 0):
                errors.append("Primary Item:Length must be absent or 0")
            if primary.padding == 0:
                errors.append("zero Primary Item:Padding must be omitted")
        if len(motion_items) == 1:
            motion = motion_items[0]
            video_length = motion.length
            if motion.mime != "video/mp4":
                errors.append("MotionPhoto Item:Mime must be video/mp4")
            if video_length is None or video_length <= 0:
                errors.append("MotionPhoto Item:Length must be positive")
            if motion.padding is not None:
                errors.append("MotionPhoto item must not have Item:Padding")

    if metadata is not None and metadata.motion_photo == 1:
        try:
            video_offset, video_length, padding = _active_motion_photo_layout(info, target)
        except XmpError as exc:
            errors.append(str(exc))
        else:
            try:
                video_hash = _hash_region(target, video_offset, video_length)
            except OSError as exc:
                errors.append(str(exc))

    if source_video is not None and video_offset is not None and video_length is not None:
        source_video_path = pathio.public_path(source_video)
        try:
            source_video_hash = _hash_region(source_video_path)
            source_video_matches = (
                pathio.stat(source_video_path).st_size == video_length and source_video_hash == video_hash
            )
            if not source_video_matches:
                errors.append("appended video bytes do not match the source video")
        except OSError as exc:
            source_video_matches = False
            errors.append(f"could not compare source video: {exc}")

    if source_image is not None and video_offset is not None:
        source_image_path = pathio.public_path(source_image)
        try:
            source_info = inspect_jpeg(source_image_path)
            image_hash = _hash_without_standard_xmp(target, info, video_offset)
            source_image_hash = _hash_without_standard_xmp(source_image_path, source_info, source_info.size)
            source_image_matches = image_hash == source_image_hash
            if not source_image_matches:
                errors.append("non-XMP image bytes do not match the source image")
        except (OSError, MotionPhotoError, ValueError) as exc:
            source_image_matches = False
            errors.append(f"could not compare source image: {exc}")

    # The requested same-name behavior intentionally trades away the Android
    # filename recommendation.  Keep it informational, never a validation error.
    if not target.stem.upper().endswith("MP"):
        warnings.append("filename does not end in MP (allowed, but some Android readers may ignore it)")

    return VerificationResult(
        valid=not errors,
        errors=tuple(errors),
        warnings=tuple(warnings),
        jpeg_info=info,
        video_offset=video_offset,
        video_length=video_length,
        primary_padding=padding,
        presentation_timestamp_us=timestamp,
        video_sha256=video_hash,
        source_video_sha256=source_video_hash,
        source_video_matches=source_video_matches,
        image_payload_sha256=image_hash,
        source_image_payload_sha256=source_image_hash,
        source_image_matches=source_image_matches,
    )


def remove_standard_xmp(jpeg_bytes: bytes) -> bytes:
    """Return bytes with the primary image's standard XMP APP1 removed.

    This comparison/testing helper leaves extended XMP and every non-standard
    XMP segment untouched.
    """

    source = bytes(jpeg_bytes)
    info = inspect_jpeg(source)
    if not info.standard_xmp_segments:
        return source
    segment = info.standard_xmp_segments[0]
    return source[: segment.offset] + source[segment.end :]


__all__ = [
    "AlreadyMotionPhotoError",
    "ContainerItem",
    "InvalidJpegError",
    "JpegInfo",
    "JpegSegment",
    "MotionPhotoError",
    "MotionPhotoInfo",
    "MotionPhotoVerificationError",
    "VerificationResult",
    "XMP_HEADER",
    "XmpConflictError",
    "XmpError",
    "XmpMetadata",
    "XmpParseError",
    "XmpReconciliationPlan",
    "XmpReconciliationState",
    "XmpTooLargeError",
    "build_motion_photo",
    "build_motion_photo_xmp",
    "inject_motion_photo_xmp",
    "inspect_jpeg",
    "parse_xmp_packet",
    "plan_xmp_reconciliation",
    "remove_standard_xmp",
    "verify_motion_photo",
    "write_motion_photo",
]
