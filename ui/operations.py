"""Process-local background tasks, shared across Streamlit reruns/sessions.

No Streamlit calls occur in worker threads. Engines open their own connections.
One dashboard operation per database is allowed; CLI processes remain separate.
"""
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from collections.abc import Callable
import time
import uuid

from core.progress import SyncProgress


@dataclass
class Operation:
    id: str
    database: str
    kind: str
    future: Future | None = None
    progress: SyncProgress | None = None
    lock: Lock = field(default_factory=Lock)
    created: float = field(default_factory=time.monotonic)

    def observe(self, event: SyncProgress) -> None:
        with self.lock:
            self.progress = event

    def snapshot(self) -> SyncProgress | None:
        with self.lock:
            return self.progress


class OperationManager:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cloudsync")
        self.lock = Lock()
        self.tasks: dict[str, Operation] = {}

    def busy(self, database: Path) -> bool:
        normalized = str(Path(database).resolve()).casefold()
        with self.lock:
            return any(task.database == normalized and (task.future is None or not task.future.done())
                       for task in self.tasks.values())

    def get(self, operation_id: str | None) -> Operation | None:
        with self.lock:
            return self.tasks.get(operation_id)

    def start(self, database: Path, kind: str, action: Callable[[Callable], int]) -> Operation:
        normalized = str(Path(database).resolve()).casefold()
        with self.lock:
            if any(task.database == normalized and (task.future is None or not task.future.done())
                   for task in self.tasks.values()):
                raise RuntimeError("Another dashboard operation is already running for this database")
            # Retain recent outcomes for reruns; SQLite is the durable history.
            for identifier, task in list(self.tasks.items()):
                if task.future is not None and task.future.done() and time.monotonic() - task.created > 86400:
                    del self.tasks[identifier]
            task = Operation(uuid.uuid4().hex, normalized, kind)
            self.tasks[task.id] = task
            try:
                task.future = self.executor.submit(action, task.observe)
            except BaseException:
                del self.tasks[task.id]
                raise
            return task


MANAGER = OperationManager()
