"""Command line interface for motion-photo-bridge."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import transcode as tc
from . import vivo

IMAGE_EXT = {".jpg", ".jpeg"}
VIDEO_EXT = {".mp4"}
MAX_MOTION_SECONDS = 10.0


def find_pairs(root: Path) -> list[tuple[Path, Path]]:
    """Return (image, clip) pairs.

    Pairing is authoritative on vivo's private ``livephoto`` id when both files
    carry one, and falls back to the filename stem otherwise.  Filenames are a
    weaker signal: the gallery app renames pairs when it re-saves them.
    """
    images: list[Path] = []
    videos: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext in IMAGE_EXT:
            images.append(path)
        elif ext in VIDEO_EXT:
            videos.append(path)

    pairs: list[tuple[Path, Path]] = []
    used_images: set[Path] = set()
    used_videos: set[Path] = set()

    # Pass 1: private id, the only link that survives a rename.
    by_id: dict[str, Path] = {}
    for image in images:
        ident = vivo.live_photo_id(image)
        if ident:
            by_id.setdefault(ident, image)
    for video in videos:
        ident = vivo.live_photo_id(video)
        if not ident:
            continue
        image = by_id.get(ident)
        if image is None or image in used_images:
            continue
        pairs.append((image, video))
        used_images.add(image)
        used_videos.add(video)

    # Pass 2: same stem, for files that carry no private metadata.
    images_by_stem: dict[str, Path] = {}
    for image in images:
        if image not in used_images:
            images_by_stem.setdefault(image.stem.lower(), image)
    for video in videos:
        if video in used_videos:
            continue
        image = images_by_stem.get(video.stem.lower())
        if image is None or image in used_images:
            continue
        pairs.append((image, video))
        used_images.add(image)
        used_videos.add(video)

    return sorted(pairs, key=lambda pair: pair[0].name.lower())



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
        # The merger finds clips by "same directory, same stem" and only looks
        # for .mp4.  A gallery rename or a .MOV source would break that, so the
        # clip is always staged under the image's stem with an .mp4 suffix.
        shutil.copy2(staged_clip, pair_dir / (jpg.stem + ".mp4"))

        out_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            _merger_cmd() + ["convert", str(pair_dir), "--output", str(out_dir),
                             "--include-probable", "--json"],
            capture_output=True, text=True,
        )
        # The merger exits 0 even when it declines to pair a file (Ambiguous /
        # Rejected), so the exit code alone cannot be trusted — read its report.
        try:
            for entry in json.loads(proc.stdout or "[]"):
                if str(entry.get("image_path", "")).endswith(jpg.name):
                    result["status"] = entry.get("status")
                    if entry.get("reason"):
                        result["reason"] = entry["reason"]
        except json.JSONDecodeError:
            pass
        result["ok"] = proc.returncode == 0
        if not result["ok"]:
            result["error"] = (proc.stderr or proc.stdout).strip()[-400:]

    produced = out_dir / (jpg.stem + ".jpg")
    if produced.exists():
        result["output"] = str(produced)
        result["output_mb"] = round(produced.stat().st_size / 1_000_000, 2)
    else:
        result["ok"] = False
        result["error"] = f"no output written: {result.get('reason') or result.get('status') or 'unknown'}"
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
