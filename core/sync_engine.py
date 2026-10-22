"""Full-copy synchronization with durable progress metadata."""
import hashlib
from pathlib import Path
import tempfile
import uuid
from dataclasses import replace

from core.chunker import DEFAULT_CHUNK_SIZE, iter_chunks
from core.paths import is_link, reject_overlap
from core.scanner import scan_files
from database.db import DEFAULT_DATABASE_PATH, connect_database, initialize_database
from storage.local_client import LocalStorageBackend
from storage.base import StorageBackend
from core.progress import ProgressCallback, SyncProgress, notify


def sync_local(source: Path, destination: Path,
               database_path: Path = DEFAULT_DATABASE_PATH,
               chunk_size: int = DEFAULT_CHUNK_SIZE, *,
               progress_callback: ProgressCallback | None = None) -> int:
    """Return a completed job ID, or raise with the failed job recorded in SQLite.

    Every invocation uploads a new full copy. Files and folders must remain
    stable during scanning/uploading; detectable source changes fail the job.
    """
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size <= 0:
        raise ValueError("Chunk size must be a positive integer")
    source, destination, database_path = map(Path, (source, destination, database_path))
    if not source.is_dir() or is_link(source):
        raise ValueError("Source must be an existing directory, not a link")
    reject_overlap(source, destination)
    if database_path.resolve().is_relative_to(destination.resolve()):
        raise ValueError("Database must be outside chunk storage")
    source, destination, database_path = source.resolve(), destination.resolve(), database_path.resolve()
    backend = LocalStorageBackend(destination)
    return _sync_files(source, backend, "local", str(destination), database_path,
                       chunk_size, scan_destination=destination, progress_callback=progress_callback)


def sync_s3(source: Path, bucket: str, database_path: Path = DEFAULT_DATABASE_PATH,
            chunk_size: int = DEFAULT_CHUNK_SIZE, *, region: str | None = None,
            profile: str | None = None,
            progress_callback: ProgressCallback | None = None) -> int:
    """Upload a fresh full copy using one reusable S3 client."""
    from storage.s3_client import S3StorageBackend
    source, database_path = Path(source), Path(database_path)
    if not source.is_dir() or is_link(source):
        raise ValueError("Source must be an existing directory, not a link")
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size <= 0:
        raise ValueError("Chunk size must be a positive integer")
    backend = S3StorageBackend(bucket, region, profile)
    return _sync_files(source.resolve(), backend, "s3", f"s3://{bucket}",
                       database_path.resolve(), chunk_size, region=backend.region,
                       progress_callback=progress_callback)


