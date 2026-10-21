#!/usr/bin/env python3
"""Keep an Immich library self-healing: link vivo pairs, then merge them.

Run it on a timer (we use every 6 hours).  It is idempotent — when nothing new
has been uploaded it does one cheap query and exits silently.

Stage 1  link   : dual-file live photos become Immich live photos
                  (image gets ``livePhotoVideoId``, clip is hidden).
                  Matched by vivo's private id first, filename stem second.
Stage 2  merge  : every linked pair whose clip still sits in ``upload/`` is
                  rewritten into one standard Motion Photo (see apply_pair.py)
                  and the clip is moved to ``encoded-video/`` so downloads and
                  shares yield a single file.

Both stages are reversible: stage 2 copies the originals to ``MPB_BACKUP_DIR``
before touching anything, and stage 1 only writes reference/visibility fields.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from motion_photo_bridge import vivo  # noqa: E402

COMPOSE = Path(os.environ.get("IMMICH_COMPOSE_DIR", Path.home() / "hermes-workspace/projects/immich"))
MEDIA = Path(os.environ.get("IMMICH_MEDIA_DIR", "/mnt/ztb/media/immich"))
OWNER = os.environ.get("VIVO_OWNER_EMAIL", "")  # empty = every owner that has vivo photos
MAX_MOTION_MS = 10_000
ID_WINDOW_SECONDS = 86_400
LOCK_FILE = Path(os.environ.get("MPB_LOCK", "/tmp/motion-photo-bridge.auto.lock"))
APPLY_PAIR = Path(__file__).with_name("apply_pair.py")


def psql(query: str) -> list[list[str]]:
    proc = subprocess.run(
        ["docker", "compose", "exec", "-T", "database", "psql", "-U", "postgres",
         "immich", "-t", "-A", "-F", "|", "-c", query],
        cwd=COMPOSE, capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:400])
    return [line.split("|") for line in proc.stdout.strip().splitlines() if line]


def update_count(rows: list[list[str]], tag: str) -> int:
    """psql prints ``UPDATE 0`` as a row even when nothing matched."""
    for row in rows:
        cell = (row[0] if row else "").strip()
        if cell.startswith(f"{tag} "):
            return int(cell.split()[1])
    return len(rows)


def host_path(container_path: str) -> Path:
    return MEDIA / Path(container_path).relative_to("/data")


def sql_owners(owners: list[str]) -> str:
    quoted = ",".join("'" + email.replace("'", "''") + "'" for email in owners)
    return f"({quoted})"


def vivo_owners() -> list[str]:
    """Every account that has at least one vivo/iQOO photo (owners are isolated)."""
    rows = psql("""SELECT DISTINCT u.email FROM asset_exif e
                    JOIN asset a ON a.id=e.\"assetId\"
                    JOIN \"user\" u ON u.id=a.\"ownerId\"
                    WHERE e.model ILIKE '%vivo%' OR e.model ILIKE '%iqoo%';""")
    return [row[0] for row in rows if row and row[0]]


def link_by_stem(owners: list[str]) -> int:
    """Same-name jpg + short mp4 → link.  Returns how many were linked."""
    sql = f"""
    WITH u AS (SELECT id FROM "user" WHERE email IN {sql_owners(owners)}),
    vids AS (SELECT a.id, a.duration,
                    lower(regexp_replace(a."originalFileName",'\\.(mp4|mov|3gp)$','')) AS stem
             FROM asset a WHERE a."ownerId"IN (SELECT id FROM u) AND a.type='VIDEO'),
    imgs AS (SELECT a.id, a."livePhotoVideoId",
                    lower(regexp_replace(a."originalFileName",'\\.(jpg|jpeg|heic)$','')) AS stem
             FROM asset a WHERE a."ownerId"IN (SELECT id FROM u) AND a.type='IMAGE'),
    pairs AS (SELECT i.id AS img_id, v.id AS vid_id
              FROM imgs i JOIN vids v ON v.stem=i.stem
              WHERE v.duration<={MAX_MOTION_MS} AND i."livePhotoVideoId" IS NULL)
    UPDATE asset SET "livePhotoVideoId"=p.vid_id FROM pairs p WHERE asset.id=p.img_id
    RETURNING asset.id;"""
    return update_count(psql(sql), "UPDATE")


def link_by_id(owners: list[str], limit: int = 200) -> list[str]:
    """Link pairs whose filenames disagree but whose private id matches."""
    videos = psql(f"""
        SELECT v.id, v."originalPath", v."originalFileName"
        FROM asset v JOIN "user" u ON u.id=v."ownerId"
        WHERE u.email IN {sql_owners(owners)} AND v.type='VIDEO' AND v.duration<={MAX_MOTION_MS}
          AND NOT EXISTS (SELECT 1 FROM asset i WHERE i."livePhotoVideoId"=v.id)
        ORDER BY v."fileCreatedAt" DESC LIMIT {limit};""")
    linked: list[str] = []
    for vid_id, vid_path, vid_name in videos:
        try:
            ident = vivo.live_photo_id(host_path(vid_path))
        except OSError:
            continue
        if not ident:
            continue
        candidates = psql(f"""
            SELECT i.id, i."originalPath"
            FROM asset i
            WHERE i.type='IMAGE'
              AND i."livePhotoVideoId" IS NULL
              AND i."ownerId" IN (SELECT id FROM "user" WHERE email IN {sql_owners(owners)})
              AND i."fileCreatedAt" BETWEEN
                  (SELECT "fileCreatedAt" - interval '{ID_WINDOW_SECONDS} seconds'
                   FROM asset WHERE id='{vid_id}')
                  AND
                  (SELECT "fileCreatedAt" + interval '{ID_WINDOW_SECONDS} seconds'
                   FROM asset WHERE id='{vid_id}')
            LIMIT 50;""")
        match = None
        for img_id, img_path in candidates:
            try:
                if vivo.live_photo_id(host_path(img_path)) == ident:
                    match = img_id
                    break
            except OSError:
                continue
        if not match:
            continue
        psql(f"UPDATE asset SET \"livePhotoVideoId\"='{vid_id}' WHERE id='{match}' "
             f"AND \"livePhotoVideoId\" IS NULL;")
        psql(f"UPDATE asset SET visibility='hidden' WHERE id='{vid_id}' AND visibility<>'hidden';")
        linked.append(f"{vid_name} → id {ident}")
    return linked


def hide_linked_clips(owners: list[str]) -> int:
    return update_count(psql(f"""
        WITH u AS (SELECT id FROM "user" WHERE email IN {sql_owners(owners)})
        UPDATE asset SET visibility='hidden'
        WHERE type='VIDEO' AND visibility<>'hidden'
          AND id IN (SELECT "livePhotoVideoId" FROM asset WHERE "livePhotoVideoId" IS NOT NULL)
          AND "ownerId"IN (SELECT id FROM u) RETURNING asset.id;"""), "UPDATE")


def pending_merges(owners: list[str], limit: int = 0, include_mov: bool = False) -> list[list[str]]:
    # Apple-style pairs (JPG + MOV with still-image-time) are already a coherent
    # live photo and are left alone: merging them gains nothing and risks it.
    mov_clause = "" if include_mov else "AND lower(v.\"originalFileName\") NOT LIKE '%.mov'"
    sql = f"""
        SELECT i.id, i."originalFileName", v.duration
        FROM asset i
        JOIN asset v ON v.id=i."livePhotoVideoId"
        JOIN asset_exif e ON e."assetId"=i.id
        JOIN "user" u ON u.id=i."ownerId"
        WHERE u.email IN {sql_owners(owners)} AND i."deletedAt" IS NULL AND i.type='IMAGE'
          AND (e.model ILIKE '%vivo%' OR e.model ILIKE '%iqoo%')
          AND lower(i."originalFileName") ~ '\\.(jpg|jpeg)$'
          AND i."livePhotoVideoId" IS NOT NULL
          AND v."originalPath" NOT LIKE '/data/encoded-video/%'
          AND v.duration<={MAX_MOTION_MS}
          {mov_clause}
        ORDER BY i."localDateTime"
    """
    if limit:
        sql += f" LIMIT {limit}"
    return psql(sql + ";")


def merge(rows: list[list[str]]) -> tuple[int, list[tuple[str, str]]]:
    ok, failures = 0, []
    for index, (img_id, name, duration) in enumerate(rows, 1):
        proc = subprocess.run([sys.executable, str(APPLY_PAIR), "--image-id", img_id],
                              capture_output=True, text=True)
        if proc.returncode == 0:
            ok += 1
            print(f"[{index}/{len(rows)}] {name} ({int(duration) / 1000:.2f}s) merged", flush=True)
        else:
            detail = (proc.stderr or proc.stdout).strip().splitlines()[-1][:180]
            failures.append((name, detail))
            print(f"[{index}/{len(rows)}] {name} FAILED: {detail}", flush=True)
    return ok, failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", action="append", default=None,
                    help="owner email (repeatable); default = every vivo/iQOO owner")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="cap merges per run (0 = all)")
    ap.add_argument("--skip-link", action="store_true", help="only merge, do not link")
    ap.add_argument("--include-mov", action="store_true",
                    help="also merge Apple-style JPG+MOV pairs (skipped by default)")
    ap.add_argument("--quiet", action="store_true", help="print nothing when there is no work")
    args = ap.parse_args()

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock = LOCK_FILE.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another run is in progress, exiting", file=sys.stderr)
        return 0

    owners = args.owner or ([OWNER] if OWNER else []) or vivo_owners()
    if not owners:
        print("auto-merge: no vivo/iQOO owner found", flush=True)
        return 0

    started = time.time()
    linked_stem = linked_id = hidden = 0
    if not args.skip_link:
        linked_stem = 0 if args.dry_run else link_by_stem(owners)
        linked_id = 0 if args.dry_run else len(link_by_id(owners))
        hidden = 0 if args.dry_run else hide_linked_clips(owners)

    rows = pending_merges(owners, args.limit, include_mov=args.include_mov)
    if args.dry_run:
        print(f"[dry-run] would merge {len(rows)} pair(s); link-by-stem={linked_stem}")
        for img_id, name, _duration in rows[:10]:
            print(f"  {name}  {img_id}")
        return 0

    ok, failures = merge(rows) if rows else (0, [])
    elapsed = time.time() - started

    if ok or linked_stem or linked_id or failures:
        print(f"\nauto-merge: linked={linked_stem + linked_id} (id-matched={linked_id}) "
              f"hidden={hidden} merged={ok} failed={len(failures)} "
              f"in {elapsed / 60:.1f} min", flush=True)
    elif not args.quiet:
        print("auto-merge: nothing to do", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
