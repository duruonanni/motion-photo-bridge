from __future__ import annotations

import datetime as dt
import hashlib
import os
from collections import defaultdict
from pathlib import Path
from threading import Event
from typing import Callable, Iterable
from uuid import uuid4

from . import paths as pathio
from .metadata import MetadataError, inspect_jpeg_basic
from .models import (
    ImageMetadata,
    MergeStatus,
    PairRecord,
    PairStatus,
    SourceAction,
    SourceActionStatus,
    VerificationResult,
    VideoMetadata,
)
from .source_actions import (
    SourceActionStatus as ArchiveOutcomeStatus,
    archive_sources as run_archive_sources,
)


ProgressCallback = Callable[[int, int, PairRecord], None]
ARCHIVE_DIRECTORY_NAME = "已合并"
ArchiveEntry = tuple[str, Path, Path]


class WorkflowError(ValueError):
    pass


def _canonical(path: Path) -> str:
    return pathio.path_key(path)


def _is_within(path: Path, parent: Path) -> bool:
    child_text = _canonical(path)
    parent_text = _canonical(parent)
    try:
        return os.path.commonpath([child_text, parent_text]) == parent_text
    except ValueError:
        return False


def validate_roots(input_root: Path | str, output_root: Path | str | None) -> tuple[Path, Path | None]:
    source = pathio.resolve_path(Path(input_root).expanduser(), strict=False)
    if not pathio.is_dir(source):
        raise WorkflowError(f"输入目录不存在或不是目录：{source}")
    target = (
        None
        if output_root is None
        else pathio.resolve_path(Path(output_root).expanduser(), strict=False)
    )
    if target is not None and _canonical(source) == _canonical(target):
        raise WorkflowError("输出目录不能与输入目录相同")
    if target is not None and pathio.exists(target) and not pathio.is_dir(target):
        raise WorkflowError(f"输出路径已存在但不是目录：{target}")
    return source, target


def _iter_media_files(input_root: Path, output_root: Path | None, recursive: bool) -> Iterable[Path]:
    excluded_roots = [input_root / ARCHIVE_DIRECTORY_NAME]
    if output_root is not None and _is_within(output_root, input_root):
        excluded_roots.append(output_root)
    if not recursive:
        for path in pathio.iterdir(input_root):
            if pathio.is_file(path) and path.suffix.lower() in {".jpg", ".jpeg", ".mp4"}:
                yield path
        return

    for current_path, directory_names, file_names in pathio.walk(input_root, followlinks=False):
        if any(_is_within(current_path, excluded) for excluded in excluded_roots):
            directory_names.clear()
            continue
        directory_names[:] = [
            name
            for name in directory_names
            if not any(
                _is_within(current_path / name, excluded)
                for excluded in excluded_roots
            )
        ]
        for name in file_names:
            path = current_path / name
            if path.suffix.lower() in {".jpg", ".jpeg", ".mp4"}:
                yield path


def _to_timestamp(value: object) -> tuple[str | None, float | None]:
    if not isinstance(value, dt.datetime):
        return None, None
    normalized = value
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    return value.isoformat(), normalized.timestamp()


def _inspect_image(path: Path) -> ImageMetadata:
    try:
        basic = inspect_jpeg_basic(path)
        captured_at, captured_timestamp = _to_timestamp(basic.exif_datetime)
        already = False
        try:
            from .jpeg import (
                XmpReconciliationState,
                inspect_jpeg,
                plan_xmp_reconciliation,
                verify_motion_photo,
            )

            jpeg_info = inspect_jpeg(path)
            trailing_size = int(
                getattr(jpeg_info, "trailing_size", getattr(jpeg_info, "padding", 0))
            )
            reconciliation = plan_xmp_reconciliation(
                getattr(jpeg_info, "xmp_packet", None)
            )
            if reconciliation.state is XmpReconciliationState.CONFLICT:
                return ImageMetadata(
                    path=path,
                    width=basic.width,
                    height=basic.height,
                    captured_at=captured_at,
                    captured_timestamp=captured_timestamp,
                    live_photo_id=basic.live_photo_id,
                    trailing_size=trailing_size,
                    valid=False,
                    error=f"XMP cannot be reconciled safely: {reconciliation.reason}",
                )
            if reconciliation.state is XmpReconciliationState.ACTIVE:
                verification = verify_motion_photo(path)
                if not verification.valid:
                    return ImageMetadata(
                        path=path,
                        width=basic.width,
                        height=basic.height,
                        captured_at=captured_at,
                        captured_timestamp=captured_timestamp,
                        live_photo_id=basic.live_photo_id,
                        trailing_size=trailing_size,
                        valid=False,
                        error="Motion Photo metadata does not match the physical file: "
                        + "; ".join(verification.errors),
                    )
                already = True
        except (ImportError, AttributeError):
            trailing_size = pathio.stat(path).st_size - basic.eoi_end
        return ImageMetadata(
            path=path,
            width=basic.width,
            height=basic.height,
            captured_at=captured_at,
            captured_timestamp=captured_timestamp,
            live_photo_id=basic.live_photo_id,
            trailing_size=trailing_size,
            already_motion_photo=already,
        )
    except (OSError, MetadataError, ValueError) as error:
        return ImageMetadata(path=path, valid=False, error=str(error))


