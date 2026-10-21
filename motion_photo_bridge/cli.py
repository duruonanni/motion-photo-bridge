"""Command line interface for motion-photo-bridge."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import exif
from . import pairing
from . import transcode as tc
from . import vivo

MAX_MOTION_SECONDS = 10.0


def find_pairs(root: Path, include_mov: bool = False):
    """Pair by vivo's private id, then by a normalised filename stem."""
    return pairing.find_pairs(root, include_mov=include_mov)



def _merger_cmd() -> list[str]:
    """Always use the bundled merger (no git dependency, no extra install)."""
    exe = shutil.which("android-motion-photo-merger")
    if exe and os.environ.get("MPB_USE_SYSTEM_MERGER"):
        return [exe]
    return [sys.executable, "-m", "motion_photo_bridge._vendor.vivo_motion_photo"]


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
        # A photo copied out of a gallery or synced by a cloud client often has a
        # clip with no creation_time at all.  The merger then refuses to pair it
        # and reports "缺少可比较的拍摄时间" — stamp the JPEG's own capture time
        # onto the clip (metadata only, no re-encode) before merging.
        photo_time = exif.read_datetime_utc(jpg)
        result["photo_time"] = photo_time
        staged_clip = clip
        if do_transcode and info.needs_range_fix:
            staged_clip = work / "transcoded.mp4"
            tc.transcode(clip, staged_clip, creation_time=photo_time)
            result["transcoded"] = True
        elif photo_time:
            staged_clip = work / "stamped.mp4"
            proc = subprocess.run([tc.FFMPEG, "-y", "-v", "error", "-i", str(clip), "-c", "copy",
                                   "-metadata", f"creation_time={photo_time}", str(staged_clip)],
                                  capture_output=True, text=True)
            if proc.returncode != 0 or not staged_clip.exists():
                staged_clip = clip
            else:
                result["stamped"] = True
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
    pairs, skipped = find_pairs(Path(args.input), include_mov=args.include_mov)
    if not pairs:
        print("no jpg + clip pairs found")
        for item in skipped[:10]:
            print(f"  skip {item.path.name}: {item.reason}")
        return 1
    print(f"{'image':<34} {'clip':<34} {'dur':>6}  {'pix_fmt':<10} range  via")
    for pair in pairs:
        try:
            info = tc.probe(pair.clip)
            print(f"{pair.image.name:<34} {pair.clip.name:<34} {info.duration:6.2f}s {info.pix_fmt:<10} "
                  f"{info.color_range or '-':<4} {pair.how}")
        except Exception as exc:  # noqa: BLE001
            print(f"{pair.image.name:<34} {pair.clip.name:<34}  probe failed: {exc}")
    if skipped and not args.quiet:
        print(f"\n{len(skipped)} file(s) skipped (junk, duplicates, unmatched)")
        for item in skipped[:8]:
            print(f"  - {item.path.name}: {item.reason}")
    return 0


def cmd_convert(args) -> int:
    pairs, skipped = find_pairs(Path(args.input), include_mov=args.include_mov)
    if not pairs:
        print("no jpg + clip pairs found")
        for item in skipped[:10]:
            print(f"  skip {item.path.name}: {item.reason}")
        return 1
    out_dir = Path(args.output)
    ok = skipped_existing = 0
    failures: list[tuple[str, str]] = []
    for pair in pairs:
        jpg, clip = pair.image, pair.clip
        relative = jpg.parent.relative_to(Path(args.input))
        target_dir = out_dir if args.flat else out_dir / relative
        if args.skip_existing and (target_dir / (jpg.stem + ".jpg")).exists():
            skipped_existing += 1
            continue
        result = convert_one(jpg, clip, target_dir, args.transcode, args.dry_run)
        ok += bool(result["ok"])
        if not result["ok"]:
            failures.append((jpg.name, result.get("error") or result.get("skipped") or "unknown"))
        if args.quiet and result["ok"]:
            continue
        status = "OK" if result["ok"] else "FAIL"
        extra = ""
        if result.get("transcoded"):
            extra += " [transcoded]"
        if result.get("stamped"):
            extra += " [time-stamped]"
        if result.get("skipped"):
            extra = f" [skipped: {result['skipped']}]"
        if result.get("output_mb"):
            extra += f" -> {result['output_mb']} MB"
        if result.get("error"):
            extra += f"\n    {result['error']}"
        print(f"{status:<4} {pair.image.name}{extra}")

    total = len(pairs)
    summary = f"\n{ok}/{total} converted"
    if skipped_existing:
        summary += f" · {skipped_existing} already done"
    if skipped:
        summary += f" · {len(skipped)} file(s) skipped"
    print(summary)
    if failures and not args.quiet:
        print("failures:")
        for name, why in failures[:10]:
            print(f"  ✗ {name}: {why}")
    return 0 if ok + skipped_existing == total else 2


def cmd_serve(args) -> int:
    from .webhook import main as serve

    sys.argv = ["mpb-serve", "--host", args.host, "--port", str(args.port)]
    serve()
    return 0


TOOL_COMMANDS = None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    global TOOL_COMMANDS
    if TOOL_COMMANDS is None:
        from . import auto_merge as _auto_merge
        from . import doctor as _doctor
        from . import immich_apply as _apply
        from . import immich_batch as _batch
        TOOL_COMMANDS = {
            "apply-pair": _apply.main,
            "apply-batch": _batch.main,
            "auto-merge": _auto_merge.main,
            "doctor": _doctor.main,
        }
    if argv and argv[0] in TOOL_COMMANDS:
        return TOOL_COMMANDS[argv[0]](argv[1:])

    parser = argparse.ArgumentParser(
        prog="mpb",
        description=__doc__,
        epilog="Immich subcommands (run with --help for options):\n"
               "  apply-pair    merge one pair already inside Immich\n"
               "  apply-batch   merge every pending pair (resumable)\n"
               "  auto-merge    link + merge on a timer (multi-owner, idempotent)\n"
               "  doctor        check python / ffmpeg / bundled merger / Immich",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="list dual-file live photo pairs")
    p_scan.add_argument("input")
    p_scan.add_argument("--include-mov", action="store_true", help="also pair .MOV clips (Apple-style)")
    p_scan.add_argument("--quiet", action="store_true", help="do not list skipped files")
    p_scan.set_defaults(func=cmd_scan)

    p_conv = sub.add_parser("convert", help="merge pairs into standalone Motion Photo JPEGs")
    p_conv.add_argument("input")
    p_conv.add_argument("-o", "--output", required=True)
    p_conv.add_argument("--transcode", action="store_true",
                        help="re-encode full-range clips so Windows hardware decoding shows them")
    p_conv.add_argument("--dry-run", action="store_true")
    p_conv.add_argument("--include-mov", action="store_true", help="also convert .MOV clips")
    p_conv.add_argument("--flat", action="store_true", help="write every output into one directory")
    p_conv.add_argument("--skip-existing", action="store_true", help="resume: skip outputs that already exist")
    p_conv.add_argument("--quiet", action="store_true", help="print only failures and the summary")
    p_conv.set_defaults(func=cmd_convert)

    # Listed for discoverability; dispatched above to their own argparse parsers.
    for name, text in (("doctor", "check python / ffmpeg / bundled merger / Immich"),
                       ("apply-pair", "merge one pair already inside Immich"),
                       ("apply-batch", "merge every pending pair (resumable)"),
                       ("auto-merge", "link + merge on a timer (multi-owner, idempotent)")):
        sub.add_parser(name, help=text, add_help=False)

    p_serve = sub.add_parser("serve", help="run the Immich webhook receiver")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=9100)
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
