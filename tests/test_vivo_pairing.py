"""Pairing must survive a rename: the vivo private id is authoritative."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from motion_photo_bridge import vivo  # noqa: E402
from motion_photo_bridge import pairing  # noqa: E402


def find_pairs(root):
    pairs, _skipped = pairing.find_pairs(root)
    return [(p.image, p.clip) for p in pairs]

MAGIC = bytes([255, 255, 255, 255, 27, 42, 57, 72, 87, 102, 117, 132, 147, 162, 179])


def vivo_blob(ident: str, model: str = "vivo X100 Ultra") -> bytes:
    meta = {
        "com.android.camera.takenmodel": model,
        "com.android.camera.moduleid": "photo",
        "com.android.camera.livephoto": ident,
        "version": 2104,
        "com.android.camera.project": "PD2366",
    }
    return b"vivo" + json.dumps(meta, separators=(",", ":")).encode() + MAGIC


def make_pair(directory: Path, jpg_name: str, mp4_name: str, ident: str) -> tuple[Path, Path]:
    jpg = directory / jpg_name
    mp4 = directory / mp4_name
    jpg.write_bytes(b"\xff\xd8\xff\xe0" + b"jpeg-payload" * 64 + vivo_blob(ident))
    mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"mp4-payload" * 64 + vivo_blob(ident))
    return jpg, mp4


def test_reads_id_from_both_files(tmp_path: Path) -> None:
    jpg, mp4 = make_pair(tmp_path, "IMG_1.jpg", "IMG_1.mp4", "17913510182383438e8b00000000")
    assert vivo.live_photo_id(jpg) == "17913510182383438e8b00000000"
    assert vivo.live_photo_id(mp4) == "17913510182383438e8b00000000"


def test_plain_file_has_no_id(tmp_path: Path) -> None:
    plain = tmp_path / "IMG_2.jpg"
    plain.write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 5000)
    assert vivo.read_meta(plain) is None
    assert vivo.live_photo_id(plain) is None


def test_pairs_renamed_files_by_id(tmp_path: Path) -> None:
    jpg, mp4 = make_pair(tmp_path, "IMG_20260509_102002.jpg", "IMG_20260509_102002.mp4",
                         "17913510182383438e8b00000000")
    renamed_mp4 = tmp_path / "clip-renamed-by-gallery.mp4"
    mp4.rename(renamed_mp4)

    pairs = find_pairs(tmp_path)
    assert pairs == [(jpg, renamed_mp4)]


def test_falls_back_to_stem_without_metadata(tmp_path: Path) -> None:
    jpg = tmp_path / "IMG_3.jpg"
    mp4 = tmp_path / "IMG_3.mp4"
    jpg.write_bytes(b"\xff\xd8" + b"x" * 1000)
    mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"y" * 1000)

    assert find_pairs(tmp_path) == [(jpg, mp4)]


def test_does_not_pair_different_ids(tmp_path: Path) -> None:
    (tmp_path / "a.jpg").write_bytes(b"\xff\xd8" + b"jpeg" * 10
                                     + vivo_blob("aaaaaaaaaaaaaaaaaaaa00000000"))
    mp4 = tmp_path / "b.mp4"
    mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"mp4" * 10
                    + vivo_blob("bbbbbbbbbbbbbbbbbbbb00000000"))
    mp4.rename(tmp_path / "b-renamed.mp4")

    assert find_pairs(tmp_path) == []


def test_finds_double_tail_from_newer_models(tmp_path: Path) -> None:
    """X300-style single file: vendor blob before ftyp and again at EOF."""
    first = vivo_blob("", model="vivo X300 Pro")
    EOF_ID = "1792000000000000abcd00000000"
    path = tmp_path / "IMG_4.jpg"
    path.write_bytes(
        b"\xff\xd8\xff\xe0" + b"jpeg" * 100 + first
        + b"\x00\x00\x00\x18ftypmp42" + b"video" * 100
        + vivo_blob(EOF_ID, model="vivo X300 Pro")
    )
    assert vivo.live_photo_id(path) == EOF_ID