def _nested_get(value: object, *names: str, default=None):
    current = value
    for name in names:
        if current is None:
            return default
        current = getattr(current, name, None)
    return default if current is None else current


def _inspect_video(path: Path) -> VideoMetadata:
    try:
        from .mp4 import get_presentation_timestamp_us, probe_mp4

        info = probe_mp4(path)
        video = getattr(info, "video", None)
        vivo = getattr(info, "vivo", None)
        duration = getattr(info, "duration_seconds", None)
        if duration is None:
            duration = getattr(info, "duration", None)
        created = getattr(info, "creation_time", None)
        created_at, created_timestamp = _to_timestamp(created)
        timestamp_us = get_presentation_timestamp_us(info)
        # Pair photographs against the video's visual orientation, not its raw
        # encoded raster.  A rejected/non-standard tkhd transform deliberately
        # yields no display dimensions so fallback pairing fails closed instead
        # of silently trusting dimensions in the wrong coordinate space.
        display_width = _nested_get(video, "display_width")
        display_height = _nested_get(video, "display_height")
        return VideoMetadata(
            path=path,
            width=display_width,
            height=display_height,
            duration_seconds=float(duration) if duration is not None else None,
            created_at=created_at,
            created_timestamp=created_timestamp,
            live_photo_id=_nested_get(vivo, "live_photo_id"),
            image_time=_nested_get(vivo, "image_time"),
            presentation_timestamp_us=int(timestamp_us),
            codec=_nested_get(video, "codec"),
        )
    except (OSError, ValueError, TypeError, ImportError) as error:
        return VideoMetadata(path=path, valid=False, error=str(error))


def _output_path(image_path: Path, input_root: Path, output_root: Path | None) -> Path | None:
    if output_root is None:
        return None
    return output_root / image_path.relative_to(input_root)


def _safe_fallback(image: ImageMetadata, video: VideoMetadata) -> tuple[bool, str]:
    if not image.valid or not video.valid:
        return False, "媒体文件无效"
    duration = video.duration_seconds
    if duration is None or not 0.2 <= duration <= 10.0:
        return False, "视频时长不在 0.2–10 秒范围内"
    if not all((image.width, image.height, video.width, video.height)):
        return False, "缺少图片或视频尺寸"
    image_ratio = image.width / image.height  # type: ignore[operator]
    video_ratio = video.width / video.height  # type: ignore[operator]
    if abs(image_ratio - video_ratio) / image_ratio > 0.02:
        return False, "图片与视频宽高比差异超过 2%"
    if image.captured_timestamp is None or video.created_timestamp is None:
        return False, "缺少可比较的拍摄时间"
    difference = abs(image.captured_timestamp - video.created_timestamp)
    if difference > 15.0:
        return False, f"拍摄时间相差 {difference:.1f} 秒，超过 15 秒"
    return True, f"同名、媒体校验通过，拍摄时间相差 {difference:.1f} 秒"


def _make_record(
    status: PairStatus,
    image: ImageMetadata | None,
    video: VideoMetadata | None,
    input_root: Path,
    output_root: Path | None,
    reason: str,
) -> PairRecord:
    selected = status is PairStatus.EXACT
    live_id = None
    if image and video and image.live_photo_id == video.live_photo_id:
        live_id = image.live_photo_id
    timestamp = video.presentation_timestamp_us if video else -1
    warnings: list[str] = []
    if video and video.codec and video.codec.lower() not in {"avc1", "h264", "hev1", "hvc1", "hevc", "av01", "av1"}:
        warnings.append(f"视频编码 {video.codec} 不在 Motion Photo 1.0 推荐集合中")
    return PairRecord(
        status=status,
        image_path=image.path if image else None,
        video_path=video.path if video else None,
        output_path=_output_path(image.path, input_root, output_root) if image else None,
        reason=reason,
        selected=selected,
        live_photo_id=live_id,
        presentation_timestamp_us=timestamp,
        warnings=warnings,
    )


