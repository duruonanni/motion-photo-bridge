"""Crash-recoverable actions applied to converted source photo pairs.

The conversion output and the source-file action are deliberately separate.
This module only archives an already verified JPG/MP4 pair.  A durable journal
is written before a source name can be removed, so an interrupted operation can
be inspected or recovered on the next run.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from threading import Event
from typing import Sequence
from uuid import uuid4

from . import paths as pathio


ARCHIVE_DIRECTORY_NAME = "已合并"
TRANSACTION_DIRECTORY_NAME = ".vivo-motion-photo-transactions"
MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA_VERSION = 1
_TRANSACTION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MANIFEST_TEMP_RE = re.compile(r"^\.manifest-[^/\\]+\.tmp$")
_PHASES = {
    "planned",
    "staging",
    "prepared",
    "committing",
    "committed",
    "cleaning",
    "rolling_back",
    "completed",
    "rolled_back",
    "recovery_required",
}
_BACKENDS = {"auto", "hardlink", "rename"}


class SourceActionStatus(str, Enum):
    COMPLETED = "Completed"
    FAILED = "Failed"
    CANCELED = "Canceled"
    RECOVERY_REQUIRED = "RecoveryRequired"


class RecoveryStatus(str, Enum):
    WOULD_COMPLETE = "WouldComplete"
    WOULD_ROLL_BACK = "WouldRollBack"
    COMPLETED = "Completed"
    ROLLED_BACK = "RolledBack"
    RECOVERY_REQUIRED = "RecoveryRequired"
    INVALID = "Invalid"


@dataclass(frozen=True)
class ArchivePlanEntry:
    label: str
    source: Path
    target: Path


@dataclass(frozen=True)
class SourceActionOutcome:
    status: SourceActionStatus
    reason: str
    transaction_id: str | None = None
    backend: str | None = None
    archive_paths: tuple[Path, ...] = ()
    journal_path: Path | None = None


@dataclass(frozen=True)
class RecoveryResult:
    transaction_id: str
    status: RecoveryStatus
    reason: str
    journal_path: Path
    backend: str | None = None
    archive_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class _Fingerprint:
    sha256: str
    size: int
    mtime_ns: int

    @classmethod
    def from_json(cls, value: object) -> "_Fingerprint":
        if not isinstance(value, dict):
            raise ValueError("fingerprint must be an object")
        if set(value) != {"sha256", "size", "mtime_ns"}:
            raise ValueError("fingerprint contains missing or unknown fields")
        digest = value.get("sha256")
        size = value.get("size")
        mtime_ns = value.get("mtime_ns")
        if (
            not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(mtime_ns, int)
            or isinstance(mtime_ns, bool)
        ):
            raise ValueError("invalid source fingerprint")
        return cls(digest, size, mtime_ns)

    def to_json(self) -> dict[str, object]:
        return {
            "sha256": self.sha256,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
        }


@dataclass(frozen=True)
class _Entry:
    label: str
    source: Path
    target: Path
    stage: Path
    fingerprint: _Fingerprint


@dataclass
class _Transaction:
    transaction_id: str
    input_root: Path
    archive_root: Path
    transaction_root: Path
    directory: Path
    manifest_path: Path
    payload: dict[str, object]
    entries: tuple[_Entry, ...]

    @property
    def backend(self) -> str:
        return str(self.payload["backend"])


def _canonical(path: Path) -> str:
    return pathio.path_key(path)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath([_canonical(path), _canonical(parent)]) == _canonical(parent)
    except (OSError, ValueError):
        return False


def _identity(stat_result: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(stat_result.st_dev),
        int(stat_result.st_ino),
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
    )


def _hash_open_stream(stream: object) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = stream.read(1024 * 1024)  # type: ignore[attr-defined]
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _fingerprint(path: Path) -> _Fingerprint:
    """Hash a stable regular file and record the filesystem's actual mtime."""

    if pathio.is_reparse_point(path) or not pathio.is_file(path):
        raise ValueError(f"source is not a regular non-link file: {path}")
    with pathio.open_path(path, "rb") as stream:
        before = os.fstat(stream.fileno())
        digest = _hash_open_stream(stream)
        after = os.fstat(stream.fileno())
    current = pathio.stat(path)
    if _identity(before) != _identity(after) or _identity(after) != _identity(current):
        raise OSError(f"source changed while it was being fingerprinted: {path}")
    return _Fingerprint(digest, int(current.st_size), int(current.st_mtime_ns))


