#!/usr/bin/env python3
"""Apply the bridge to every pending vivo pair in Immich (resumable)."""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

COMPOSE = Path.home() / "hermes-workspace/projects/immich"
SCRIPT = Path(__file__).with_name("apply_pair.py")
PY = sys.executable


def psql(query: str) -> list[list[str]]:
    proc = subprocess.run(
        ["docker", "compose", "exec", "-T", "database", "psql", "-U", "postgres",
         "immich", "-t", "-A", "-F", "|", "-c", query],
        cwd=COMPOSE, capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:400])
    return [line.split("|") for line in proc.stdout.strip().splitlines() if line]


def pending(owner: str) -> list[list[str]]:
    return psql(f"""
        SELECT i.id, i."originalFileName", v.duration
        FROM asset i
        JOIN asset v ON v.id = i."livePhotoVideoId"
        JOIN "user" u ON u.id = i."ownerId"
        WHERE u.email = '{owner}'
          AND i."deletedAt" IS NULL
          AND i.type = 'IMAGE'
          AND v."originalPath" NOT LIKE '/data/encoded-video/%'
          AND v.duration <= 10000
        ORDER BY i."localDateTime";""")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", default="vivocamera@duruonanni.net")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = pending(args.owner)
    if args.limit:
        rows = rows[: args.limit]
    print(f"待处理 {len(rows)} 对", flush=True)
    if args.dry_run or not rows:
        return 0

    ok = fail = 0
    failures: list[tuple[str, str]] = []
    started = time.time()
    for index, (img_id, name, duration) in enumerate(rows, 1):
        print(f"[{index}/{len(rows)}] {name} ({int(duration)/1000:.2f}s) ... ", end="", flush=True)
        proc = subprocess.run([PY, str(SCRIPT), "--image-id", img_id],
                              capture_output=True, text=True)
        if proc.returncode == 0:
            ok += 1
            print("OK", flush=True)
        else:
            fail += 1
            detail = (proc.stderr or proc.stdout).strip().splitlines()[-1][:180]
            failures.append((name, detail))
            print(f"FAIL: {detail}", flush=True)

    elapsed = time.time() - started
    print(f"\n完成: {ok} 成功 / {fail} 失败, 用时 {elapsed/60:.1f} 分钟", flush=True)
    for name, detail in failures:
        print(f"  ✗ {name}: {detail}", flush=True)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