def _pair_directory(
    images: list[ImageMetadata],
    videos: list[VideoMetadata],
    input_root: Path,
    output_root: Path | None,
) -> list[PairRecord]:
    records: list[PairRecord] = []
    usable_images: list[ImageMetadata] = []
    usable_videos: list[VideoMetadata] = []

    for image in images:
        if not image.valid:
            records.append(_make_record(PairStatus.INVALID, image, None, input_root, output_root, image.error or "无效 JPG"))
        elif image.already_motion_photo:
            records.append(
                _make_record(
                    PairStatus.ALREADY_MOTION_PHOTO,
                    image,
                    None,
                    input_root,
                    output_root,
                    "图片已经包含 Motion Photo 元数据",
                )
            )
        else:
            usable_images.append(image)
    for video in videos:
        if not video.valid:
            records.append(_make_record(PairStatus.INVALID, None, video, input_root, output_root, video.error or "无效 MP4"))
        else:
            usable_videos.append(video)

    used_images: set[Path] = set()
    used_videos: set[Path] = set()
    images_by_id: dict[str, list[ImageMetadata]] = defaultdict(list)
    videos_by_id: dict[str, list[VideoMetadata]] = defaultdict(list)
    for image in usable_images:
        if image.live_photo_id:
            images_by_id[image.live_photo_id].append(image)
    for video in usable_videos:
        if video.live_photo_id:
            videos_by_id[video.live_photo_id].append(video)

    for live_id in sorted(images_by_id.keys() & videos_by_id.keys()):
        image_candidates = images_by_id[live_id]
        video_candidates = videos_by_id[live_id]
        if len(image_candidates) == 1 and len(video_candidates) == 1:
            image, video = image_candidates[0], video_candidates[0]
            records.append(
                _make_record(
                    PairStatus.EXACT,
                    image,
                    video,
                    input_root,
                    output_root,
                    "vivo Live Photo ID 精确匹配",
                )
            )
            used_images.add(image.path)
            used_videos.add(video.path)
        else:
            names = ", ".join(path.path.name for path in [*image_candidates, *video_candidates])
            for image in image_candidates:
                records.append(
                    _make_record(
                        PairStatus.AMBIGUOUS,
                        image,
                        None,
                        input_root,
                        output_root,
                        f"Live Photo ID {live_id} 存在重复候选：{names}",
                    )
                )
                used_images.add(image.path)
            used_videos.update(video.path for video in video_candidates)

    remaining_images = [item for item in usable_images if item.path not in used_images]
    remaining_videos = [item for item in usable_videos if item.path not in used_videos]
    image_stems: dict[str, list[ImageMetadata]] = defaultdict(list)
    video_stems: dict[str, list[VideoMetadata]] = defaultdict(list)
    for image in remaining_images:
        image_stems[image.path.stem.casefold()].append(image)
    for video in remaining_videos:
        video_stems[video.path.stem.casefold()].append(video)

    for stem in sorted(image_stems.keys() | video_stems.keys()):
        image_candidates = image_stems.get(stem, [])
        video_candidates = video_stems.get(stem, [])
        if len(image_candidates) == 1 and len(video_candidates) == 1:
            image, video = image_candidates[0], video_candidates[0]
            if image.live_photo_id and video.live_photo_id and image.live_photo_id != video.live_photo_id:
                status, reason = PairStatus.AMBIGUOUS, "同名文件的 vivo Live Photo ID 不一致"
            else:
                safe, reason = _safe_fallback(image, video)
                status = PairStatus.PROBABLE if safe else PairStatus.AMBIGUOUS
            records.append(_make_record(status, image, video, input_root, output_root, reason))
            continue
        if image_candidates and video_candidates:
            names = ", ".join(item.path.name for item in [*image_candidates, *video_candidates])
            for image in image_candidates:
                records.append(
                    _make_record(
                        PairStatus.AMBIGUOUS,
                        image,
                        None,
                        input_root,
                        output_root,
                        f"同名文件存在多个候选：{names}",
                    )
                )
            continue
        for image in image_candidates:
            records.append(
                _make_record(PairStatus.MISSING_PAIR, image, None, input_root, output_root, "缺少同目录 MP4")
            )
        for video in video_candidates:
            records.append(
                _make_record(PairStatus.MISSING_PAIR, None, video, input_root, output_root, "缺少同目录 JPG/JPEG")
            )
    return records


def scan_directory(
    input_root: Path | str,
    output_root: Path | str | None = None,
    recursive: bool = True,
) -> list[PairRecord]:
    source, target = validate_roots(input_root, output_root)
    by_directory: dict[Path, dict[str, list[Path]]] = defaultdict(lambda: {"images": [], "videos": []})
    for path in _iter_media_files(source, target, recursive):
        key = "videos" if path.suffix.lower() == ".mp4" else "images"
        by_directory[path.parent][key].append(path)
    records: list[PairRecord] = []
    for directory in sorted(by_directory, key=lambda item: str(item).casefold()):
        paths = by_directory[directory]
        images = [_inspect_image(path) for path in sorted(paths["images"], key=lambda item: item.name.casefold())]
        videos = [_inspect_video(path) for path in sorted(paths["videos"], key=lambda item: item.name.casefold())]
        records.extend(_pair_directory(images, videos, source, target))
    return records


