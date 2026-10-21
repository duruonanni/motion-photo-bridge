"""Real-world folders: cloud-sync junk, copies, renamed files."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from motion_photo_bridge import pairing  # noqa: E402


def touch(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_normalize_stem_undoes_copy_suffixes() -> None:
    assert pairing.normalize_stem("IMG_1 (1).JPG") == "img_1"
    assert pairing.normalize_stem("IMG_1（2）.jpg") == "img_1"
    assert pairing.normalize_stem("Copy of IMG_1.jpg") == "img_1"
    assert pairing.normalize_stem("IMG_1 - 副本.jpg") == "img_1"


def test_skips_cloud_junk(tmp_path: Path) -> None:
    touch(tmp_path / "._IMG_1.jpg")
    touch(tmp_path / "Thumbs.db")
    touch(tmp_path / "IMG_2.mp4.crdownload")
    touch(tmp_path / "@eaDir" / "SYNOPHOTO_THUMB.jpg")
    touch(tmp_path / "DCIM" / "IMG_1.jpg")
    touch(tmp_path / "DCIM" / "IMG_1.mp4")

    pairs, skipped = pairing.find_pairs(tmp_path)
    assert [p.image.name for p in pairs] == ["IMG_1.jpg"]
    reasons = {s.path.name: s.reason for s in skipped}
    # media-looking junk is reported; non-media junk (Thumbs.db) is ignored outright
    assert "junk" in reasons["._IMG_1.jpg"]
    assert "junk" in reasons["SYNOPHOTO_THUMB.jpg"]
    assert all(s.path.name != "Thumbs.db" for s in skipped)


def test_prefers_original_name_over_copy(tmp_path: Path) -> None:
    (tmp_path / "IMG_1 (1).jpg").write_bytes(b"copy")
    (tmp_path / "IMG_1 (1).mp4").write_bytes(b"copy")
    original_jpg = tmp_path / "IMG_1.jpg"
    original_jpg.write_bytes(b"original")
    original_mp4 = tmp_path / "IMG_1.mp4"
    original_mp4.write_bytes(b"original")

    pairs, skipped = pairing.find_pairs(tmp_path)
    assert len(pairs) == 1
    assert pairs[0].image == original_jpg and pairs[0].clip == original_mp4
    assert {s.path.name for s in skipped} == {"IMG_1 (1).jpg", "IMG_1 (1).mp4"}


def test_reports_mov_that_needs_a_flag(tmp_path: Path) -> None:
    touch(tmp_path / "IMG_9.jpg")
    touch(tmp_path / "IMG_9.mov")

    pairs, skipped = pairing.find_pairs(tmp_path)
    assert pairs == []
    assert any("--include-mov" in s.reason for s in skipped)

    pairs, _ = pairing.find_pairs(tmp_path, include_mov=True)
    assert [p.clip.name for p in pairs] == ["IMG_9.mov"]


def test_pairs_across_subdirectories_by_private_id(tmp_path: Path) -> None:
    import json

    blob = b"vivo" + json.dumps({"com.android.camera.livephoto": "abcdef1234567890"}, separators=(",", ":")).encode()
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "weird-name.jpg").write_bytes(b"\xff\xd8" + b"j" * 50 + blob)
    (tmp_path / "b" / "different.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"m" * 50 + blob)

    pairs, skipped = pairing.find_pairs(tmp_path)
    assert len(pairs) == 1 and pairs[0].how == "private-id"
    assert skipped == []


def test_stem_pairing_stays_inside_one_directory(tmp_path: Path) -> None:
    (tmp_path / "A").mkdir()
    (tmp_path / "B").mkdir()
    (tmp_path / "A" / "IMG_001.jpg").write_bytes(b"\xff\xd8" + b"j" * 40)
    (tmp_path / "B" / "IMG_001.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"m" * 40)

    pairs, skipped = pairing.find_pairs(tmp_path)
    assert pairs == [], "must not pair across folders without a private id"
    assert {s.path.name for s in skipped} == {"IMG_001.jpg", "IMG_001.mp4"}


def test_skips_trash_and_space_number_copies(tmp_path: Path) -> None:
    (tmp_path / ".trashed-1699999999-IMG_1.jpg").write_bytes(b"x")
    (tmp_path / "IMG_1 2.jpg").write_bytes(b"\xff\xd8" + b"j" * 10)
    (tmp_path / "IMG_1.jpg").write_bytes(b"\xff\xd8" + b"j" * 10)
    (tmp_path / "IMG_1.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"m" * 10)

    pairs, _ = pairing.find_pairs(tmp_path)
    assert [p.image.name for p in pairs] == ["IMG_1.jpg"]


def test_sql_helpers_reject_injection() -> None:
    from motion_photo_bridge import sql

    assert sql.uuid("1a243d49-73f1-4676-9530-c8b7f3df7db6").startswith("1a24")
    for bad in ("1; DROP TABLE asset", "", "not-a-uuid"):
        try:
            sql.uuid(bad)
        except SystemExit:
            continue
        raise AssertionError(f"uuid() accepted {bad!r}")
    assert sql.lit("a'b") == "'a''b'"
