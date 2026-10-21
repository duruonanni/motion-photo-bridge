r"""Filesystem helpers with transparent Windows extended-length paths.

Business objects keep ordinary, readable :class:`~pathlib.Path` values.  Only
the string passed to a filesystem API receives the ``\\?\`` (or
``\\?\UNC\``) prefix on Windows.  This bypasses the legacy MAX_PATH limit
without leaking implementation-only path spelling into GUI/CLI output.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, IO


PathLike = str | os.PathLike[str]
_EXTENDED_PREFIX = "\\\\?\\"
_EXTENDED_UNC_PREFIX = "\\\\?\\UNC\\"
_DEVICE_PREFIX = "\\\\.\\"


def _is_extended_drive_path(value: str) -> bool:
    remainder = value[len(_EXTENDED_PREFIX) :]
    return (
        len(remainder) >= 3
        and remainder[0].isalpha()
        and remainder[1] == ":"
        and remainder[2] in "\\/"
    )


def io_path(path: PathLike) -> str:
    """Return the path spelling to pass to an operating-system I/O call."""

    value = os.fspath(path)
    if os.name != "nt":
        return value
    value = os.path.normpath(os.path.abspath(value))
    folded = value.casefold()
    if folded.startswith(_DEVICE_PREFIX.casefold()):
        raise ValueError("Windows device namespace paths are not supported")
    if folded.startswith(_EXTENDED_PREFIX.casefold()):
        if folded.startswith(_EXTENDED_UNC_PREFIX.casefold()) or _is_extended_drive_path(value):
            return value
        raise ValueError("only extended drive-letter and UNC paths are supported")
    if value.startswith("\\\\"):
        return _EXTENDED_UNC_PREFIX + value[2:]
    return _EXTENDED_PREFIX + value


def public_path(path: PathLike) -> Path:
    """Strip a Windows I/O prefix and return a normal user-facing path."""

    value = os.fspath(path)
    if os.name == "nt":
        folded = value.casefold()
        if folded.startswith(_EXTENDED_UNC_PREFIX.casefold()):
            value = "\\\\" + value[len(_EXTENDED_UNC_PREFIX) :]
        elif folded.startswith(_EXTENDED_PREFIX.casefold()):
            if not _is_extended_drive_path(value):
                raise ValueError("only extended drive-letter and UNC paths are supported")
            value = value[len(_EXTENDED_PREFIX) :]
        elif folded.startswith(_DEVICE_PREFIX.casefold()):
            raise ValueError("Windows device namespace paths are not supported")
    return Path(value)


def resolve_path(path: PathLike, *, strict: bool = False) -> Path:
    """Resolve a path through the extended Win32 spelling, returning it plain."""

    expanded = os.path.expanduser(os.fspath(path))
    absolute = os.path.normpath(os.path.abspath(expanded))
    native = io_path(absolute)
    try:
        resolved = os.path.realpath(native, strict=strict)
    except TypeError:  # Python < 3.10 compatibility for realpath(strict=...).
        resolved = os.path.realpath(native)
        if strict and not os.path.exists(resolved):
            raise FileNotFoundError(absolute)
    return public_path(resolved)


def path_key(path: PathLike) -> str:
    """Canonical comparison key without exposing an extended-length prefix."""

    return os.path.normcase(os.fspath(resolve_path(path, strict=False)))


def open_path(path: PathLike, mode: str = "r", *args: Any, **kwargs: Any) -> IO[Any]:
    return open(io_path(path), mode, *args, **kwargs)


def read_bytes(path: PathLike) -> bytes:
    with open_path(path, "rb") as stream:
        return stream.read()


def stat(path: PathLike) -> os.stat_result:
    return os.stat(io_path(path))


def utime(path: PathLike, *, ns: tuple[int, int]) -> None:
    """Set access and modification times using the extended path spelling."""

    os.utime(io_path(path), ns=ns)


def exists(path: PathLike) -> bool:
    return os.path.exists(io_path(path))


def lexists(path: PathLike) -> bool:
    """Return whether a directory entry exists, including a broken symlink."""

    return os.path.lexists(io_path(path))


def is_file(path: PathLike) -> bool:
    return os.path.isfile(io_path(path))


def is_link(path: PathLike) -> bool:
    return os.path.islink(io_path(path))


def is_reparse_point(path: PathLike) -> bool:
    """Return whether an existing Windows entry is a reparse point.

    This includes junctions, which :func:`os.path.islink` did not consistently
    identify on all supported Python/Windows combinations.
    """

    if os.name != "nt":
        return is_link(path)
    try:
        attributes = getattr(os.lstat(io_path(path)), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def is_dir(path: PathLike) -> bool:
    return os.path.isdir(io_path(path))


def mkdir(path: PathLike, *, parents: bool = False, exist_ok: bool = False) -> None:
    target = io_path(path)
    if parents:
        os.makedirs(target, exist_ok=exist_ok)
        return
    try:
        os.mkdir(target)
    except FileExistsError:
        if not exist_ok or not os.path.isdir(target):
            raise


def unlink(path: PathLike, *, missing_ok: bool = False) -> None:
    try:
        os.unlink(io_path(path))
    except FileNotFoundError:
        if not missing_ok:
            raise


def rmdir(path: PathLike) -> None:
    """Remove one empty directory using the extended path spelling."""

    os.rmdir(io_path(path))


def samefile(first: PathLike, second: PathLike) -> bool:
    return os.path.samefile(io_path(first), io_path(second))


def replace(source: PathLike, destination: PathLike) -> None:
    os.replace(io_path(source), io_path(destination))


def link(source: PathLike, destination: PathLike) -> None:
    os.link(io_path(source), io_path(destination))


def rename(source: PathLike, destination: PathLike) -> None:
    os.rename(io_path(source), io_path(destination))


def mkstemp(*, prefix: str, suffix: str, directory: PathLike) -> tuple[int, Path]:
    descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=io_path(directory))
    return descriptor, public_path(name)


def iterdir(path: PathLike) -> Iterator[Path]:
    """Yield public child paths while scanning with an extended root."""

    parent = public_path(io_path(path))
    with os.scandir(io_path(path)) as entries:
        for entry in entries:
            yield parent / entry.name


def walk(
    path: PathLike,
    *,
    followlinks: bool = False,
) -> Iterator[tuple[Path, list[str], list[str]]]:
    """Like :func:`os.walk`, but never expose the extended prefix."""

    for current, directory_names, file_names in os.walk(
        io_path(path), followlinks=followlinks
    ):
        yield public_path(current), directory_names, file_names


__all__ = [
    "exists",
    "is_dir",
    "is_file",
    "is_link",
    "is_reparse_point",
    "io_path",
    "iterdir",
    "lexists",
    "link",
    "mkdir",
    "mkstemp",
    "open_path",
    "public_path",
    "path_key",
    "read_bytes",
    "rename",
    "replace",
    "resolve_path",
    "rmdir",
    "samefile",
    "stat",
    "unlink",
    "utime",
    "walk",
]