def _delete_converted_sources(image_path: Path, video_path: Path) -> tuple[bool, str]:
    """Delete both sources after a verified output has been committed.

    Deletions cannot be made atomic across two independent filesystem entries.
    Attempt both paths even if one fails so the caller receives an exact account
    of which sources remain after a partial failure.
    """

    outcomes: list[str] = []
    failed = False
    # Delete the usually larger video first, matching the option's user-facing
    # wording while still reporting each path independently.
    for label, source_path in (("源 MP4", video_path), ("源 JPG", image_path)):
        try:
            pathio.unlink(source_path)
        except Exception as error:  # keep a deletion failure isolated to this record
            failed = True
            try:
                remains = pathio.exists(source_path)
            except Exception:
                remains = True
            state = "仍存在" if remains else "当前已不存在"
            outcomes.append(f"{label} 删除失败且{state}（{error}）")
        else:
            outcomes.append(f"{label} 已删除")

    details = "；".join(outcomes)
    if failed:
        return False, f"输出已成功生成并保留，但删除源文件未全部完成：{details}"
    return True, f"转换成功，源文件已删除：{details}"


def _resolve_archive_roots(input_root: Path | str | None) -> tuple[Path, Path, Path]:
    """Validate and resolve the fixed source-archive directory."""

    if input_root is None:
        raise WorkflowError("启用源文件归档时必须提供输入根目录")
    try:
        source_root = pathio.resolve_path(Path(input_root).expanduser(), strict=False)
        if not pathio.is_dir(source_root):
            raise WorkflowError(f"归档所需的输入目录不存在或不是目录：{source_root}")
        source_root = pathio.resolve_path(source_root, strict=True)
        archive_entry = source_root / ARCHIVE_DIRECTORY_NAME
        archive_exists = pathio.lexists(archive_entry)
        if archive_exists and not pathio.is_dir(archive_entry):
            raise WorkflowError(f"源文件归档路径已存在但不是目录：{archive_entry}")
        archive_root = pathio.resolve_path(archive_entry, strict=archive_exists)
    except WorkflowError:
        raise
    except (OSError, ValueError) as error:
        raise WorkflowError(f"无法安全解析源文件归档目录：{input_root}（{error}）") from error
    if _canonical(archive_root) == _canonical(source_root) or not _is_within(
        archive_root, source_root
    ):
        raise WorkflowError(f"源文件归档目录必须位于输入目录内：{archive_entry}")
    return source_root, archive_entry, archive_root


def _plan_source_archive(
    image_path: Path,
    video_path: Path,
    output_path: Path,
    source_root: Path,
    archive_entry: Path,
    archive_root: Path,
) -> tuple[ArchiveEntry, ArchiveEntry]:
    """Build a safe, no-overwrite archive plan without changing the filesystem."""

    if _is_within(output_path, archive_root):
        raise ValueError(f"输出路径不能位于源文件归档目录：{output_path}")

    entries: list[ArchiveEntry] = []
    for label, original in (("源 JPG", image_path), ("源 MP4", video_path)):
        if pathio.is_link(original):
            raise ValueError(f"{label} 是符号链接，拒绝归档：{original}")
        try:
            source = pathio.resolve_path(original, strict=True)
        except OSError as error:
            raise ValueError(f"{label} 不存在或无法解析：{original}（{error}）") from error
        if not pathio.is_file(source):
            raise ValueError(f"{label} 不存在或不是文件：{source}")
        if not _is_within(source, source_root):
            raise ValueError(f"{label} 不在输入目录内，拒绝归档：{source}")
        if _is_within(source, archive_root):
            raise ValueError(f"{label} 已位于归档目录内，拒绝再次归档：{source}")
        try:
            relative = source.relative_to(source_root)
        except ValueError as error:
            raise ValueError(f"{label} 无法映射到输入目录：{source}") from error
        target = archive_entry / relative
        if not _is_within(target, archive_root):
            raise ValueError(f"{label} 的归档目标解析后超出归档目录，拒绝写入：{target}")
        if pathio.lexists(target):
            raise FileExistsError(f"归档目标已存在，拒绝覆盖：{target}")
        if _canonical(target) == _canonical(output_path):
            raise ValueError(f"归档目标与输出路径冲突：{target}")
        entries.append((label, source, target))

    if _canonical(entries[0][2]) == _canonical(entries[1][2]):
        raise ValueError("JPG 与 MP4 映射到了同一个归档目标")
    return entries[0], entries[1]


