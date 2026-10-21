from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict
from enum import Enum
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .models import PairRecord
from .source_actions import (
    RecoveryResult,
    RecoveryStatus,
    recover_archive_transactions,
)
from .workflow import WorkflowError, convert_records, scan_directory, verify_file


def _configure_utf8_streams() -> None:
    """Keep Chinese CLI output stable in consoles, pipes, and log files."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def _enum_text(value: object) -> str:
    enum_value = getattr(value, "value", value)
    return "" if enum_value is None else str(enum_value)


def _merge_status_text(record: object) -> str:
    value = _enum_text(getattr(record, "merge_status", ""))
    if value and value.replace("_", "").lower() != "notstarted":
        return value
    return _enum_text(getattr(record, "status", "")) or value


def _source_action_status_text(record: object) -> str:
    status = _enum_text(getattr(record, "source_action_status", ""))
    normalized = status.replace("_", "").lower()
    if not normalized or normalized == "notrequested":
        return ""
    action = _enum_text(getattr(record, "source_action", ""))
    if normalized in {"pending", "cancelled", "failed", "recoveryrequired"}:
        return f"{action}{status}" if action else status
    return status


def _display_status(record: object) -> str:
    value = getattr(record, "display_status", "")
    if value:
        return str(value)
    merge = _merge_status_text(record)
    source = _source_action_status_text(record)
    return " / ".join(part for part in (merge, source) if part)


def _record_reason(record: object) -> str:
    merge_reason = str(getattr(record, "merge_reason", "") or "")
    source_reason = str(getattr(record, "source_action_reason", "") or "")
    reasons = [item for item in (merge_reason, source_reason) if item]
    if reasons:
        return "；".join(dict.fromkeys(reasons))
    return str(getattr(record, "reason", "") or "")


def _has_failure(record: object) -> bool:
    value = getattr(record, "has_failure", None)
    if value is not None:
        return bool(value)
    merge = _merge_status_text(record).replace("_", "").lower()
    source = _enum_text(
        getattr(record, "source_action_status", "")
    ).replace("_", "").lower()
    return merge == "failed" or source in {"failed", "recoveryrequired"}


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _recovery_dict(result: RecoveryResult) -> dict[str, Any]:
    return _json_value(asdict(result))


def _recovery_failed(results: list[RecoveryResult]) -> bool:
    return any(
        result.status in {RecoveryStatus.RECOVERY_REQUIRED, RecoveryStatus.INVALID}
        for result in results
    )


def _print_recovery_results(
    results: list[RecoveryResult], *, stream: TextIO = sys.stdout
) -> None:
    if not results:
        print("没有发现未完成的归档事务。", file=stream)
        return
    print(f"{'事务 ID':<34} {'状态':<18} {'后端':<10} 原因", file=stream)
    print("-" * 110, file=stream)
    for result in results:
        print(
            f"{result.transaction_id:<34.34} {result.status.value:<18.18} "
            f"{(result.backend or '-'):<10.10} {result.reason}",
            file=stream,
        )


def _print_scan_table(records: list[PairRecord]) -> None:
    if not records:
        print("未发现 JPG/JPEG 或 MP4 文件。")
        return
    print(f"{'合并状态':<20} {'源文件处理':<22} {'图片':<36} {'视频':<36} 原因")
    print("-" * 146)
    for record in records:
        image = record.image_path.name if record.image_path else "-"
        video = record.video_path.name if record.video_path else "-"
        merge = _merge_status_text(record) or "-"
        source = _source_action_status_text(record) or "-"
        print(
            f"{merge:<20.20} {source:<22.22} {image:<36.36} {video:<36.36} "
            f"{_record_reason(record)}"
        )
    counts = Counter(_display_status(record) or "Unknown" for record in records)
    print("\n汇总：" + "，".join(f"{key}={value}" for key, value in sorted(counts.items())))


def _progress(index: int, total: int, record: PairRecord) -> None:
    image = record.image_path.name if record.image_path else "-"
    print(
        f"[{index}/{total}] {_display_status(record)}: {image} - {_record_reason(record)}",
        file=sys.stderr,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="android-motion-photo-merger",
        description=(
            "将同名 JPG/JPEG+MP4 合并为 Immich 可识别的 Android Motion Photo；"
            "针对 vivo/iQOO 私有元数据提供增强支持"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="扫描并展示配对结果，不写入文件")
    scan.add_argument("input", type=Path, help="输入目录")
    scan.add_argument("--output", type=Path, help="仅用于预览目标路径")
    scan.add_argument("--no-recursive", action="store_true", help="仅扫描当前目录")
    scan.add_argument("--json", action="store_true", help="输出机器可读 JSON")

    convert = subparsers.add_parser("convert", help="批量转换已配对的动态照片")
    convert.add_argument("input", type=Path, help="输入目录")
    convert.add_argument("--output", type=Path, required=True, help="独立输出目录")
    convert.add_argument("--no-recursive", action="store_true", help="仅扫描当前目录")
    convert.add_argument(
        "--include-probable",
        action="store_true",
        help="同时转换安全回退得到的 Probable 配对",
    )
    convert.add_argument("--overwrite", action="store_true", help="原子覆盖已有输出")
    source_handling = convert.add_mutually_exclusive_group()
    source_handling.add_argument(
        "--archive-sources",
        action="store_true",
        help="转换并校验成功后，将源 JPG 和 MP4 移到输入目录下的“已合并”（默认关闭）",
    )
    source_handling.add_argument(
        "--delete-sources",
        action="store_true",
        help="转换并校验成功后永久删除源 JPG 和 MP4（默认关闭，不可恢复）",
    )
    convert.add_argument("--json", action="store_true", help="输出机器可读 JSON 结果")
    convert.add_argument("--quiet", action="store_true", help="不显示逐项进度")

    recover = subparsers.add_parser(
        "recover",
        help="恢复异常中断的源文件归档事务",
    )
    recover.add_argument("input", type=Path, help="原输入目录")
    recover.add_argument("--json", action="store_true", help="输出机器可读 JSON")

    verify = subparsers.add_parser("verify", help="校验一个 Motion Photo 文件")
    verify.add_argument("file", type=Path, help="待校验的 JPEG")
    verify.add_argument("--source-image", type=Path, help="可选源 JPG，用于保留性校验")
    verify.add_argument("--source-video", type=Path, help="可选源 MP4，用于逐字节校验")
    verify.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_streams()
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            records = scan_directory(args.input, args.output, recursive=not args.no_recursive)
            if args.json:
                print(json.dumps([record.to_dict() for record in records], ensure_ascii=False, indent=2))
            else:
                _print_scan_table(records)
            return 0

        if args.command == "convert":
            recovery_results: list[RecoveryResult] = []
            if args.archive_sources:
                # Archive mode is an explicit authorization to finish or roll
                # back an earlier interrupted archive before scanning.  This
                # restores split JPG/MP4 pairs to a consistent location first.
                recovery_results = recover_archive_transactions(args.input)
                if _recovery_failed(recovery_results):
                    if args.json:
                        print(
                            json.dumps(
                                {
                                    "recovery": [
                                        _recovery_dict(item)
                                        for item in recovery_results
                                    ],
                                    "results": [],
                                },
                                ensure_ascii=False,
                                indent=2,
                            )
                        )
                    else:
                        _print_recovery_results(recovery_results, stream=sys.stderr)
                    return 1
                if recovery_results and not args.quiet and not args.json:
                    print("已先处理上次未完成的归档事务：", file=sys.stderr)
                    _print_recovery_results(recovery_results, stream=sys.stderr)
            records = scan_directory(args.input, args.output, recursive=not args.no_recursive)
            results = convert_records(
                records,
                overwrite=args.overwrite,
                include_probable=args.include_probable,
                archive_sources=args.archive_sources,
                input_root=args.input,
                delete_sources=args.delete_sources,
                progress_callback=None if args.quiet or args.json else _progress,
            )
            if args.json:
                print(json.dumps([record.to_dict() for record in results], ensure_ascii=False, indent=2))
            else:
                _print_scan_table(results)
            return 1 if any(_has_failure(record) for record in results) else 0

        if args.command == "recover":
            results = recover_archive_transactions(args.input)
            if args.json:
                print(
                    json.dumps(
                        [_recovery_dict(result) for result in results],
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                _print_recovery_results(results)
            return 1 if _recovery_failed(results) else 0

        if args.command == "verify":
            result = verify_file(args.file, args.source_image, args.source_video)
            if args.json:
                print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            else:
                print("校验通过" if result.valid else "校验失败")
                print(f"文件：{result.path}")
                print(f"Motion Photo：{'是' if result.motion_photo else '否'}")
                if result.video_length is not None:
                    print(f"视频长度：{result.video_length} 字节")
                if result.padding is not None:
                    print(f"Primary Padding：{result.padding} 字节")
                if result.presentation_timestamp_us is not None:
                    print(f"展示时间：{result.presentation_timestamp_us} µs")
                for warning in result.warnings:
                    print(f"警告：{warning}")
                for error in result.errors:
                    print(f"错误：{error}", file=sys.stderr)
            return 0 if result.valid else 1
    except (WorkflowError, OSError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
