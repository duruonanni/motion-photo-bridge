"""Read vivo's private live-photo metadata.

vivo/iQOO cameras do not write the standard Google Motion Photo XMP on the
older dual-file format.  Instead both files (the JPEG and the MP4) get a JSON
blob appended at the very end::

    vivo{"com.android.camera...","com.android.camera.livephoto":"<id>",...}

The ``com.android.camera.livephoto`` value is identical in the two files and is
the only reliable link between them: gallery apps rename files and rewrite
mtime/creation_time, but they keep the id.  The key name is *not* an Android
standard — it is vivo's private field, whatever the package-like prefix
suggests.

Newer vivo models (X300 and later) write a standard single-file Motion Photo
and keep the same JSON as a "double tail" (once before the ``ftyp`` box, once
at EOF).
"""

from __future__ import annotations

import json
from pathlib import Path

VIVO_SIG = b'vivo{"'
ID_FIELD = "com.android.camera.livephoto"
TAIL_SCAN_BYTES = 256 * 1024


def _tail(path: Path, limit: int = TAIL_SCAN_BYTES) -> bytes:
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > limit:
            fh.seek(size - limit)
        return fh.read()


def _json_after(buf: bytes, start: int) -> dict | None:
    """Decode the JSON object that starts at ``start`` (just past ``vivo``)."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(buf)):
        char = buf[index:index + 1]
        if in_string:
            if escaped:
                escaped = False
            elif char == b"\\":
                escaped = True
            elif char == b'"':
                in_string = False
            continue
        if char == b'"':
            in_string = True
        elif char == b"{":
            depth += 1
        elif char == b"}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(buf[start:index + 1].decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    return None
    return None


def read_meta(path: str | Path) -> dict | None:
    """Return vivo's appended JSON metadata, or ``None`` for a plain file.

    When the blob appears twice (X300-style single file) the last occurrence
    wins: that is the one that carries the ``livephoto`` id.
    """
    path = Path(path)
    try:
        buf = _tail(path)
    except OSError:
        return None
    found: dict | None = None
    cursor = 0
    while True:
        index = buf.find(VIVO_SIG, cursor)
        if index < 0:
            break
        meta = _json_after(buf, index + len(b"vivo"))
        if meta is not None:
            found = meta
        cursor = index + len(VIVO_SIG)
    return found


def live_photo_id(path: str | Path) -> str | None:
    """The private pairing id, e.g. ``17913510182383438e8b00000000``."""
    meta = read_meta(path)
    if not meta:
        return None
    value = meta.get(ID_FIELD)
    return str(value) if value else None


def is_motion_photo(path: str | Path) -> bool:
    """True when the file already embeds its clip (standard Motion Photo)."""
    path = Path(path)
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            head = fh.read(1 << 20)
            if b"MotionPhoto" in head:
                return True
            if size > (1 << 20):
                fh.seek(max(0, size - (1 << 21)))
                return b"MotionPhoto" in fh.read()
    except OSError:
        return False
    return False