def _create_archive_stage(source: Path, target: Path, archive_root: Path) -> Path:
    """Create a verified temporary hard link beside one archive target."""

    if not _is_within(target, archive_root):
        raise ValueError(f"归档目标解析后超出归档目录，拒绝写入：{target}")
    pathio.mkdir(target.parent, parents=True, exist_ok=True)
    if not _is_within(target, archive_root):
        raise ValueError(f"归档目标目录在创建期间发生变化，拒绝写入：{target}")
    for _ in range(16):
        stage = target.parent / f".vivo-archive-{uuid4().hex}.tmp"
        try:
            pathio.link(source, stage)
        except FileExistsError:
            continue
        except OSError as error:
            raise OSError(
                "无法创建安全归档硬链接（文件系统可能不支持或权限不足）："
                f"{source} → {target}（{error}）"
            ) from error
        try:
            if not pathio.samefile(source, stage):
                raise OSError(f"归档临时链接校验失败：{stage}")
        except BaseException:
            pathio.unlink(stage, missing_ok=True)
            raise
        return stage
    raise FileExistsError(f"无法创建唯一的归档临时路径：{target.parent}")


def _cleanup_archive_links(paths: Iterable[Path]) -> list[str]:
    errors: list[str] = []
    for path in paths:
        try:
            pathio.unlink(path, missing_ok=True)
        except OSError as error:
            errors.append(f"{path}（{error}）")
    return errors


def _rollback_committed_archive_links(
    links: Iterable[tuple[Path, Path]],
) -> list[str]:
    """Remove only final links that still refer to their expected source."""

    errors: list[str] = []
    for target, expected_source in links:
        try:
            if not pathio.lexists(target):
                continue
            if not pathio.lexists(expected_source) or not pathio.samefile(
                target, expected_source
            ):
                errors.append(f"{target}（身份已变化，为避免误删已保留）")
                continue
            pathio.unlink(target)
        except OSError as error:
            errors.append(f"{target}（{error}）")
    return errors


def _archive_converted_sources(
    plan: tuple[ArchiveEntry, ArchiveEntry],
    cancel_event: Event | None,
    archive_root: Path,
) -> tuple[bool | None, str]:
    """Archive both sources transactionally, then remove their original names.

    Both archive entries are first staged and committed as verified hard links.
    Any failure before both commits rolls back every link created by this call,
    leaving both source paths untouched.  Once both durable archive names exist,
    source deletions are attempted independently and the archives are retained
    even if only one source can be removed.
    """

    stages: list[tuple[ArchiveEntry, Path]] = []
    committed: list[tuple[Path, Path]] = []

    def cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    try:
        for entry in plan:
            if cancelled():
                cleanup_errors = _cleanup_archive_links(
                    [stage for _, stage in reversed(stages)]
                )
                if cleanup_errors:
                    return False, "用户取消归档，但临时链接清理失败：" + "；".join(cleanup_errors)
                return None, "转换成功；用户已取消，源文件未归档"
            label, source, target = entry
            stages.append((entry, _create_archive_stage(source, target, archive_root)))

        if cancelled():
            cleanup_errors = _cleanup_archive_links(
                [stage for _, stage in reversed(stages)]
            )
            if cleanup_errors:
                return False, "用户取消归档，但临时链接清理失败：" + "；".join(cleanup_errors)
            return None, "转换成功；用户已取消，源文件未归档"

        for entry, stage in stages:
            label, source, target = entry
            if not _is_within(target, archive_root):
                raise ValueError(f"归档目标在提交前超出归档目录，拒绝写入：{target}")
            if pathio.lexists(target):
                raise FileExistsError(f"归档目标已出现，拒绝覆盖：{target}")
            try:
                pathio.link(stage, target)
            except FileExistsError as error:
                raise FileExistsError(f"归档目标已出现，拒绝覆盖：{target}") from error
            committed.append((target, source))
            if not pathio.samefile(stage, target):
                raise OSError(f"归档目标校验失败：{target}")
            if cancelled():
                cleanup_errors = _rollback_committed_archive_links(
                    reversed(committed)
                )
                cleanup_errors.extend(
                    _cleanup_archive_links(
                        [item for _, item in reversed(stages)]
                    )
                )
                if cleanup_errors:
                    return False, "用户取消归档，但归档链接回滚失败：" + "；".join(cleanup_errors)
                return None, "转换成功；用户已取消，源文件未归档"
    except (OSError, ValueError) as error:
        cleanup_errors = _rollback_committed_archive_links(reversed(committed))
        cleanup_errors.extend(
            _cleanup_archive_links([stage for _, stage in reversed(stages)])
        )
        reason = f"输出已成功生成并保留，但源文件归档失败，源文件未移动：{error}"
        if cleanup_errors:
            reason += "；归档链接回滚失败：" + "；".join(cleanup_errors)
        return False, reason

    stage_cleanup_errors = _cleanup_archive_links(
        [stage for _, stage in reversed(stages)]
    )
    if stage_cleanup_errors:
        rollback_errors = _rollback_committed_archive_links(reversed(committed))
        details = "；".join(stage_cleanup_errors)
        if rollback_errors:
            details += "；最终归档链接回滚失败：" + "；".join(rollback_errors)
        return False, "归档临时链接清理失败，源文件未移动：" + details

    if cancelled():
        rollback_errors = _rollback_committed_archive_links(reversed(committed))
        if rollback_errors:
            return False, "用户取消归档，但最终归档链接回滚失败：" + "；".join(
                rollback_errors
            )
        return None, "转换成功；用户已取消，源文件未归档"

    outcomes: list[str] = []
    failed = False
    # Do not observe cancellation between these unlink calls: once source
    # cleanup begins, stopping halfway would manufacture a partial result.
    for label, source, target in (plan[1], plan[0]):
        try:
            if not pathio.lexists(source):
                raise FileNotFoundError(f"源路径当前已不存在：{source}")
            if not pathio.samefile(source, target):
                raise OSError(f"源路径在归档期间发生变化，拒绝删除当前文件：{source}")
            pathio.unlink(source)
        except OSError as error:
            failed = True
            try:
                remains = pathio.lexists(source)
            except OSError:
                remains = True
            state = "仍存在" if remains else "当前已不存在"
            outcomes.append(f"{label} 移除失败且{state}（{error}）")
        else:
            outcomes.append(f"{label} 已移入归档")

    locations = "；".join(f"{label} → {target}" for label, _, target in plan)
    details = "；".join(outcomes)
    if failed:
        return (
            False,
            f"输出和归档副本已保留，但移除源文件未全部完成：{details}；归档位置：{locations}",
        )
    return True, f"转换成功，源文件已归档：{locations}"


