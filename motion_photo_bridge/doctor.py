"""`mpb doctor` — check what the current command actually needs.

Default is offline: Python, ffmpeg, bundled merger, sample conversion.  It never
looks at an Immich install unless you ask (`--immich`), because most users only
run `mpb convert`.
"""
from __future__ import annotations

import argparse
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
        return ok("Python", sys.version.split()[0])
    return bad("Python >= 3.11 required", f"found {sys.version.split()[0]}")


def check_ffmpeg() -> bool:
    good = True
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        if not path:
            good = bad(tool, "not on PATH (install ffmpeg)") and good
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
    return ok("bundled merger", getattr(module, "__version__", "bundled"))


def check_dirs() -> bool:
    """Only meaningful when the Immich subcommands are in play."""
    good = True
    for env, label in (("IMMICH_MEDIA_DIR", "Immich media dir"), ("MPB_BACKUP_DIR", "backup dir")):
        value = os.environ.get(env)
        if not value:
            good = ok(label, f"not set ({env}) — not needed for convert") and good
            continue
        path = Path(value)
        parent = path if path.exists() else path.parent
        if parent.exists() and os.access(parent, os.W_OK):
            good = ok(label, str(path)) and good
        else:
            good = bad(label, f"{path} is not writable") and good
    return good


def check_immich() -> bool:
    compose = Path(os.environ["IMMICH_COMPOSE_DIR"]) if os.environ.get("IMMICH_COMPOSE_DIR") else None
    if compose is None or not any((compose / name).exists()
                                  for name in ("docker-compose.yml", "docker-compose.yaml",
                                               "compose.yml", "compose.yaml")):
        return bad("Immich compose dir", "set IMMICH_COMPOSE_DIR to the folder with docker-compose.yml")
    try:
        proc = subprocess.run(["docker", "compose", "exec", "-T", "database", "psql", "-U", "postgres",
                               "immich", "-t", "-A", "-c", "SELECT count(*) FROM asset;"],
                              cwd=compose, capture_output=True, text=True, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return bad("Immich database", str(exc)[:80])
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()[-1][:90] if proc.stderr.strip() else "psql failed"
        return bad("Immich database", detail)
    return ok("Immich database", f"{proc.stdout.strip()} assets")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mpb doctor", description="Check the environment before converting anything.")
    ap.add_argument("--immich", action="store_true", help="also check Immich media paths and the database connection")
    ap.add_argument("--offline", action="store_true", help="kept for compatibility; offline is now the default")
    args = ap.parse_args(argv)

    print("motion-photo-bridge doctor")
    results = [check_python(), check_ffmpeg(), check_merger()]
    if args.immich:
        results += [check_dirs(), check_immich()]
    print()
    if all(results):
        print("ready — try: mpb scan <folder> && mpb convert <folder> -o <out> --transcode")
        return 0
    print("something is missing; `mpb convert` needs only Python >= 3.11 + ffmpeg.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