def _matches(path: Path, fingerprint: _Fingerprint) -> bool:
    if not pathio.lexists(path) or pathio.is_reparse_point(path) or not pathio.is_file(path):
        return False
    try:
        before = pathio.stat(path)
        if (
            int(before.st_size) != fingerprint.size
            or int(before.st_mtime_ns) != fingerprint.mtime_ns
        ):
            return False
        with pathio.open_path(path, "rb") as stream:
            opened_before = os.fstat(stream.fileno())
            digest = _hash_open_stream(stream)
            opened_after = os.fstat(stream.fileno())
        after = pathio.stat(path)
        return (
            _identity(before) == _identity(opened_before)
            and _identity(opened_before) == _identity(opened_after)
            and _identity(opened_after) == _identity(after)
            and digest == fingerprint.sha256
        )
    except OSError:
        return False


def _path_state(path: Path, fingerprint: _Fingerprint) -> str:
    try:
        if not pathio.lexists(path):
            return "absent"
        return "match" if _matches(path, fingerprint) else "conflict"
    except OSError:
        return "conflict"


def _relative_posix(path: Path, root: Path) -> str:
    relative = path.relative_to(root)
    if not relative.parts:
        raise ValueError("a journal path cannot refer to its root")
    return PurePosixPath(*relative.parts).as_posix()


