"""A gallery copy often keeps JPEG EXIF while the clip lost its timestamps."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from motion_photo_bridge import exif  # noqa: E402


def test_reads_exif_from_the_repo_sample() -> None:
    sample = REPO / "docs" / "sample" / "IMG_SAMPLE-stamped.jpg"
    if not sample.exists():
        return
    got = exif.read_datetime_original(sample)
    assert got, "sample should carry EXIF dates"
    assert got[0].startswith("2026-05-09")
    utc = exif.read_datetime_utc(sample)
    assert utc and utc.endswith("Z")


def test_missing_exif_is_none(tmp_path: Path) -> None:
    plain = tmp_path / "plain.jpg"
    plain.write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 200)
    assert exif.read_datetime_original(plain) is None
    assert exif.read_datetime_utc(plain) is None
