"""Emit the vivo X300 single-file flavour of a merged Google Motion Photo.

vivo's own live photos from X300 onwards are ordinary Google Motion Photo files
with three extra ``VCamera`` identity attributes in the same XMP packet and a
zero presentation timestamp.  Galleries are lenient, but some apps inspect the
vendor attributes before treating a file as "live", so ``--target vivo`` lets a
user reproduce that shape.

The packet is edited in place: every property the input already carries (rating,
keywords, copyright, edit history, colour metadata) survives, and only the
GCamera/Container attributes change.  Image bytes, EXIF and the appended clip are
never touched.

vivo declares ``Item:Padding="0"`` on the clip item, which Google's own spec does
not allow for non-primary items — the bundled verifier therefore rejects a
``--target vivo`` output by design.  Device galleries accept it; that is the
whole point of matching the vendor shape.

The shape (namespace, attribute names, item order) was observed on real X300
captures and is documented independently by live-photo-box's
``VivoLivePhotoProtocol``; no vendor code or binary is copied here.
"""

from __future__ import annotations

import re

VIVO_NS = "http://ns.vivo.com/photos/1.0/camera/"
VIVO_MEDIA_KIT = "1.0.0.9"
XMP_PREFIX = b"http://ns.adobe.com/xap/1.0/\x00"

_VIVO_ATTRS = (
    'VCamera:VMotionPhotoVersion="1"',
    'VCamera:VMotionPhotoSource="1"',
    f'VCamera:VMediaKitVersion="{VIVO_MEDIA_KIT}"',
)
_DESCRIPTION = re.compile(r"<rdf:Description\b[^>]*GCamera:MotionPhoto=\"[^\"]*\"[^>]*>", re.S)
_TIMESTAMP = re.compile(r'GCamera:MotionPhotoPresentationTimestampUs="[^"]*"')
_PRIMARY = re.compile(r"<Container:Item\b[^>]*Semantic=\"Primary\"[^>]*/>")
_MOTION = re.compile(r"<Container:Item\b[^>]*Semantic=\"MotionPhoto\"[^>]*/>")
_LENGTH = re.compile(r'\s+Item:Length="(\d+)"')
_PADDING = re.compile(r'\s+Item:Padding="(\d+)"')


def _locate_xmp(jpg: bytes) -> tuple[int, int, bytes]:
    """Byte span of the standard-XMP APP1 segment, plus its payload.

    Anchored on the ``http://ns.adobe.com/xap/1.0/`` marker so a stray
    ``<x:xmpmeta`` inside EXIF (MakerNote text, thumbnails) can never be mistaken
    for the packet we are about to edit.
    """
    marker = jpg.find(XMP_PREFIX)
    if marker < 0:
        raise ValueError("no standard XMP APP1 segment")
    start = marker - 4
    if jpg[start:start + 2] != b"\xff\xe1":
        raise ValueError("XMP marker is not the payload of an APP1 segment")
    seg_len = int.from_bytes(jpg[start + 2:start + 4], "big")
    end = start + 2 + seg_len
    if end > len(jpg):
        raise ValueError("APP1 segment runs past the end of the image")
    return start, end, jpg[marker + len(XMP_PREFIX):end]


def _items(payload: bytes) -> tuple[int, int]:
    """(clip length, primary padding) read from the packet's Container items."""
    xmp = payload.decode("utf-8", "replace")
    motion = _MOTION.search(xmp)
    if not motion:
        raise ValueError("no MotionPhoto item in the XMP packet")
    length = _LENGTH.search(motion.group(0))
    if not length:
        raise ValueError("MotionPhoto item has no Length attribute")
    primary = _PRIMARY.search(xmp)
    padding = _PADDING.search(primary.group(0)) if primary else None
    return int(length.group(1)), int(padding.group(1)) if padding else 0


def _add_vivo_attrs(xmp: str) -> str:
    match = _DESCRIPTION.search(xmp)
    if not match:
        raise ValueError("no rdf:Description carrying GCamera:MotionPhoto")
    tag = match.group(0)
    patched = tag if "xmlns:VCamera=" in tag else tag.replace(
        "<rdf:Description", f'<rdf:Description xmlns:VCamera="{VIVO_NS}"', 1)
    for attr in _VIVO_ATTRS:
        if attr.split("=")[0] not in patched:
            patched = patched[:-1] + " " + attr + ">"
    return xmp[:match.start()] + patched + xmp[match.end():]


def _fix_items(xmp: str) -> str:
    primary = _PRIMARY.search(xmp)
    if primary:
        item = _LENGTH.sub("", primary.group(0))
        padding = _PADDING.search(item)
        if padding and int(padding.group(1)) == 0:
            item = _PADDING.sub("", item)
        xmp = xmp[:primary.start()] + item + xmp[primary.end():]

    motion = _MOTION.search(xmp)
    if not motion:
        raise ValueError("no MotionPhoto item in the XMP packet")
    item = motion.group(0)
    if "Item:Padding=" not in item:
        # vivo declares zero padding on the clip item; the source may omit it.
        item = item[:-2].rstrip() + ' Item:Padding="0"/>'
    xmp = xmp[:motion.start()] + item + xmp[motion.end():]
    return xmp


_CAMERA_NS = re.compile(r'xmlns:(\w+)="http://ns\.google\.com/photos/1\.0/camera/"')


def _edit(payload: bytes) -> bytes:
    xmp = payload.decode("utf-8")
    bound = _CAMERA_NS.search(xmp)
    if not bound or "MotionPhoto=" not in xmp:
        raise ValueError("packet is not a Motion Photo packet")
    # The prefix is arbitrary XML, but vivo (and some apps that match on text
    # rather than namespaces) spell it GCamera — normalise before editing.
    if bound.group(1) != "GCamera":
        xmp = xmp.replace(f"xmlns:{bound.group(1)}=", "xmlns:GCamera=")
        xmp = xmp.replace(f"{bound.group(1)}:", "GCamera:")
    xmp = _add_vivo_attrs(xmp)
    if _TIMESTAMP.search(xmp):
        xmp = _TIMESTAMP.sub('GCamera:MotionPhotoPresentationTimestampUs="0"', xmp, count=1)
    else:
        xmp = xmp.replace("GCamera:MotionPhotoVersion=", 'GCamera:MotionPhotoPresentationTimestampUs="0" '
                          "GCamera:MotionPhotoVersion=", 1)
    xmp = _fix_items(xmp)
    return xmp.encode("utf-8")


def video_length(data: bytes) -> int:
    """Byte length of the appended clip, read from the standard XMP packet."""
    return _items(_locate_xmp(data)[2])[0]


def rewrite(data: bytes) -> bytes:
    """Return ``data`` with vivo-shaped XMP; pixels, EXIF and clip untouched."""
    start, end, payload = _locate_xmp(data)
    video_len, _ = _items(payload)
    if video_len <= 0 or video_len >= len(data):
        raise ValueError("MotionPhoto item length is out of range")
    image, video = data[:len(data) - video_len], data[-video_len:]
    if b"ftyp" not in video[:16]:
        raise ValueError("appended clip does not start with an MP4 box")
    if end > len(image):
        raise ValueError("XMP segment overlaps the appended clip")

    new_payload = XMP_PREFIX + _edit(payload)
    segment = b"\xff\xe1" + (len(new_payload) + 2).to_bytes(2, "big") + new_payload
    return image[:start] + segment + image[end:] + video
