"""Recursive metadata scanning without following links."""
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterator

from core.paths import is_link, reject_overlap, safe_path


@dataclass(frozen=True)
class ScannedFile:
    path: Path
    relative_path: str
    size_bytes: int
    modified_at_ns: int


def scan_files(source: Path, destination: Path | None, database_path: Path) -> Iterator[ScannedFile]:
    source, database_path = Path(source), Path(database_path)
    if not source.is_dir() or is_link(source):
        raise ValueError("Source must be an existing directory, not a link")
    source = source.resolve()
    if destination is not None:
        reject_overlap(source, Path(destination))
    excluded = {Path(str(database_path.resolve()) + suffix) for suffix in ("", "-wal", "-shm", "-journal")}

    def walk(folder: Path) -> Iterator[ScannedFile]:
        for path in sorted(folder.iterdir()):
            if is_link(path):
                continue
            if path.is_dir():
                yield from walk(path)
            elif path.is_file() and path.resolve() not in excluded:
                relative = path.relative_to(source).as_posix()
                safe_path(source, relative)
                metadata = path.stat()
                yield ScannedFile(path, relative, metadata.st_size, metadata.st_mtime_ns)

    yield from walk(source)
