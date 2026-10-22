"""Local implementation of the existing file-based storage contract."""
from pathlib import Path
import os
import shutil
import tempfile

from core.paths import is_link, safe_path
from storage.base import StorageBackend


class LocalStorageBackend(StorageBackend):
    def __init__(self, root: Path):
        root = Path(root)
        if root.exists() and (not root.is_dir() or is_link(root)):
            raise ValueError("Storage root must be a directory, not a link")
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def upload_chunk(self, source: Path, key: str) -> None:
        target = safe_path(self.root, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".chunk-", delete=False) as output:
                temporary = Path(output.name)
                with Path(source).open("rb") as input_file:
                    shutil.copyfileobj(input_file, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def download_chunk(self, key: str, destination: Path) -> None:
        source = safe_path(self.root, key)
        # Caller supplies an explicit temporary file; destination validation for
        # user-facing restored paths belongs to the restore service.
        with source.open("rb") as input_file, Path(destination).open("wb") as output:
            shutil.copyfileobj(input_file, output, length=1024 * 1024)

    def exists(self, key: str) -> bool:
        return safe_path(self.root, key).is_file()

    def delete_chunk(self, key: str) -> None:
        safe_path(self.root, key).unlink(missing_ok=True)
