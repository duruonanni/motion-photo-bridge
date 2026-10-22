"""Contract tests for the vivo X300 single-file flavour (``--target vivo``)."""

from __future__ import annotations

import pytest

from motion_photo_bridge import vivo_single

GOOGLE_XMP = (
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description rdf:about="" '
    'xmlns:GCamera="http://ns.google.com/photos/1.0/camera/" '
    'xmlns:Container="http://ns.google.com/photos/1.0/container/" '
    'xmlns:Item="http://ns.google.com/photos/1.0/container/item/" '
    'GCamera:MotionPhoto="1" GCamera:MotionPhotoVersion="1" '
    'GCamera:MotionPhotoPresentationTimestampUs="-1">'
    "<Container:Directory><rdf:Seq>"
    '<rdf:li rdf:parseType="Resource"><Container:Item Item:Mime="image/jpeg" '
    'Item:Length="0" Item:Padding="0" Item:Semantic="Primary"/></rdf:li>'
    '<rdf:li rdf:parseType="Resource"><Container:Item Item:Mime="video/mp4" '
    'Item:Length="{video}" Item:Padding="0" Item:Semantic="MotionPhoto"/></rdf:li>'
    "</rdf:Seq></Container:Directory></rdf:Description></rdf:RDF></x:xmpmeta>"
)


def build_merged(video_size: int = 64) -> bytes:
    video = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * (video_size - 12)
    xmp = GOOGLE_XMP.format(video=len(video)).encode()
    payload = b"http://ns.adobe.com/xap/1.0/\x00" + xmp
    app1 = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    return b"\xff\xd8" + app1 + b"\xff\xd9" + video


def test_rewrites_to_vivo_shape():
    merged = build_merged()
    out = vivo_single.rewrite(merged)

    assert b'xmlns:VCamera="http://ns.vivo.com/photos/1.0/camera/"' in out
    assert b'VCamera:VMotionPhotoVersion="1"' in out
    assert b'VCamera:VMotionPhotoSource="1"' in out
    assert b'VCamera:VMediaKitVersion="' in out
    # vivo writes a zero timestamp and never puts Length/Padding on Primary.
    assert b'GCamera:MotionPhotoPresentationTimestampUs="0"' in out
    assert b'Item:Semantic="Primary"' in out
    assert b'Item:Semantic="Primary"' in out and b'Item:Length="0"' not in out.split(b"MotionPhoto")[0]
    assert b'Item:Padding="0" Item:Semantic="MotionPhoto"' in out


def test_video_bytes_and_length_survive():
    merged = build_merged()
    out = vivo_single.rewrite(merged)
    video = merged[-vivo_single.video_length(merged):]

    assert out.endswith(video)
    assert int(vivo_single.video_length(out)) == len(video)


def test_exif_and_image_bytes_untouched():
    merged = build_merged()
    out = vivo_single.rewrite(merged)
    # Everything before the APP1 segment (SOI + any EXIF) is copied verbatim.
    assert out.startswith(b"\xff\xd8")
    assert merged[:2] == out[:2]
    # The image tail after the XMP segment is preserved.
    assert merged.split(b"\xff\xd9")[0].endswith(b"</x:xmpmeta>")
    assert out.split(b"</x:xmpmeta>")[1].startswith(b"\xff\xd9")


def test_unrelated_xmp_properties_survive():
    merged = build_merged().replace(b'GCamera:MotionPhoto="1"',
                                    b'GCamera:MotionPhoto="1" xmp:Rating="5" xmp:CreatorTool="Photos"')
    out = vivo_single.rewrite(merged)
    assert b'xmp:Rating="5"' in out and b'xmp:CreatorTool="Photos"' in out


def test_primary_padding_is_preserved():
    merged = build_merged().replace(b'Item:Length="0" Item:Padding="0" Item:Semantic="Primary"',
                                    b'Item:Length="0" Item:Padding="571" Item:Semantic="Primary"')
    out = vivo_single.rewrite(merged)
    primary = out.split(b"Semantic=\"Primary\"")[0].split(b"<Container:Item")[-1]
    assert b'Item:Padding="571"' in primary and b"Item:Length" not in primary


def test_rejects_file_without_xmp():
    with pytest.raises(ValueError):
        vivo_single.rewrite(b"\xff\xd8" + b"\x00" * 100)


def test_rejects_motion_item_without_length():
    xmp = GOOGLE_XMP.format(video=0).replace(b'Item:Length="0" Item:Padding="0" Item:Semantic="MotionPhoto"'.decode(), "").encode()
    payload = b"http://ns.adobe.com/xap/1.0/\x00" + xmp
    app1 = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    with pytest.raises(ValueError):
        vivo_single.rewrite(b"\xff\xd8" + app1 + b"\xff\xd9")
