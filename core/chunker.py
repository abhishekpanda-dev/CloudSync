"""Stream fixed-size binary chunks, including the last partial chunk."""
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from core.hasher import hash_chunk

DEFAULT_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class Chunk:
    index: int
    offset_bytes: int
    data: bytes
    checksum_sha256: str

    @property
    def size_bytes(self) -> int:
        return len(self.data)


def iter_chunks(path: Path, chunk_size: int = DEFAULT_CHUNK_SIZE) -> Iterator[Chunk]:
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size <= 0:
        raise ValueError("Chunk size must be a positive integer")
    offset, index = 0, 0
    with Path(path).open("rb") as source:
        while data := source.read(chunk_size):
            yield Chunk(index, offset, data, hash_chunk(data))
            offset += len(data)
            index += 1
