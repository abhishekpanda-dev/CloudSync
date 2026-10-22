"""Read-only dashboard queries; no shared connections or invented metrics."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3


@contextmanager
def read_database(path: Path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        # One read snapshot for related metrics. Readers never change metadata.
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()


def summary(path: Path) -> dict:
    with read_database(path) as db:
        jobs = db.execute("SELECT COUNT(*), COALESCE(SUM(status = 'completed'), 0) FROM sync_jobs").fetchone()
        files = db.execute("SELECT COUNT(*) FROM files WHERE status = ?", ("completed",)).fetchone()[0]
        transferred = db.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM chunks WHERE status = ? AND upload_status = ?", ("uploaded", "completed")).fetchone()[0]
        latest = db.execute("SELECT id, status FROM sync_jobs ORDER BY id DESC LIMIT 1").fetchone()
    return {"total_jobs": jobs[0], "completed_jobs": jobs[1], "files_uploaded": files,
            "bytes_transferred": transferred, "latest": dict(latest) if latest else None}


def job_history(path: Path) -> list[dict]:
    with read_database(path) as db:
        rows = db.execute("""
            SELECT j.*, (SELECT COUNT(*) FROM files f WHERE f.sync_job_id = j.id) AS file_count,
                (SELECT COALESCE(SUM(c.size_bytes), 0) FROM chunks c JOIN files f ON f.id = c.file_id
                 WHERE f.sync_job_id = j.id AND c.status = 'uploaded' AND c.upload_status = 'completed') AS transferred_bytes
            FROM sync_jobs j ORDER BY j.id DESC
        """).fetchall()
    return [dict(row) for row in rows]


def file_metadata(path: Path, job_id: int) -> list[dict]:
    with read_database(path) as db:
        rows = db.execute("""
            SELECT f.*, COUNT(c.id) AS chunk_count FROM files f
            LEFT JOIN chunks c ON c.file_id = f.id WHERE f.sync_job_id = ?
            GROUP BY f.id ORDER BY f.relative_path
        """, (job_id,)).fetchall()
    return [dict(row) for row in rows]


def chunk_metadata(path: Path, file_id: int) -> list[dict]:
    with read_database(path) as db:
        rows = db.execute("SELECT * FROM chunks WHERE file_id = ? ORDER BY chunk_index", (file_id,)).fetchall()
    return [dict(row) for row in rows]