def _sync_files(source: Path, backend: StorageBackend, backend_name: str,
                destination: str, database_path: Path, chunk_size: int, *,
                scan_destination: Path | None = None, region: str | None = None,
                progress_callback: ProgressCallback | None = None) -> int:
    initialize_database(database_path)
    with connect_database(database_path) as db:
        job_id = db.execute(
            "INSERT INTO sync_jobs (source_root, destination, backend, s3_region) VALUES (?, ?, ?, ?)",
            (str(source), destination, backend_name, region),
        ).lastrowid
    namespace = f"jobs/{job_id}-{uuid.uuid4().hex}"
    progress = SyncProgress(job_id, "pending")
    notify(progress_callback, progress)
    file_id = chunk_id = None
    try:
        with connect_database(database_path) as db:
            db.execute("UPDATE sync_jobs SET status = ? WHERE id = ?", ("running", job_id))
        progress = replace(progress, status="running")
        notify(progress_callback, progress)
        # Scan before uploading so all discovered files initially have pending
        # rows; this also keeps the traversal independent of temporary files.
        files = list(scan_files(source, scan_destination, database_path))
        with connect_database(database_path) as db:
            db.executemany(
                "INSERT INTO files (sync_job_id, relative_path, size_bytes, modified_at_ns) VALUES (?, ?, ?, ?)",
                [(job_id, f.relative_path, f.size_bytes, f.modified_at_ns) for f in files],
            )
        progress = replace(progress, total_files=len(files), total_bytes=sum(f.size_bytes for f in files),
                           total_chunks=sum((f.size_bytes + chunk_size - 1) // chunk_size for f in files))
        notify(progress_callback, progress)
        with tempfile.TemporaryDirectory(prefix="cloudsync-") as staging:
            staged_chunk = Path(staging) / "chunk.bin"
            for scanned in files:
                chunk_id = None
                with connect_database(database_path) as db:
                    file_id = db.execute(
                        "SELECT id FROM files WHERE sync_job_id = ? AND relative_path = ?",
                        (job_id, scanned.relative_path),
                    ).fetchone()[0]
                    db.execute("UPDATE files SET status = ? WHERE id = ?", ("uploading", file_id))
                progress = replace(progress, current_file=scanned.relative_path, current_chunk=None)
                notify(progress_callback, progress)
                before = scanned.path.stat()
                if is_link(scanned.path) or (before.st_size, before.st_mtime_ns) != (scanned.size_bytes, scanned.modified_at_ns):
                    raise OSError(f"Source changed during sync: {scanned.relative_path}")
                digest, total = hashlib.sha256(), 0
                for chunk in iter_chunks(scanned.path, chunk_size):
                    key = f"{namespace}/files/{file_id}/{chunk.index:08d}.bin"
                    with connect_database(database_path) as db:
                        chunk_id = db.execute(
                            "INSERT INTO chunks (file_id, chunk_index, offset_bytes, size_bytes, checksum_sha256, storage_key) VALUES (?, ?, ?, ?, ?, ?)",
                            (file_id, chunk.index, chunk.offset_bytes, chunk.size_bytes, chunk.checksum_sha256, key),
                        ).lastrowid
                    staged_chunk.write_bytes(chunk.data)
                    with connect_database(database_path) as db:
                        db.execute("UPDATE chunks SET upload_status = ? WHERE id = ?", ("uploading", chunk_id))
                    progress = replace(progress, current_chunk=chunk.index)
                    notify(progress_callback, progress)
                    backend.upload_chunk(staged_chunk, key)
                    with connect_database(database_path) as db:
                        db.execute("UPDATE chunks SET status = ?, upload_status = ? WHERE id = ?", ("uploaded", "completed", chunk_id))
                    progress = replace(progress, transferred_bytes=progress.transferred_bytes + chunk.size_bytes,
                                       uploaded_chunks=progress.uploaded_chunks + 1)
                    notify(progress_callback, progress)
                    digest.update(chunk.data)
                    total += chunk.size_bytes
                    chunk_id = None
                after = scanned.path.stat()
                if is_link(scanned.path) or total != scanned.size_bytes or (after.st_size, after.st_mtime_ns, after.st_ino) != (before.st_size, before.st_mtime_ns, before.st_ino):
                    raise OSError(f"Source changed during sync: {scanned.relative_path}")
                with connect_database(database_path) as db:
                    incomplete = db.execute(
                        "SELECT COUNT(*) FROM chunks WHERE file_id = ? AND upload_status != ?", (file_id, "completed")
                    ).fetchone()[0]
                    if incomplete:
                        raise RuntimeError("File has incomplete chunks")
                    db.execute("UPDATE files SET checksum_sha256 = ?, status = ? WHERE id = ?", (digest.hexdigest(), "completed", file_id))
                progress = replace(progress, completed_files=progress.completed_files + 1)
                notify(progress_callback, progress)
                file_id = None
        with connect_database(database_path) as db:
            db.execute("UPDATE sync_jobs SET status = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?", ("completed", job_id))
        progress = replace(progress, status="completed", current_file=None, current_chunk=None)
        notify(progress_callback, progress)
        return job_id
    except BaseException as error:
        with connect_database(database_path) as db:
            if chunk_id is not None:
                db.execute("UPDATE chunks SET status = ?, upload_status = ? WHERE id = ?", ("failed", "failed", chunk_id))
            if file_id is not None:
                db.execute("UPDATE files SET status = ?, error_message = ? WHERE id = ?", ("failed", str(error), file_id))
            db.execute("UPDATE sync_jobs SET status = ?, error_message = ? WHERE id = ?", ("failed", str(error), job_id))
        notify(progress_callback, replace(progress, status="failed"))
        raise
