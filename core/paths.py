"""Shared path validation for local storage and restoration."""
from pathlib import Path, PureWindowsPath
import stat


def is_link(path: Path) -> bool:
    """Include Windows junctions/reparse points as well as ordinary symlinks."""
    return path.is_symlink() or bool(
        getattr(path.lstat(), "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def reject_overlap(first: Path, second: Path) -> None:
    first, second = first.resolve(), second.resolve()
    if first == second or first in second.parents or second in first.parents:
        raise ValueError("Source, storage, and restore folders must not overlap")


def validate_key(key: str) -> list[str]:
    """Validate a portable relative object key without touching the filesystem."""
    if not isinstance(key, str) or not key or "\\" in key or ":" in key or "\x00" in key:
        raise ValueError("Invalid relative path")
    parts = key.split("/")
    if PureWindowsPath(key).is_absolute() or any(
        part in ("", ".", "..") or part.endswith((" ", "."))
        or any(char in part for char in '<>"|?*')
        or any(ord(char) < 32 for char in part)
        or PureWindowsPath(part).is_reserved()
        for part in parts
    ):
        raise ValueError("Invalid relative path")
    return parts


def safe_path(root: Path, key: str) -> Path:
    """Validate portable relative keys, including Windows drive/ADS/device paths.

    This rejects existing links. Directories must not be changed by another
    process during an operation (local trusted-folder assumption).
    """
    parts = validate_key(key)
    root = Path(root).resolve()
    candidate = root
    for part in parts:
        candidate /= part
        if candidate.exists() or candidate.is_symlink():
            if is_link(candidate):
                raise ValueError("Links are not allowed in stored or restored paths")
    if not candidate.resolve().is_relative_to(root):
        raise ValueError("Path escapes its root")
    return candidate
