"""Reusable SHA-256 helpers; files are read in bounded blocks."""
import hashlib
from pathlib import Path

DEFAULT_READ_SIZE = 1024 * 1024


def hash_chunk(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_file(path: Path, read_size: int = DEFAULT_READ_SIZE) -> str:
    if read_size <= 0:
        raise ValueError("Read size must be positive")
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while data := source.read(read_size):
            digest.update(data)
    return digest.hexdigest()
