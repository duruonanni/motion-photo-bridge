from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any


class PairStatus(StrEnum):
    EXACT = "Exact"
    PROBABLE = "Probable"
    AMBIGUOUS = "Ambiguous"
    MISSING_PAIR = "MissingPair"
    ALREADY_MOTION_PHOTO = "AlreadyMotionPhoto"
    INVALID = "Invalid"
    CONVERTED = "Converted"
    SKIPPED = "Skipped"
    FAILED = "Failed"


class MergeStatus(StrEnum):
    """Outcome of producing or validating the merged Motion Photo."""

    NOT_STARTED = "NotStarted"
    CONVERTED = "Converted"
    EXISTING_VERIFIED = "ExistingVerified"
    SKIPPED = "Skipped"
    FAILED = "Failed"


class SourceAction(StrEnum):
    """Requested disposition of the source JPEG and MP4."""

    KEEP = "Keep"
    ARCHIVE = "Archive"
    DELETE = "Delete"


class SourceActionStatus(StrEnum):
    """Outcome of the source-file action, independent of the merge."""

    NOT_REQUESTED = "NotRequested"
    KEPT = "Kept"
    PENDING = "Pending"
    ARCHIVED = "Archived"
    DELETED = "Deleted"
    CANCELLED = "Cancelled"
    FAILED = "Failed"
    RECOVERY_REQUIRED = "RecoveryRequired"


@dataclass(slots=True)
class ImageMetadata:
    path: Path
    width: int | None = None
    height: int | None = None
    captured_at: str | None = None
    captured_timestamp: float | None = None
    live_photo_id: str | None = None
    trailing_size: int = 0
    already_motion_photo: bool = False
    valid: bool = True
    error: str | None = None


@dataclass(slots=True)
class VideoMetadata:
    path: Path
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    created_at: str | None = None
    created_timestamp: float | None = None
    live_photo_id: str | None = None
    image_time: int | None = None
    presentation_timestamp_us: int = -1
    codec: str | None = None
    valid: bool = True
    error: str | None = None


@dataclass(slots=True)
class PairRecord:
    status: PairStatus
    image_path: Path | None
    video_path: Path | None
    output_path: Path | None
    reason: str = ""
    selected: bool = False
    live_photo_id: str | None = None
    presentation_timestamp_us: int = -1
    warnings: list[str] = field(default_factory=list)
    original_status: PairStatus | None = None
    merge_status: MergeStatus = MergeStatus.NOT_STARTED
    source_action: SourceAction = SourceAction.KEEP
    source_action_status: SourceActionStatus = SourceActionStatus.NOT_REQUESTED
    merge_reason: str = ""
    source_action_reason: str = ""
    journal_id: str | None = None
    archive_locations: list[Path] = field(default_factory=list)

    @property
    def selectable(self) -> bool:
        return self.status in {PairStatus.EXACT, PairStatus.PROBABLE}

    @property
    def effective_merge_status(self) -> MergeStatus:
        """Return the split merge state, with legacy result compatibility."""

        if self.merge_status is not MergeStatus.NOT_STARTED:
            return self.merge_status
        return {
            PairStatus.CONVERTED: MergeStatus.CONVERTED,
            PairStatus.SKIPPED: MergeStatus.SKIPPED,
            PairStatus.FAILED: MergeStatus.FAILED,
        }.get(self.status, MergeStatus.NOT_STARTED)

    @property
    def has_failure(self) -> bool:
        """Whether either independently reported phase needs attention."""

        if self.effective_merge_status is MergeStatus.FAILED:
            return True
        return self.source_action_status in {
            SourceActionStatus.FAILED,
            SourceActionStatus.RECOVERY_REQUIRED,
        }

    @property
    def display_status(self) -> str:
        """Compact user-facing status that does not hide partial success.

        Pairing records retain their legacy status until conversion begins.
        Source-action failures are action-qualified so an archive failure is
        shown as ``Converted / ArchiveFailed`` instead of the misleading
        legacy value ``Failed``.
        """

        merge_status = self.effective_merge_status
        if merge_status is MergeStatus.NOT_STARTED:
            merge_text = self.status.value
        else:
            merge_text = merge_status.value

        source_status = self.source_action_status
        if source_status is SourceActionStatus.NOT_REQUESTED:
            return merge_text
        if source_status in {
            SourceActionStatus.PENDING,
            SourceActionStatus.CANCELLED,
            SourceActionStatus.FAILED,
            SourceActionStatus.RECOVERY_REQUIRED,
        }:
            source_text = f"{self.source_action.value}{source_status.value}"
        else:
            source_text = source_status.value
        return f"{merge_text} / {source_text}"

    def to_dict(self) -> dict[str, Any]:
        value = _serialize(asdict(self))
        value["has_failure"] = self.has_failure
        value["display_status"] = self.display_status
        return value


@dataclass(slots=True)
class VerificationResult:
    valid: bool
    path: Path
    motion_photo: bool
    video_length: int | None = None
    padding: int | None = None
    presentation_timestamp_us: int | None = None
    video_sha256: str | None = None
    source_image_preserved: bool | None = None
    source_video_matches: bool | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


def _serialize(value: Any) -> Any:
    """Recursively convert dataclass values into JSON-compatible objects."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    return value
