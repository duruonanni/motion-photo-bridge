"""`mpb doctor` — check everything the pipeline needs before it fails halfway."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def ok(label: str, detail: str = "") -> bool:
    print(f"  \u2713 {label}" + (f"  {detail}" if detail else ""))
    return True


def bad(label: str, detail: str = "") -> bool:
    print(f"  \u2717 {label}" + (f"  {detail}" if detail else ""))
    return False


def check_python() -> bool:
    if sys.version_info >= (3, 11):
        return ok("Python", f"{sys.version.split()[0]}")
    return bad("Python >= 3.11 required", f"found {sys.version.split()[0]}")


def check_ffmpeg() -> bool:
    good = True
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        if not path:
            good = bad(tool, "not on PATH")
            continue
        try:
            version = subprocess.run([tool, "-version"], capture_output=True, text=True, timeout=10).stdout.splitlines()[0]
        except Exception as exc:  # noqa: BLE001
            version = str(exc)
        good = ok(tool, version[:70]) and good
    return good


def check_merger() -> bool:
    try:
        import importlib
        module = importlib.import_module("motion_photo_bridge._vendor.vivo_motion_photo")
    except Exception as exc:  # noqa: BLE001
        return bad("bundled merger", f"cannot import: {exc}")
    return ok("bundled merger", getattr(module, "__version__", ""))


def check_outputs() -> bool:
    from .immich_apply import BACKUP, MEDIA
    good = True
    for label, path in (("media dir", MEDIA), ("backup dir", BACKUP)):
        parent = path if path.exists() else path.parent
        if parent.exists() and os.access(parent, os.W_OK):
            good = ok(label, str(path)) and good
        else:
            good = bad(label, f"{path} not writable (set IMMICH_MEDIA_DIR / MPB_BACKUP_DIR)") and good
    return good


def check_immich() -> bool:
    compose = Path(os.environ.get("IMMICH_COMPOSE_DIR", Path.home() / "hermes-workspace/projects/immich"))
    url = os.environ.get("IMMICH_URL", "http://localhost:2283")
    if not (compose / "docker-compose.yml").exists() and not (compose / "compose.yaml").exists():
        return bad("Immich compose dir", f"{compose} (set IMMICH_COMPOSE_DIR) — only needed for library mode")
    try:
        proc = subprocess.run(["docker", "compose", "exec", "-T", "database", "psql", "-U", "postgres",
                               "immich", "-t", "-A", "-c", "SELECT count(*) FROM asset;"],
                              cwd=compose, capture_output=True, text=True, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return bad("Immich database", str(exc)[:80])
    if proc.returncode != 0:
        return bad("Immich database", proc.stderr.strip().splitlines()[-1][:90] if proc.stderr.strip() else "psql failed")
    return ok("Immich database", f"{proc.stdout.strip()} assets at {url}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mpb doctor", description="Check the environment before converting anything.")
    ap.add_argument("--offline", action="store_true", help="skip the Immich/library checks")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    print("motion-photo-bridge doctor")
    results = [check_python(), check_ffmpeg(), check_merger()]
    if not args.offline:
        results += [check_outputs(), check_immich()]
    print()
    if all(results):
        print("ready — try: mpb scan <folder> && mpb convert <folder> -o <out> --transcode")
        return 0
    print("some checks failed; offline conversion needs only Python + ffmpeg.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
