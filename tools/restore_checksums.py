#!/usr/bin/env python3
"""Restore original SHA1 checksums for assets that were merged in place.

Why: the Immich mobile app marks a local file as "uploaded" by comparing its
SHA1 with asset.checksum. Merging changed the bytes, so we keep the *original*
checksum in the DB (the file on disk is the merged one). This tool recomputes
those original hashes from the pre-merge backups.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from motion_photo_bridge import exif  # noqa: E402

COMPOSE = Path(os.environ.get("IMMICH_COMPOSE_DIR", ""))
MEDIA = Path(os.environ.get("IMMICH_MEDIA_DIR", ""))
BACKUP = Path(os.environ.get("MPB_BACKUP_DIR", ""))
OWNER_EMAIL = os.environ.get("VIVO_OWNER_EMAIL", "vivocamera@duruonanni.net")


def psql(q: str) -> list[list[str]]:
    p = subprocess.run(["docker", "compose", "exec", "-T", "database", "psql", "-U", "postgres",
                        "immich", "-t", "-A", "-F", "|", "-c", q],
                       cwd=COMPOSE, capture_output=True, text=True, timeout=180)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[:300])
    return [line.split("|") for line in p.stdout.strip().splitlines() if line]


def sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pick_image(stem: str, merged: Path) -> Path | None:
    cands = sorted((BACKUP / stem).glob("*.jpg")) + sorted((BACKUP / stem).glob("*.JPG"))
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    want = exif.read_datetime_original(merged)
    for c in cands:
        if exif.read_datetime_original(c) == want:
            return c
    return None


def pick_clip(stem: str, name: str) -> Path | None:
    suffix = Path(name).suffix.lower()
    cands = [p for p in (BACKUP / stem).glob("*") if p.suffix.lower() in (".mp4", ".mov")]
    same = [p for p in cands if p.suffix.lower() == suffix]
    return (same or cands or [None])[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    rows = psql(f"""
        SELECT i.id, i."originalFileName", i."originalPath",
               v.id, v."originalFileName"
        FROM asset i
        JOIN asset v ON v.id = i."livePhotoVideoId"
        JOIN "user" u ON u.id = i."ownerId"
        WHERE u.email = '{OWNER_EMAIL}' AND i."deletedAt" IS NULL
          AND v."originalPath" LIKE '/data/encoded-video/%'
          AND lower(i."originalFileName") ~ '\\.(jpg|jpeg)$'
        ORDER BY i."fileCreatedAt";""")
    if args.limit:
        rows = rows[: args.limit]
    print(f"候选 {len(rows)} 对")

    img_ok = vid_ok = 0
    img_missing = vid_missing = 0
    for img_id, img_name, img_path, vid_id, vid_name in rows:
        stem = Path(img_name).stem
        merged = MEDIA / Path(img_path).relative_to("/data")

        original_img = pick_image(stem, merged)
        if original_img:
            if not args.dry_run:
                psql(f"UPDATE asset SET checksum=decode('{sha1(original_img)}','hex') WHERE id='{img_id}';")
            img_ok += 1
        else:
            img_missing += 1

        original_clip = pick_clip(stem, vid_name)
        if original_clip:
            if not args.dry_run:
                psql(f"UPDATE asset SET checksum=decode('{sha1(original_clip)}','hex') WHERE id='{vid_id}';")
            vid_ok += 1
        else:
            vid_missing += 1

    mode = "[dry-run] " if args.dry_run else ""
    print(f"{mode}image checksum restored: {img_ok}, unmatched: {img_missing}")
    print(f"{mode}clip  checksum restored: {vid_ok}, unmatched: {vid_missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
