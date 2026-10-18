"""Command line interface for motion-photo-bridge."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import transcode as tc

IMAGE_EXT = {".jpg", ".jpeg"}
VIDEO_EXT = {".mp4"}
MAX_MOTION_SECONDS = 10.0


def find_pairs(root: Path) -> list[tuple[Path, Path]]:
    """Return (image, clip) pairs that share a filename stem."""
    images: dict[str, Path] = {}
    videos: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext in IMAGE_EXT:
            images[path.stem.lower()] = path
        elif ext in VIDEO_EXT:
            videos[path.stem.lower()] = path
    return [(images[k], videos[k]) for k in sorted(images.keys() & videos.keys())]


def _merger_cmd() -> list[str]:
    exe = shutil.which("android-motion-photo-merger")
    return [exe] if exe else [sys.executable, "-m", "vivo_motion_photo"]


def convert_one(jpg: Path, clip: Path, out_dir: Path, do_transcode: bool, dry_run: bool) -> dict:
    info = tc.probe(clip)
    result = {
        "image": jpg.name,
        "clip": clip.name,
        "duration_s": round(info.duration, 2),
        "pix_fmt": info.pix_fmt,
        "color_range": info.color_range,
        "transcoded": False,
        "ok": False,
    }
    if info.duration > MAX_MOTION_SECONDS:
        result["skipped"] = f"clip is {info.duration:.1f}s (> {MAX_MOTION_SECONDS}s), looks like a real video"
        return result

    if dry_run:
        result["ok"] = True
        result["dry_run"] = True
        return result

    with tempfile.TemporaryDirectory(prefix="mpb-") as td:
        work = Path(td)
        staged_clip = clip
        if do_transcode and info.needs_range_fix:
            staged_clip = work / "transcoded.mp4"
            tc.transcode(clip, staged_clip)
            result["transcoded"] = True
        pair_dir = work / "pair"
        pair_dir.mkdir()
        shutil.copy2(jpg, pair_dir / jpg.name)
        shutil.copy2(staged_clip, pair_dir / clip.name)

        out_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            _merger_cmd() + ["convert", str(pair_dir), "--output", str(out_dir), "--include-probable"],
            capture_output=True, text=True,
        )
        result["ok"] = proc.returncode == 0
        if not result["ok"]:
            result["error"] = (proc.stderr or proc.stdout).strip()[-400:]

    produced = out_dir / (jpg.stem + ".jpg")
    if produced.exists():
        result["output"] = str(produced)
        result["output_mb"] = round(produced.stat().st_size / 1_000_000, 2)
    return result


def cmd_scan(args) -> int:
    pairs = find_pairs(Path(args.input))
    if not pairs:
        print("no same-stem jpg+mp4 pairs found")
        return 1
    print(f"{'image':<34} {'clip':<34} {'dur':>6}  {'pix_fmt':<10} range")
    for jpg, clip in pairs:
        try:
            info = tc.probe(clip)
            print(f"{jpg.name:<34} {clip.name:<34} {info.duration:6.2f}s {info.pix_fmt:<10} {info.color_range or '-'}")
        except Exception as exc:  # noqa: BLE001
            print(f"{jpg.name:<34} {clip.name:<34}  probe failed: {exc}")
    return 0


def cmd_convert(args) -> int:
    pairs = find_pairs(Path(args.input))
    if not pairs:
        print("no same-stem jpg+mp4 pairs found")
        return 1
    out_dir = Path(args.output)
    ok = 0
    for jpg, clip in pairs:
        result = convert_one(jpg, clip, out_dir, args.transcode, args.dry_run)
        status = "OK" if result["ok"] else "FAIL"
        extra = ""
        if result.get("transcoded"):
            extra = " [transcoded]"
        if result.get("skipped"):
            extra = f" [skipped: {result['skipped']}]"
        if result.get("output_mb"):
            extra += f" -> {result['output_mb']} MB"
        if result.get("error"):
            extra += f"\n    {result['error']}"
        print(f"{status:<4} {jpg.name}{extra}")
        ok += bool(result["ok"])
    print(f"\n{ok}/{len(pairs)} converted")
    return 0 if ok == len(pairs) else 2


def cmd_serve(args) -> int:
    from .webhook import main as serve

    sys.argv = ["mpb-serve", "--host", args.host, "--port", str(args.port)]
    serve()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mpb", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="list dual-file live photo pairs")
    p_scan.add_argument("input")
    p_scan.set_defaults(func=cmd_scan)

    p_conv = sub.add_parser("convert", help="merge pairs into standalone Motion Photo JPEGs")
    p_conv.add_argument("input")
    p_conv.add_argument("-o", "--output", required=True)
    p_conv.add_argument("--transcode", action="store_true",
                        help="re-encode full-range clips so Windows hardware decoding shows them")
    p_conv.add_argument("--dry-run", action="store_true")
    p_conv.set_defaults(func=cmd_convert)

    p_serve = sub.add_parser("serve", help="run the Immich webhook receiver")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=9100)
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
