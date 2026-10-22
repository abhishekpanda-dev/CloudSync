"""Storage contract; concrete local and S3 backends are future work."""
from abc import ABC, abstractmethod
from pathlib import Path


class StorageBackend(ABC):
    """Store chunks under backend-neutral, relative object keys.

    Implementations must reject absolute keys and parent traversal. Network and
    filesystem errors should propagate to the caller. No backend is implemented
    in this foundation.
    """

    @abstractmethod
    def upload_chunk(self, source: Path, key: str) -> None:
        """Store a chunk file, replacing the object if its key already exists."""
        raise NotImplementedError

    @abstractmethod
    def download_chunk(self, key: str, destination: Path) -> None:
        """Write a stored chunk to destination; missing keys raise FileNotFoundError."""
        raise NotImplementedError

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Return whether a chunk is stored at key."""
        raise NotImplementedError

    @abstractmethod
    def delete_chunk(self, key: str) -> None:
        """Delete a stored chunk; missing keys are a no-op."""
        raise NotImplementedError