def _requested_source_action(
    *, delete_sources: bool, archive_sources: bool
) -> SourceAction:
    if archive_sources:
        return SourceAction.ARCHIVE
    if delete_sources:
        return SourceAction.DELETE
    return SourceAction.KEEP


def _begin_result_record(record: PairRecord, action: SourceAction) -> None:
    record.source_action = action
    record.source_action_status = (
        SourceActionStatus.KEPT
        if action is SourceAction.KEEP
        else SourceActionStatus.PENDING
    )
    record.source_action_reason = (
        "按设置保留源文件" if action is SourceAction.KEEP else "等待合并结果验证"
    )
    record.merge_status = MergeStatus.NOT_STARTED
    record.merge_reason = ""
    record.journal_id = None
    record.archive_locations.clear()


def _sync_legacy_reason(record: PairRecord) -> None:
    parts = [part for part in (record.merge_reason, record.source_action_reason) if part]
    record.reason = "；".join(parts)


def _source_action_not_run(record: PairRecord, reason: str) -> None:
    if record.source_action is SourceAction.KEEP:
        record.source_action_status = SourceActionStatus.KEPT
        record.source_action_reason = "按设置保留源文件"
    else:
        record.source_action_status = SourceActionStatus.FAILED
        record.source_action_reason = reason


def _cancel_source_action(record: PairRecord) -> None:
    if record.source_action is SourceAction.KEEP:
        record.source_action_status = SourceActionStatus.KEPT
        record.source_action_reason = "按设置保留源文件"
    else:
        record.source_action_status = SourceActionStatus.CANCELLED
        record.source_action_reason = (
            "用户取消，源文件未归档"
            if record.source_action is SourceAction.ARCHIVE
            else "用户取消，源文件未删除"
        )


def _apply_verified_source_action(
    record: PairRecord,
    *,
    archive_roots: tuple[Path, Path, Path] | None,
    cancel_event: Event | None,
) -> None:
    """Run only the source action after a new or existing output is verified."""

    if record.source_action is SourceAction.KEEP:
        record.source_action_status = SourceActionStatus.KEPT
        record.source_action_reason = "按设置保留源文件"
        return
    if cancel_event is not None and cancel_event.is_set():
        _cancel_source_action(record)
        return
    assert record.image_path is not None
    assert record.video_path is not None
    assert record.output_path is not None

    if record.source_action is SourceAction.DELETE:
        deleted, reason = _delete_converted_sources(record.image_path, record.video_path)
        record.source_action_status = (
            SourceActionStatus.DELETED if deleted else SourceActionStatus.FAILED
        )
        record.source_action_reason = reason
        return

    if archive_roots is None:
        record.source_action_status = SourceActionStatus.FAILED
        record.source_action_reason = "归档根目录未通过配置校验"
        return
    try:
        archive_plan = _plan_source_archive(
            record.image_path,
            record.video_path,
            record.output_path,
            *archive_roots,
        )
    except (OSError, ValueError) as error:
        record.source_action_status = SourceActionStatus.FAILED
        record.source_action_reason = f"源文件归档预检失败：{error}"
        return

    outcome = run_archive_sources(
        archive_plan,
        input_root=archive_roots[0],
        archive_root=archive_roots[2],
        output_verified=True,
        cancel_event=cancel_event,
    )
    record.journal_id = outcome.transaction_id
    record.archive_locations = list(outcome.archive_paths)
    record.source_action_reason = outcome.reason
    record.source_action_status = {
        ArchiveOutcomeStatus.COMPLETED: SourceActionStatus.ARCHIVED,
        ArchiveOutcomeStatus.CANCELED: SourceActionStatus.CANCELLED,
        ArchiveOutcomeStatus.FAILED: SourceActionStatus.FAILED,
        ArchiveOutcomeStatus.RECOVERY_REQUIRED: SourceActionStatus.RECOVERY_REQUIRED,
    }[outcome.status]


