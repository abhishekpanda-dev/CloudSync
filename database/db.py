"""SQLite connection lifecycle and repeatable schema initialization."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from collections.abc import Iterator

DEFAULT_DATABASE_PATH = Path(__file__).resolve().parents[1] / "data" / "cloudsync.sqlite3"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


@contextmanager
def connect_database(path: Path = DEFAULT_DATABASE_PATH) -> Iterator[sqlite3.Connection]:
    """Open with foreign keys enabled; commit on success, rollback on error, close."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database(path: Path = DEFAULT_DATABASE_PATH) -> Path:
    """Create missing tables without removing existing metadata.

    The SQL script is trusted, static project data. Future queries with user
    values must use SQLite placeholders, never string interpolation.
    """
    path = Path(path)
    with connect_database(path) as connection:
        connection.executescript("BEGIN;\n" + SCHEMA_PATH.read_text(encoding="utf-8"))
        _migrate_local_sync(connection)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sync_jobs)")}
        if "s3_region" not in columns:
            connection.execute("ALTER TABLE sync_jobs ADD COLUMN s3_region TEXT")
    return path


def _migrate_local_sync(connection: sqlite3.Connection) -> None:
    """Add progress fields without rebuilding tables or changing old constraints.

    Legacy files have no proven completion state and stay pending. Previously
    uploaded chunks retain their progress, but legacy jobs cannot be restored
    until file completion has been explicitly recorded by the new engine.
    """
    file_columns = {row[1] for row in connection.execute("PRAGMA table_info(files)")}
    if "status" not in file_columns:
        connection.execute(
            "ALTER TABLE files ADD COLUMN status TEXT NOT NULL DEFAULT 'pending' "
            "CHECK (status IN ('pending', 'uploading', 'completed', 'failed'))"
        )
    if "error_message" not in file_columns:
        connection.execute("ALTER TABLE files ADD COLUMN error_message TEXT")
    chunk_columns = {row[1] for row in connection.execute("PRAGMA table_info(chunks)")}
    if "upload_status" not in chunk_columns:
        connection.execute(
            "ALTER TABLE chunks ADD COLUMN upload_status TEXT NOT NULL DEFAULT 'pending' "
            "CHECK (upload_status IN ('pending', 'uploading', 'completed', 'failed'))"
        )
        connection.execute(
            "UPDATE chunks SET upload_status = CASE status "
            "WHEN 'uploaded' THEN 'completed' WHEN 'failed' THEN 'failed' ELSE 'pending' END"
        )


if __name__ == "__main__":
    print(f"Initialized {initialize_database()}")
