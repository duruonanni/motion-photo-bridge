"""Find dual-file live photos in messy, real-world folders.

Designed for three sources that behave differently:

* the phone's own DCIM folder           — clean names, ids present
* a copy pasted out of the vivo gallery — same names, ids may be stripped
* a cloud-sync folder (OneDrive/NAS)    — junk files, " (1)" duplicates, partials
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import vivo

IMAGE_EXT = {".jpg", ".jpeg"}
VIDEO_EXT = {".mp4"}
VIDEO_EXT_MOV = {".mov"}

JUNK_DIRS = {"@eadir", "#recycle", ".thumbnails", ".trashes", "__macosx", "$recycle.bin"}
JUNK_NAMES = {"thumbs.db", "desktop.ini", ".ds_store", "icon\r", ".directory"}
JUNK_SUFFIX = (".tmp", ".part", ".crdownload", ".download", "~")
# "IMG_1234 2.jpg" — Synology/qBittorrent style second copy
_DUP_SPACE = re.compile(r"\s\d+$")

_DUP = re.compile(r"\s*[\(（]\d+[\)）]$")
_COPY = re.compile(r"^\s*(copy of|副本\s*[-—]?\s*|[-—]\s*副本)\s*", re.I)


@dataclass
class Pair:
    image: Path
    clip: Path
    how: str


@dataclass
class Skipped:
    path: Path
    reason: str


def is_junk(path: Path) -> bool:
    name = path.name
    lowered = name.lower()
    if any(part.lower() in JUNK_DIRS for part in path.parts[:-1]):
        return True
    if lowered in JUNK_NAMES or name.startswith(("._", ".trashed-", ".@__thumb")):
        return True
    return lowered.endswith(JUNK_SUFFIX)


def normalize_stem(name: str) -> str:
    """Undo the renaming a copy or a cloud client adds: ``IMG_1 (1).JPG``."""
    stem = Path(name).stem.strip().lower()
    stem = _DUP.sub("", stem)
    stem = _DUP_SPACE.sub("", stem)
    stem = _COPY.sub("", stem)
    stem = re.sub(r"\s*[-—]\s*副本$", "", stem)
    return stem.strip()


def collect(root: Path, include_mov: bool = False) -> tuple[list[Path], list[Path], list[Skipped]]:
    images: list[Path] = []
    videos: list[Path] = []
    skipped: list[Skipped] = []
    video_ext = VIDEO_EXT | (VIDEO_EXT_MOV if include_mov else set())
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext == ".mov" and not include_mov:
            skipped.append(Skipped(path, "MOV clip ignored (rerun with --include-mov)"))
            continue
        if ext not in IMAGE_EXT and ext not in video_ext:
            continue
        if is_junk(path):
            skipped.append(Skipped(path, "junk / sync artefact"))
            continue
        (images if ext in IMAGE_EXT else videos).append(path)
    return images, videos, skipped


def pick(candidates: list[Path], stem: str) -> Path:
    """Choose among copies of the same photo.

    A sync client creates ``IMG_1 (1).jpg``; the original name wins even when the
    copy is newer, otherwise every rerun would drift to the duplicates.  Within
    the same class the newest file wins, and for ties the shorter name.
    """
    def score(path: Path) -> tuple:
        exact = path.stem.strip().lower() == stem
        duplicated = bool(_DUP.search(path.stem))
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        return (not exact, duplicated, -mtime, len(path.name))

    return sorted(candidates, key=score)[0]


def find_pairs(root: str | Path, include_mov: bool = False) -> tuple[list[Pair], list[Skipped]]:
    root = Path(root)
    images, videos, skipped = collect(root, include_mov=include_mov)
    used_images: set[Path] = set()
    used_videos: set[Path] = set()
    pairs: list[Pair] = []

    # Pass 1 — vivo's private id: survives renames, duplicate suffixes and mtime edits.
    by_id: dict[str, list[Path]] = {}
    for image in images:
        ident = vivo.live_photo_id(image)
        if ident:
            by_id.setdefault(ident, []).append(image)
    for video in videos:
        ident = vivo.live_photo_id(video)
        if not ident:
            continue
        for image in by_id.get(ident, []):
            if image in used_images:
                continue
            pairs.append(Pair(image, video, "private-id"))
            used_images.add(image)
            used_videos.add(video)
            break

    # Pass 2 — normalised filename stem (case, " (1)" copies, "副本" prefixes).
    # Keyed by (directory, stem): pairing across folders would attach A/IMG_001.jpg
    # to B/IMG_001.mp4, which is almost never what the user means.  The private
    # id above is the only signal allowed to cross directories.
    img_by_stem: dict[tuple[Path, str], list[Path]] = {}
    for image in images:
        if image not in used_images:
            img_by_stem.setdefault((image.parent, normalize_stem(image.name)), []).append(image)
    vid_by_stem: dict[tuple[Path, str], list[Path]] = {}
    for video in videos:
        if video not in used_videos:
            vid_by_stem.setdefault((video.parent, normalize_stem(video.name)), []).append(video)

    for key in sorted(img_by_stem.keys() & vid_by_stem.keys(), key=lambda k: (str(k[0]), k[1])):
        directory, stem = key
        cand_images = [p for p in img_by_stem[key] if p not in used_images]
        cand_videos = [p for p in vid_by_stem[key] if p not in used_videos]
        if not cand_images or not cand_videos:
            continue
        best_image = pick(cand_images, stem)
        best_video = pick(cand_videos, stem)
        pairs.append(Pair(best_image, best_video, "stem"))
        used_images.add(best_image)
        used_videos.add(best_video)
        for extra in cand_images:
            if extra is not best_image:
                skipped.append(Skipped(extra, "duplicate image (an identical-looking copy exists)"))
        for extra in cand_videos:
            if extra is not best_video:
                skipped.append(Skipped(extra, "duplicate clip (an identical-looking copy exists)"))

    for image in images:
        if image not in used_images:
            skipped.append(Skipped(image, "no matching clip"))
    for video in videos:
        if video not in used_videos:
            skipped.append(Skipped(video, "no matching image"))

    # A file can earn two reasons (duplicate + unmatched); report it once.
    seen: set[Path] = set()
    unique: list[Skipped] = []
    for item in skipped:
        if item.path in seen:
            continue
        seen.add(item.path)
        unique.append(item)
    return pairs, unique