def _verify_existing_output_for_source_action(
    record: PairRecord,
    verify_motion_photo: Callable[..., object],
) -> tuple[bool, str]:
    """Prove an existing output belongs to this pair before touching sources."""

    assert record.image_path is not None
    assert record.video_path is not None
    assert record.output_path is not None
    try:
        source_mtime_ns = pathio.stat(record.image_path).st_mtime_ns
        verification = verify_motion_photo(
            record.output_path,
            source_image=record.image_path,
            source_video=record.video_path,
        )
        valid = bool(getattr(verification, "valid", False))
        image_matches = getattr(verification, "source_image_matches", None) is True
        video_matches = getattr(verification, "source_video_matches", None) is True
        if not (valid and image_matches and video_matches):
            errors = getattr(verification, "errors", ()) or ()
            detail = "；".join(str(error) for error in errors) or "源 JPG/MP4 字节身份不一致"
            return False, f"已有输出未通过源文件身份校验：{detail}"
        output_mtime_ns = pathio.stat(record.output_path).st_mtime_ns
        if output_mtime_ns != source_mtime_ns:
            return (
                False,
                "已有输出修改时间与源 JPG 不一致，拒绝处理源文件"
                f"（输出 {output_mtime_ns}，源 JPG {source_mtime_ns}）",
            )
        return True, "已有输出已验证为当前 JPG/MP4 的 Motion Photo，修改时间一致"
    except (OSError, ValueError) as error:
        return False, f"无法安全验证已有输出：{error}"