def _validated_relative(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError(f"invalid relative journal path: {field}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} or ":" in part for part in path.parts):
        raise ValueError(f"unsafe relative journal path: {field}")
    if path.as_posix() != value:
        raise ValueError(f"non-canonical relative journal path: {field}")
    return path.parts


def _join_contained(root: Path, value: object, field: str) -> Path:
    result = root.joinpath(*_validated_relative(value, field))
    if not _is_within(result, root):
        raise ValueError(f"journal path escapes its root: {field}")
    return result


def _canonical_payload(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _payload_checksum(payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_payload(payload)).hexdigest()


def _fsync_parent(path: Path) -> None:
    """Best-effort directory sync on platforms that permit opening directories."""

    if os.name == "nt":
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(pathio.io_path(path), os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_manifest(transaction: _Transaction, *, phase: str | None = None, backend: str | None = None) -> None:
    if phase is not None:
        if phase not in _PHASES:
            raise ValueError(f"invalid transaction phase: {phase}")
        transaction.payload["phase"] = phase
    if backend is not None:
        if backend not in _BACKENDS:
            raise ValueError(f"invalid archive backend: {backend}")
        transaction.payload["backend"] = backend
    document = {
        "payload": transaction.payload,
        "checksum": _payload_checksum(transaction.payload),
    }
    encoded = _canonical_payload(document)
    descriptor, temporary = pathio.mkstemp(
        prefix=".manifest-", suffix=".tmp", directory=transaction.directory
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        pathio.replace(temporary, transaction.manifest_path)
        _fsync_parent(transaction.directory)
    except BaseException:
        pathio.unlink(temporary, missing_ok=True)
        raise


def _coerce_plan(plan: Sequence[ArchivePlanEntry | tuple[str, Path, Path]]) -> tuple[ArchivePlanEntry, ...]:
    entries: list[ArchivePlanEntry] = []
    for item in plan:
        if isinstance(item, ArchivePlanEntry):
            entry = item
        else:
            try:
                label, source, target = item
            except (TypeError, ValueError) as error:
                raise ValueError("each archive plan entry must contain label, source, target") from error
            entry = ArchivePlanEntry(str(label), Path(source), Path(target))
        if not entry.label:
            raise ValueError("archive plan labels cannot be empty")
        entries.append(entry)
    if len(entries) != 2:
        raise ValueError("a source archive transaction requires exactly two files")
    return tuple(entries)


def _prepare_roots(input_root: Path | str, archive_root: Path | str | None) -> tuple[Path, Path, Path]:
    source_root = pathio.resolve_path(Path(input_root).expanduser(), strict=True)
    if not pathio.is_dir(source_root):
        raise ValueError(f"input root is not a directory: {source_root}")
    archive_entry = source_root / ARCHIVE_DIRECTORY_NAME
    requested = archive_entry if archive_root is None else Path(archive_root)
    if _canonical(requested) != _canonical(archive_entry):
        raise ValueError(f"archive root must be {archive_entry}")
    if pathio.lexists(archive_entry):
        if pathio.is_reparse_point(archive_entry) or not pathio.is_dir(archive_entry):
            raise ValueError(f"archive root is not a safe directory: {archive_entry}")
    else:
        pathio.mkdir(archive_entry, parents=True, exist_ok=False)
    resolved_archive = pathio.resolve_path(archive_entry, strict=True)
    if not _is_within(resolved_archive, source_root):
        raise ValueError(f"archive root escapes the input root: {archive_entry}")
    transaction_root = resolved_archive / TRANSACTION_DIRECTORY_NAME
    if pathio.lexists(transaction_root):
        if pathio.is_reparse_point(transaction_root) or not pathio.is_dir(transaction_root):
            raise ValueError(f"transaction root is not a safe directory: {transaction_root}")
    else:
        pathio.mkdir(transaction_root, parents=False, exist_ok=False)
    return source_root, resolved_archive, transaction_root


def _build_transaction(
    plan: Sequence[ArchivePlanEntry | tuple[str, Path, Path]],
    input_root: Path | str,
    archive_root: Path | str | None,
) -> _Transaction:
    planned = _coerce_plan(plan)
    source_root, resolved_archive, transaction_root = _prepare_roots(input_root, archive_root)
    normalized: list[tuple[str, Path, Path, _Fingerprint]] = []
    target_keys: set[str] = set()
    for item in planned:
        if pathio.is_reparse_point(item.source):
            raise ValueError(f"{item.label} is a symbolic link: {item.source}")
        source = pathio.resolve_path(item.source, strict=True)
        if not pathio.is_file(source) or not _is_within(source, source_root):
            raise ValueError(f"{item.label} is outside the input root: {source}")
        if _is_within(source, resolved_archive):
            raise ValueError(f"{item.label} is already inside the archive root: {source}")
        target = pathio.public_path(item.target)
        if not _is_within(target, resolved_archive):
            raise ValueError(f"{item.label} target escapes the archive root: {target}")
        if _is_within(target, transaction_root):
            raise ValueError(
                f"{item.label} target is inside the private transaction root: {target}"
            )
        pathio.mkdir(target.parent, parents=True, exist_ok=True)
        if not _is_within(target, resolved_archive):
            raise ValueError(f"{item.label} target parent changed during preparation: {target}")
        if _is_within(target, transaction_root):
            raise ValueError(
                f"{item.label} target parent changed into the private transaction root: {target}"
            )
        if pathio.lexists(target):
            raise FileExistsError(f"archive target already exists; refusing overwrite: {target}")
        if _relative_posix(target, resolved_archive) != _relative_posix(source, source_root):
            raise ValueError(f"{item.label} target does not mirror its source path")
        key = _canonical(target)
        if key in target_keys:
            raise ValueError("two sources map to the same archive target")
        target_keys.add(key)
        normalized.append((item.label, source, target, _fingerprint(source)))

    archive_device = int(pathio.stat(transaction_root).st_dev)
    for label, source, target, fingerprint in normalized:
        if int(pathio.stat(source).st_dev) != archive_device:
            raise OSError(errno.EXDEV, f"{label} and archive root are on different volumes")
        if int(pathio.stat(target.parent).st_dev) != archive_device:
            raise OSError(errno.EXDEV, f"{label} target is on a different volume")

    transaction_id = uuid4().hex
    directory = transaction_root / transaction_id
    pathio.mkdir(directory, exist_ok=False)
    entries: list[_Entry] = []
    manifest_entries: list[dict[str, object]] = []
    try:
        for index, (label, source, target, fingerprint) in enumerate(normalized):
            suffix = source.suffix if source.suffix else ".bin"
            stage = directory / f"stage-{index}{suffix}"
            entries.append(_Entry(label, source, target, stage, fingerprint))
            manifest_entries.append(
                {
                    "label": label,
                    "source_rel": _relative_posix(source, source_root),
                    "target_rel": _relative_posix(target, resolved_archive),
                    "stage_rel": _relative_posix(stage, directory),
                    "fingerprint": fingerprint.to_json(),
                }
            )
        payload: dict[str, object] = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "transaction_id": transaction_id,
            "phase": "planned",
            "backend": "auto",
            "entries": manifest_entries,
        }
        transaction = _Transaction(
            transaction_id,
            source_root,
            resolved_archive,
            transaction_root,
            directory,
            directory / MANIFEST_NAME,
            payload,
            tuple(entries),
        )
        _write_manifest(transaction)
        return transaction
    except BaseException:
        try:
            pathio.rmdir(directory)
        except OSError:
            pass
        raise


def _is_explicit_hardlink_unsupported(error: OSError) -> bool:
    winerror = getattr(error, "winerror", None)
    if winerror is not None:
        # ERROR_INVALID_FUNCTION is what exFAT commonly reports here;
        # ERROR_NOT_SUPPORTED is the other documented capability failure.
        # A concrete Win32 error takes precedence over Python's translated
        # errno so access, sharing, path, and collision failures never trigger
        # a destructive rename fallback.
        return winerror in {1, 50}
    unsupported = {errno.ENOSYS}
    if hasattr(errno, "ENOTSUP"):
        unsupported.add(errno.ENOTSUP)
    if hasattr(errno, "EOPNOTSUPP"):
        unsupported.add(errno.EOPNOTSUPP)
    return error.errno in unsupported


def _create_hardlink(source: Path, destination: Path) -> None:
    pathio.link(source, destination)
    if not pathio.samefile(source, destination):
        raise OSError(f"hard-link identity verification failed: {destination}")


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Move one file without ever replacing an existing directory entry."""

    if pathio.lexists(destination):
        raise FileExistsError(f"destination already exists: {destination}")
    if os.name == "nt":
        # MoveFileEx without MOVEFILE_REPLACE_EXISTING is the behavior exposed
        # by os.rename on Windows.  It atomically refuses a racing destination.
        pathio.rename(source, destination)
        return
    # POSIX rename replaces an existing file.  link+unlink gives equivalent
    # same-volume, no-replace file movement when hard links are available; if
    # they are not, fail closed instead of introducing an overwrite race.
    pathio.link(source, destination)
    try:
        pathio.unlink(source)
    except BaseException:
        pathio.unlink(destination, missing_ok=True)
        raise


def _safe_unlink_hardlink(
    path: Path,
    anchor: Path,
    fingerprint: _Fingerprint,
) -> None:
    """Remove only a verified alias of a trusted surviving hard link.

    A fingerprint alone cannot distinguish an independently created file with
    identical bytes and timestamps.  Rechecking ``samefile`` immediately
    before deletion prevents such a replacement occupant from being removed.
    """

    if not pathio.lexists(path):
        return
    if not _matches(path, fingerprint):
        raise OSError(f"path identity changed; preserving occupant: {path}")
    if not _matches(anchor, fingerprint):
        raise OSError(f"hard-link anchor changed; preserving occupant: {anchor}")
    try:
        same_file = pathio.samefile(path, anchor)
    except (OSError, ValueError) as error:
        raise OSError(
            f"hard-link identity cannot be verified; preserving occupant: {path}"
        ) from error
    if not same_file:
        raise OSError(
            f"path is no longer an alias of the verified hard link; preserving occupant: {path}"
        )
    pathio.unlink(path)


def _require_absent(path: Path, *, context: str) -> None:
    """Fail closed if a rename-only path acquired a racing occupant."""

    if pathio.lexists(path):
        raise OSError(f"{context}; preserving unexpected occupant: {path}")


def _move_matching(source: Path, destination: Path, fingerprint: _Fingerprint) -> None:
    if not _matches(source, fingerprint):
        raise OSError(f"path identity changed; refusing move: {source}")
    _rename_noreplace(source, destination)
    if _matches(destination, fingerprint):
        return
    # A racing replacement was moved.  Put it back when that remains safe.
    try:
        if not pathio.lexists(source):
            _rename_noreplace(destination, source)
    except OSError:
        pass
    raise OSError(f"moved file failed identity verification: {destination}")


def _remove_transaction_files(transaction: _Transaction) -> None:
    """Remove an inactive journal directory after a terminal durable state."""

    for child in tuple(pathio.iterdir(transaction.directory)):
        if child == transaction.manifest_path or _MANIFEST_TEMP_RE.fullmatch(child.name):
            pathio.unlink(child, missing_ok=True)
            continue
        if any(child == entry.stage for entry in transaction.entries):
            if pathio.lexists(child):
                raise OSError(f"transaction stage still exists: {child}")
            continue
        raise OSError(f"unexpected file prevents journal cleanup: {child}")
    pathio.rmdir(transaction.directory)
    _fsync_parent(transaction.transaction_root)


def _archive_locations(transaction: _Transaction) -> tuple[Path, ...]:
    return tuple(entry.target for entry in transaction.entries)


def _load_transaction(input_root: Path, archive_root: Path, transaction_root: Path, directory: Path) -> _Transaction:
    transaction_id = directory.name
    if not _TRANSACTION_ID_RE.fullmatch(transaction_id):
        raise ValueError("invalid transaction directory name")
    if _canonical(directory.parent) != _canonical(transaction_root):
        raise ValueError("transaction directory is not a direct child of its root")
    if pathio.is_reparse_point(directory) or not pathio.is_dir(directory):
        raise ValueError("transaction entry is not a safe directory")
    manifest_path = directory / MANIFEST_NAME
    if pathio.is_reparse_point(manifest_path) or not pathio.is_file(manifest_path):
        raise ValueError("transaction manifest is missing or unsafe")
    if pathio.stat(manifest_path).st_size > 1024 * 1024:
        raise ValueError("transaction manifest is too large")
    with pathio.open_path(manifest_path, "rb") as stream:
        raw = stream.read()
    try:
        def strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate JSON key: {key}")
                result[key] = value
            return result

        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid transaction manifest JSON: {error}") from error
    if not isinstance(document, dict) or set(document) != {"payload", "checksum"}:
        raise ValueError("invalid transaction manifest envelope")
    payload = document["payload"]
    checksum = document["checksum"]
    if not isinstance(payload, dict) or not isinstance(checksum, str):
        raise ValueError("invalid transaction manifest envelope values")
    if not re.fullmatch(r"[0-9a-f]{64}", checksum) or checksum != _payload_checksum(payload):
        raise ValueError("transaction manifest checksum mismatch")
    schema_version = payload.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != MANIFEST_SCHEMA_VERSION
    ):
        raise ValueError("unsupported transaction manifest schema")
    if set(payload) != {"schema_version", "transaction_id", "phase", "backend", "entries"}:
        raise ValueError("transaction payload contains missing or unknown fields")
    if payload.get("transaction_id") != transaction_id:
        raise ValueError("transaction id does not match its directory")
    phase = payload.get("phase")
    backend = payload.get("backend")
    if (
        not isinstance(phase, str)
        or phase not in _PHASES
        or not isinstance(backend, str)
        or backend not in _BACKENDS
    ):
        raise ValueError("invalid transaction state")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list) or len(raw_entries) != 2:
        raise ValueError("transaction must describe exactly two source files")
    entries: list[_Entry] = []
    targets: set[str] = set()
    for index, value in enumerate(raw_entries):
        if not isinstance(value, dict):
            raise ValueError("invalid transaction entry")
        if set(value) != {"label", "source_rel", "target_rel", "stage_rel", "fingerprint"}:
            raise ValueError("transaction entry contains missing or unknown fields")
        label = value.get("label")
        if not isinstance(label, str) or not label:
            raise ValueError("invalid transaction entry label")
        source = _join_contained(input_root, value.get("source_rel"), f"entries[{index}].source_rel")
        target = _join_contained(archive_root, value.get("target_rel"), f"entries[{index}].target_rel")
        stage = _join_contained(directory, value.get("stage_rel"), f"entries[{index}].stage_rel")
        if value.get("target_rel") != value.get("source_rel"):
            raise ValueError("journal target does not mirror its source path")
        expected_stage = f"stage-{index}{source.suffix if source.suffix else '.bin'}"
        if value.get("stage_rel") != expected_stage:
            raise ValueError("journal stage path is not canonical")
        if _is_within(source, archive_root):
            raise ValueError("journal source path is inside the archive root")
        if _is_within(target, transaction_root):
            raise ValueError("journal target path is inside the transaction root")
        if target.parent == archive_root and target.name == TRANSACTION_DIRECTORY_NAME:
            raise ValueError("journal target conflicts with the transaction root")
        key = _canonical(target)
        if key in targets:
            raise ValueError("duplicate journal target")
        targets.add(key)
        entries.append(
            _Entry(label, source, target, stage, _Fingerprint.from_json(value.get("fingerprint")))
        )
    return _Transaction(
        transaction_id,
        input_root,
        archive_root,
        transaction_root,
        directory,
        manifest_path,
        payload,
        tuple(entries),
    )


def _inventory_decision(transaction: _Transaction) -> tuple[str, str]:
    allowed = {transaction.manifest_path, *(entry.stage for entry in transaction.entries)}
    for child in pathio.iterdir(transaction.directory):
        if child in allowed or _MANIFEST_TEMP_RE.fullmatch(child.name):
            continue
        return "conflict", f"unexpected transaction artifact: {child}"
    inventories: list[dict[str, str]] = []
    for entry in transaction.entries:
        inventory = {
            "source": _path_state(entry.source, entry.fingerprint),
            "stage": _path_state(entry.stage, entry.fingerprint),
            "target": _path_state(entry.target, entry.fingerprint),
        }
        inventories.append(inventory)
        conflicts = [name for name, state in inventory.items() if state == "conflict"]
        if conflicts:
            return "conflict", f"{entry.label} has changed occupants at: {', '.join(conflicts)}"
        if "match" not in inventory.values():
            return "conflict", f"no matching copy remains for {entry.label}"
        matching_paths = [
            path
            for path, state in (
                (entry.source, inventory["source"]),
                (entry.stage, inventory["stage"]),
                (entry.target, inventory["target"]),
            )
            if state == "match"
        ]
        if transaction.backend == "rename" and len(matching_paths) != 1:
            return "conflict", f"rename transaction has duplicate copies for {entry.label}"
        if transaction.backend in {"auto", "hardlink"} and len(matching_paths) > 1:
            try:
                if any(not pathio.samefile(matching_paths[0], path) for path in matching_paths[1:]):
                    return "conflict", f"hard-link identities disagree for {entry.label}"
            except OSError:
                return "conflict", f"hard-link identity cannot be verified for {entry.label}"
    if all(inventory["target"] == "match" for inventory in inventories):
        return "complete", "both final archive files match the durable manifest"
    return "rollback", "the final archive pair is incomplete; safe rollback is available"


def _result(transaction: _Transaction, status: RecoveryStatus, reason: str) -> RecoveryResult:
    return RecoveryResult(
        transaction.transaction_id,
        status,
        reason,
        transaction.manifest_path,
        transaction.backend,
        _archive_locations(transaction),
    )


def _recover_loaded(transaction: _Transaction) -> RecoveryResult:
    try:
        decision, reason = _inventory_decision(transaction)
    except (OSError, ValueError) as error:
        reason = f"transaction inventory could not be read safely: {error}"
        try:
            _write_manifest(transaction, phase="recovery_required")
        except OSError as journal_error:
            reason += f"; journal update also failed: {journal_error}"
        return _result(transaction, RecoveryStatus.RECOVERY_REQUIRED, reason)
    if decision == "conflict":
        try:
            _write_manifest(transaction, phase="recovery_required")
        except OSError as error:
            reason += f"; journal update also failed: {error}"
        return _result(transaction, RecoveryStatus.RECOVERY_REQUIRED, reason)
    try:
        if decision == "complete":
            _write_manifest(transaction, phase="cleaning")
            # Both final files are already verified.  For hard-link journals,
            # recheck alias identity at the exact point of deletion: matching
            # bytes and timestamps alone must never authorize unlinking a
            # replacement occupant.  Rename journals cannot legitimately have
            # any source/stage alias once both targets exist, so fail closed if
            # one appears after inventory instead of trying to delete it.
            if transaction.backend in {"auto", "hardlink"}:
                for entry in reversed(transaction.entries):
                    _safe_unlink_hardlink(
                        entry.stage, entry.target, entry.fingerprint
                    )
                    _safe_unlink_hardlink(
                        entry.source, entry.target, entry.fingerprint
                    )
            else:
                for entry in reversed(transaction.entries):
                    _require_absent(
                        entry.stage,
                        context="rename stage reappeared during completion",
                    )
                    _require_absent(
                        entry.source,
                        context="rename source reappeared during completion",
                    )
            for entry in transaction.entries:
                if not _matches(entry.target, entry.fingerprint):
                    raise OSError(f"final archive file changed during recovery: {entry.target}")
            _write_manifest(transaction, phase="completed")
            _remove_transaction_files(transaction)
            return _result(transaction, RecoveryStatus.COMPLETED, "completed the verified archive pair")

        _write_manifest(transaction, phase="rolling_back")
        for entry in transaction.entries:
            source_state = _path_state(entry.source, entry.fingerprint)
            if source_state == "conflict":
                raise OSError(f"source path is occupied by a different file: {entry.source}")
            if source_state == "absent":
                if _path_state(entry.stage, entry.fingerprint) == "match":
                    candidate = entry.stage
                elif _path_state(entry.target, entry.fingerprint) == "match":
                    candidate = entry.target
                else:
                    raise OSError(f"no matching copy can restore source: {entry.source}")
                pathio.mkdir(entry.source.parent, parents=True, exist_ok=True)
                _move_matching(candidate, entry.source, entry.fingerprint)
            if not _matches(entry.source, entry.fingerprint):
                raise OSError(f"restored source failed verification: {entry.source}")
        if transaction.backend in {"auto", "hardlink"}:
            for entry in transaction.entries:
                _safe_unlink_hardlink(
                    entry.stage, entry.source, entry.fingerprint
                )
                _safe_unlink_hardlink(
                    entry.target, entry.source, entry.fingerprint
                )
        else:
            # A rename has exactly one name for each source at every durable
            # boundary.  After restoration, stage and target must therefore be
            # absent; deleting a newly appeared lookalike would be unsafe.
            for entry in transaction.entries:
                _require_absent(
                    entry.stage,
                    context="rename stage reappeared during rollback",
                )
                _require_absent(
                    entry.target,
                    context="rename target reappeared during rollback",
                )
        _write_manifest(transaction, phase="rolled_back")
        _remove_transaction_files(transaction)
        return _result(transaction, RecoveryStatus.ROLLED_BACK, "rolled the incomplete archive pair back to both sources")
    except (OSError, ValueError) as error:
        detail = f"{reason}; recovery stopped safely: {error}"
        try:
            _write_manifest(transaction, phase="recovery_required")
        except OSError as journal_error:
            detail += f"; journal update also failed: {journal_error}"
        return _result(transaction, RecoveryStatus.RECOVERY_REQUIRED, detail)


def _recover_after_failure(transaction: _Transaction, reason: str) -> SourceActionOutcome:
    recovered = _recover_loaded(transaction)
    if recovered.status is RecoveryStatus.ROLLED_BACK:
        return SourceActionOutcome(
            SourceActionStatus.FAILED,
            f"archive failed and was safely rolled back: {reason}",
            transaction.transaction_id,
            transaction.backend,
            _archive_locations(transaction),
            transaction.manifest_path,
        )
    if recovered.status is RecoveryStatus.COMPLETED:
        return SourceActionOutcome(
            SourceActionStatus.COMPLETED,
            f"archive completed during recovery after an interruption: {reason}",
            transaction.transaction_id,
            transaction.backend,
            _archive_locations(transaction),
            transaction.manifest_path,
        )
    return SourceActionOutcome(
        SourceActionStatus.RECOVERY_REQUIRED,
        f"archive needs recovery: {reason}; {recovered.reason}",
        transaction.transaction_id,
        transaction.backend,
        _archive_locations(transaction),
        transaction.manifest_path,
    )


def _cancel_and_rollback(transaction: _Transaction) -> SourceActionOutcome:
    recovered = _recover_loaded(transaction)
    if recovered.status is RecoveryStatus.ROLLED_BACK:
        return SourceActionOutcome(
            SourceActionStatus.CANCELED,
            "archive canceled before commit; both sources were restored",
            transaction.transaction_id,
            transaction.backend,
            _archive_locations(transaction),
            transaction.manifest_path,
        )
    return SourceActionOutcome(
        SourceActionStatus.RECOVERY_REQUIRED,
        f"archive cancellation could not be rolled back automatically: {recovered.reason}",
        transaction.transaction_id,
        transaction.backend,
        _archive_locations(transaction),
        transaction.manifest_path,
    )


def _stage_hardlinks(transaction: _Transaction) -> bool:
    """Return False only for an explicitly unsupported hard-link filesystem."""

    _write_manifest(transaction, phase="staging", backend="auto")
    created: list[_Entry] = []
    try:
        for entry in transaction.entries:
            _create_hardlink(entry.source, entry.stage)
            created.append(entry)
    except OSError as error:
        if not _is_explicit_hardlink_unsupported(error):
            raise
        cleanup_errors: list[str] = []
        for entry in reversed(created):
            try:
                _safe_unlink_hardlink(
                    entry.stage, entry.source, entry.fingerprint
                )
            except OSError as cleanup_error:
                cleanup_errors.append(str(cleanup_error))
        if cleanup_errors:
            raise OSError("hard-link fallback cleanup failed: " + "; ".join(cleanup_errors)) from error
        return False
    _write_manifest(transaction, phase="prepared", backend="hardlink")
    return True


def _stage_renames(transaction: _Transaction) -> None:
    _write_manifest(transaction, phase="staging", backend="rename")
    for entry in transaction.entries:
        if not _matches(entry.source, entry.fingerprint):
            raise OSError(f"source changed before rename staging: {entry.source}")
        _move_matching(entry.source, entry.stage, entry.fingerprint)
    _write_manifest(transaction, phase="prepared", backend="rename")


def _commit_hardlinks(transaction: _Transaction) -> None:
    _write_manifest(transaction, phase="committing", backend="hardlink")
    for entry in transaction.entries:
        if not _matches(entry.stage, entry.fingerprint):
            raise OSError(f"hard-link stage changed before commit: {entry.stage}")
        _create_hardlink(entry.stage, entry.target)
    for entry in transaction.entries:
        if not _matches(entry.target, entry.fingerprint):
            raise OSError(f"final archive verification failed: {entry.target}")
    _write_manifest(transaction, phase="committed")
    _write_manifest(transaction, phase="cleaning")
    # Once both finals are verified, cancellation is intentionally ignored.
    # Stopping between these two source removals would create a partial action.
    for entry in reversed(transaction.entries):
        _safe_unlink_hardlink(entry.source, entry.target, entry.fingerprint)
    for entry in reversed(transaction.entries):
        _safe_unlink_hardlink(entry.stage, entry.target, entry.fingerprint)


def _commit_renames(transaction: _Transaction) -> None:
    _write_manifest(transaction, phase="committing", backend="rename")
    for entry in transaction.entries:
        _move_matching(entry.stage, entry.target, entry.fingerprint)
    for entry in transaction.entries:
        if not _matches(entry.target, entry.fingerprint):
            raise OSError(f"final archive verification failed: {entry.target}")
    _write_manifest(transaction, phase="committed")


def archive_sources(
    plan: Sequence[ArchivePlanEntry | tuple[str, Path, Path]],
    *,
    input_root: Path | str,
    archive_root: Path | str | None = None,
    output_verified: bool,
    cancel_event: Event | None = None,
) -> SourceActionOutcome:
    """Archive two verified conversion sources as one recoverable transaction."""

    if not output_verified:
        return SourceActionOutcome(SourceActionStatus.FAILED, "source action requires a verified conversion output")
    if cancel_event is not None and cancel_event.is_set():
        return SourceActionOutcome(SourceActionStatus.CANCELED, "archive canceled before the transaction began")
    try:
        transaction = _build_transaction(plan, input_root, archive_root)
    except (OSError, ValueError) as error:
        return SourceActionOutcome(SourceActionStatus.FAILED, f"archive preflight failed: {error}")

    # Fingerprinting large videos can take long enough for cancellation to be
    # requested during preflight.  Honor it before creating any hard link or
    # moving either source; the durable planned journal is rolled back/removed.
    if cancel_event is not None and cancel_event.is_set():
        return _cancel_and_rollback(transaction)

    try:
        hardlink_backend = _stage_hardlinks(transaction)
        if not hardlink_backend:
            _stage_renames(transaction)
        if cancel_event is not None and cancel_event.is_set():
            return _cancel_and_rollback(transaction)
        if hardlink_backend:
            _commit_hardlinks(transaction)
        else:
            _commit_renames(transaction)
        _write_manifest(transaction, phase="completed")
        _remove_transaction_files(transaction)
        locations = "; ".join(
            f"{entry.label} -> {entry.target}" for entry in transaction.entries
        )
        return SourceActionOutcome(
            SourceActionStatus.COMPLETED,
            f"sources archived successfully: {locations}",
            transaction.transaction_id,
            transaction.backend,
            _archive_locations(transaction),
            transaction.manifest_path,
        )
    except (OSError, ValueError) as error:
        return _recover_after_failure(transaction, str(error))


def _enumerate_transactions(input_root: Path | str) -> tuple[Path, Path, Path, tuple[Path, ...]]:
    source_root = pathio.resolve_path(Path(input_root).expanduser(), strict=True)
    if not pathio.is_dir(source_root):
        raise ValueError(f"input root is not a directory: {source_root}")
    archive_entry = source_root / ARCHIVE_DIRECTORY_NAME
    if not pathio.lexists(archive_entry):
        return source_root, archive_entry, archive_entry / TRANSACTION_DIRECTORY_NAME, ()
    if pathio.is_reparse_point(archive_entry) or not pathio.is_dir(archive_entry):
        raise ValueError(f"archive root is not a safe directory: {archive_entry}")
    archive_root = pathio.resolve_path(archive_entry, strict=True)
    transaction_root = archive_root / TRANSACTION_DIRECTORY_NAME
    if not pathio.lexists(transaction_root):
        return source_root, archive_root, transaction_root, ()
    if pathio.is_reparse_point(transaction_root) or not pathio.is_dir(transaction_root):
        raise ValueError(f"transaction root is not a safe directory: {transaction_root}")
    return source_root, archive_root, transaction_root, tuple(pathio.iterdir(transaction_root))


def _invalid_result(directory: Path, reason: str) -> RecoveryResult:
    return RecoveryResult(
        directory.name,
        RecoveryStatus.INVALID,
        reason,
        directory / MANIFEST_NAME,
    )


def inspect_archive_transactions(input_root: Path | str) -> list[RecoveryResult]:
    """Read and classify pending archive journals without changing any file."""

    try:
        source_root, archive_root, transaction_root, directories = _enumerate_transactions(input_root)
    except (OSError, ValueError) as error:
        fallback = pathio.public_path(input_root) / ARCHIVE_DIRECTORY_NAME / TRANSACTION_DIRECTORY_NAME
        return [_invalid_result(fallback, f"cannot inspect archive transactions: {error}")]
    results: list[RecoveryResult] = []
    for directory in sorted(directories, key=lambda item: item.name):
        try:
            transaction = _load_transaction(source_root, archive_root, transaction_root, directory)
            decision, reason = _inventory_decision(transaction)
            if decision == "complete":
                status = RecoveryStatus.WOULD_COMPLETE
            elif decision == "rollback":
                status = RecoveryStatus.WOULD_ROLL_BACK
            else:
                status = RecoveryStatus.RECOVERY_REQUIRED
            results.append(_result(transaction, status, reason))
        except (OSError, ValueError) as error:
            results.append(_invalid_result(directory, str(error)))
    return results


def recover_archive_transactions(input_root: Path | str) -> list[RecoveryResult]:
    """Recover every valid journal; conflicts and invalid journals are retained."""

    try:
        source_root, archive_root, transaction_root, directories = _enumerate_transactions(input_root)
    except (OSError, ValueError) as error:
        fallback = pathio.public_path(input_root) / ARCHIVE_DIRECTORY_NAME / TRANSACTION_DIRECTORY_NAME
        return [_invalid_result(fallback, f"cannot recover archive transactions: {error}")]
    results: list[RecoveryResult] = []
    for directory in sorted(directories, key=lambda item: item.name):
        try:
            transaction = _load_transaction(source_root, archive_root, transaction_root, directory)
            results.append(_recover_loaded(transaction))
        except (OSError, ValueError) as error:
            results.append(_invalid_result(directory, str(error)))
    return results


__all__ = [
    "ARCHIVE_DIRECTORY_NAME",
    "ArchivePlanEntry",
    "RecoveryResult",
    "RecoveryStatus",
    "SourceActionOutcome",
    "SourceActionStatus",
    "TRANSACTION_DIRECTORY_NAME",
    "archive_sources",
    "inspect_archive_transactions",
    "recover_archive_transactions",
]
