"""Backups must never overwrite a same-named photo from another account."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def load_apply_pair():
    from motion_photo_bridge import immich_apply

    return immich_apply


def test_same_stem_with_different_bytes_gets_its_own_dir(tmp_path: Path) -> None:
    module = load_apply_pair()
    module.BACKUP = tmp_path

    existing = tmp_path / "IMG_1"
    existing.mkdir()
    (existing / "IMG_1.jpg").write_bytes(b"other account's photo")

    source = tmp_path / "src.jpg"
    source.write_bytes(b"my photo")
    clip = tmp_path / "src.mp4"
    clip.write_bytes(b"my clip")

    chosen = module.back_up("IMG_1", "abcdef123456", source, "IMG_1.jpg", clip, "IMG_1.mp4")
    assert chosen == tmp_path / "IMG_1__abcdef12"
    assert chosen.is_dir()


def test_identical_bytes_reuse_the_existing_dir(tmp_path: Path) -> None:
    module = load_apply_pair()
    module.BACKUP = tmp_path

    existing = tmp_path / "IMG_2"
    existing.mkdir()
    (existing / "IMG_2.jpg").write_bytes(b"same photo")
    (existing / "IMG_2.mp4").write_bytes(b"same clip")

    source = tmp_path / "src.jpg"
    source.write_bytes(b"same photo")
    clip = tmp_path / "src.mp4"
    clip.write_bytes(b"same clip")

    assert module.back_up("IMG_2", "123456789abc", source, "IMG_2.jpg", clip,
                          "IMG_2.mp4") == existing


def test_dry_run_does_not_create_directories(tmp_path: Path) -> None:
    module = load_apply_pair()
    module.BACKUP = tmp_path / "backups"

    source = tmp_path / "src.jpg"
    source.write_bytes(b"x")
    clip = tmp_path / "src.mp4"
    clip.write_bytes(b"y")

    module.back_up("IMG_3", "deadbeef00", source, "IMG_3.jpg", clip, "IMG_3.mp4", create=False)
    assert not (tmp_path / "backups").exists()
