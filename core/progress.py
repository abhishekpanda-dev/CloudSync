"""Immutable progress snapshots for observers of the full-copy engine."""
from collections.abc import Callable
from dataclasses import dataclass
import logging


@dataclass(frozen=True)
class SyncProgress:
    job_id: int
    status: str
    total_files: int = 0
    completed_files: int = 0
    total_bytes: int = 0
    transferred_bytes: int = 0
    total_chunks: int = 0
    uploaded_chunks: int = 0
    current_file: str | None = None
    current_chunk: int | None = None


ProgressCallback = Callable[[SyncProgress], None]


def notify(callback: ProgressCallback | None, event: SyncProgress) -> None:
    """An observer failure must never alter storage or persisted job results."""
    if callback is not None:
        try:
            callback(event)
        except Exception:
            logging.getLogger(__name__).warning("CloudSync progress observer failed")