def convert_records(
    records: list[PairRecord],
    overwrite: bool = False,
    include_probable: bool = False,
    progress_callback: ProgressCallback | None = None,
    cancel_event: Event | None = None,
    delete_sources: bool = False,
    archive_sources: bool = False,
    input_root: Path | str | None = None,
) -> list[PairRecord]:
    if delete_sources and archive_sources:
        raise WorkflowError("删除源文件与归档源文件不能同时启用")
    archive_roots = _resolve_archive_roots(input_root) if archive_sources else None
    requested_action = _requested_source_action(
        delete_sources=delete_sources,
        archive_sources=archive_sources,
    )

    try:
        from .jpeg import XmpError, verify_motion_photo, write_motion_photo
    except ImportError as error:  # pragma: no cover - installation damage
        raise WorkflowError(f"JPEG 转换模块不可用：{error}") from error

    total = len(records)
    for index, record in enumerate(records, start=1):
        if cancel_event is not None and cancel_event.is_set():
            if record.selectable:
                record.original_status = record.status
                record.status = PairStatus.SKIPPED
                _begin_result_record(record, requested_action)
                record.merge_status = MergeStatus.SKIPPED
                record.merge_reason = "用户取消，未开始合并"
                _cancel_source_action(record)
                _sync_legacy_reason(record)
            if progress_callback:
                progress_callback(index, total, record)
            continue
        eligible = record.status is PairStatus.EXACT and record.selected
        eligible = eligible or (
            record.status is PairStatus.PROBABLE and (record.selected or include_probable)
        )
        if not eligible:
            if progress_callback:
                progress_callback(index, total, record)
            continue
        record.original_status = record.status
        _begin_result_record(record, requested_action)
        if record.image_path is None or record.video_path is None or record.output_path is None:
            record.status = PairStatus.FAILED
            record.merge_status = MergeStatus.FAILED
            record.merge_reason = "转换记录缺少输入或输出路径"
            _source_action_not_run(record, "合并前置条件失败，源文件未处理")
        elif _canonical(record.output_path) in {_canonical(record.image_path), _canonical(record.video_path)}:
            record.status = PairStatus.FAILED
            record.merge_status = MergeStatus.FAILED
            record.merge_reason = "拒绝覆盖输入源文件"
            _source_action_not_run(record, "输出路径不安全，源文件未处理")
        elif pathio.exists(record.output_path) and not overwrite:
            if requested_action is SourceAction.KEEP:
                record.status = PairStatus.SKIPPED
                record.merge_status = MergeStatus.SKIPPED
                record.merge_reason = "输出文件已存在"
            else:
                verified, reason = _verify_existing_output_for_source_action(
                    record, verify_motion_photo
                )
                if verified:
                    record.status = PairStatus.SKIPPED
                    record.merge_status = MergeStatus.EXISTING_VERIFIED
                    record.merge_reason = reason
                    _apply_verified_source_action(
                        record,
                        archive_roots=archive_roots,
                        cancel_event=cancel_event,
                    )
                else:
                    # Preserve the legacy safe-skip spelling while the split
                    # merge result records that this output could not be
                    # verified for a destructive follow-up action.
                    record.status = PairStatus.SKIPPED
                    record.merge_status = MergeStatus.FAILED
                    record.merge_reason = reason
                    _source_action_not_run(
                        record,
                        "已有输出未能证明属于当前源文件，源文件未处理",
                    )
        else:
            try:
                # Path/target preflight intentionally precedes writing a new
                # output.  Runtime archive failures after this point remain a
                # source-action result and never overwrite merge success.
                if requested_action is SourceAction.ARCHIVE:
                    assert archive_roots is not None
                    _plan_source_archive(
                        record.image_path,
                        record.video_path,
                        record.output_path,
                        *archive_roots,
                    )
                pathio.mkdir(record.output_path.parent, parents=True, exist_ok=True)
                write_motion_photo(
                    record.image_path,
                    record.video_path,
                    record.output_path,
                    record.presentation_timestamp_us,
                    overwrite=overwrite,
                )
                record.status = PairStatus.CONVERTED
                record.merge_status = MergeStatus.CONVERTED
                record.merge_reason = "转换成功并通过完整校验"
                _apply_verified_source_action(
                    record,
                    archive_roots=archive_roots,
                    cancel_event=cancel_event,
                )
            except XmpError as error:
                # Existing/damaged/conflicting XMP is a safe, expected skip:
                # never discard metadata or guess how to rewrite it.
                record.status = PairStatus.SKIPPED
                record.merge_status = MergeStatus.SKIPPED
                record.merge_reason = f"XMP 无法安全合并，已跳过：{error}"
                _source_action_not_run(record, "未生成已验证输出，源文件未处理")
            except (OSError, ValueError) as error:
                record.status = PairStatus.FAILED
                record.merge_status = MergeStatus.FAILED
                record.merge_reason = str(error)
                _source_action_not_run(record, "合并失败，源文件未处理")
        _sync_legacy_reason(record)
        if progress_callback:
            progress_callback(index, total, record)
    return records


def _sha256(path: Path, offset: int = 0, length: int | None = None) -> str:
    digest = hashlib.sha256()
    remaining = length
    with pathio.open_path(path, "rb") as stream:
        stream.seek(offset)
        while remaining is None or remaining > 0:
            size = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
            chunk = stream.read(size)
            if not chunk:
                break
            digest.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return digest.hexdigest()


def verify_file(
    path: Path | str,
    source_image: Path | str | None = None,
    source_video: Path | str | None = None,
) -> VerificationResult:
    target = pathio.public_path(path)
    result = VerificationResult(valid=False, path=target, motion_photo=False)
    try:
        from .jpeg import verify_motion_photo

        info = verify_motion_photo(
            target,
            source_image=pathio.public_path(source_image) if source_image else None,
            source_video=pathio.public_path(source_video) if source_video else None,
        )
        result.valid = bool(getattr(info, "valid", True))
        result.motion_photo = bool(getattr(info, "motion_photo", getattr(info, "has_motion_photo", True)))
        result.video_length = getattr(info, "video_length", None)
        result.padding = getattr(
            info,
            "padding",
            getattr(info, "primary_padding", getattr(info, "trailing_size", None)),
        )
        result.presentation_timestamp_us = getattr(info, "presentation_timestamp_us", None)
        result.video_sha256 = getattr(info, "video_sha256", None)
        result.source_image_preserved = getattr(
            info,
            "source_image_preserved",
            getattr(info, "source_image_matches", None),
        )
        result.source_video_matches = getattr(info, "source_video_matches", None)
        result.errors.extend(getattr(info, "errors", []) or [])
        result.warnings.extend(getattr(info, "warnings", []) or [])
        if source_video and result.video_length:
            source_path = pathio.public_path(source_video)
            embedded_hash = _sha256(
                target,
                pathio.stat(target).st_size - result.video_length,
                result.video_length,
            )
            result.video_sha256 = embedded_hash
            result.source_video_matches = embedded_hash == _sha256(source_path)
            if not result.source_video_matches:
                result.valid = False
                result.errors.append("输出末尾视频与源 MP4 不一致")
        return result
    except (OSError, ValueError) as error:
        result.errors.append(str(error))
        return result
