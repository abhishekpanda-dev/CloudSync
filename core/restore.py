"""Verified restoration of completed job versions without overwriting files."""
import hashlib
import os
from pathlib import Path
import tempfile

from core.hasher import hash_file
from core.paths import is_link, reject_overlap, safe_path
from database.db import DEFAULT_DATABASE_PATH, connect_database
from storage.local_client import LocalStorageBackend


def restore_job(job_id: int, destination: Path,
                database_path: Path = DEFAULT_DATABASE_PATH, *,
                backend: str | None = None, bucket: str | None = None,
                region: str | None = None, profile: str | None = None) -> int:
    """Return file count. Publish each file only after all integrity checks pass.

    A later error can leave earlier verified files in place; restoration is
    atomic per file, not for the entire directory. Existing files are refused.
    """
    database_path, destination = Path(database_path), Path(destination)
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    if destination.exists() and (not destination.is_dir() or is_link(destination)):
        raise ValueError("Restore destination must be a directory, not a link")
    destination = destination.resolve()
    with connect_database(database_path) as db:
        job = db.execute("SELECT * FROM sync_jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None or job["status"] != "completed" or job["backend"] not in {"local", "s3"}:
            raise ValueError("Only completed sync jobs can be restored")
        if backend is not None and backend != job["backend"]:
            raise ValueError("Selected backend does not match the saved job")
        reject_overlap(destination, Path(job["source_root"]))
        if job["backend"] == "local":
            if any(value is not None for value in (bucket, region, profile)):
                raise ValueError("S3 options cannot be used for a local job")
            reject_overlap(destination, Path(job["destination"]))
        if database_path.resolve().is_relative_to(destination):
            raise ValueError("Restore destination must not contain the application database")
        files = db.execute("SELECT * FROM files WHERE sync_job_id = ? ORDER BY relative_path", (job_id,)).fetchall()
        targets, seen = [], set()
        for file in files:
            if file["status"] != "completed" or not file["checksum_sha256"]:
                raise ValueError("Job contains an incomplete file version")
            target = safe_path(destination, file["relative_path"])
            normalized = str(target).casefold()
            if target.exists():
                raise FileExistsError(target)
            if normalized in seen:
                raise ValueError("Conflicting restore paths")
            seen.add(normalized)
            targets.append(target)
        # Reject file/directory conflicts before publishing anything.
        for target in targets:
            if any(str(parent).casefold() in seen for parent in target.parents):
                raise ValueError("Conflicting restore paths")
        if job["backend"] == "local":
            storage_root = Path(job["destination"])
            if not storage_root.is_dir():
                raise FileNotFoundError(storage_root)
            storage_backend = LocalStorageBackend(storage_root)
        else:
            from storage.s3_client import S3StorageBackend
            if not job["destination"].startswith("s3://"):
                raise ValueError("Invalid saved S3 destination")
            saved_bucket = job["destination"][5:]
            saved_region = job["s3_region"] if "s3_region" in job.keys() else None
            if bucket is not None and bucket != saved_bucket:
                raise ValueError("Bucket does not match the saved job")
            if region is not None and saved_region is not None and region != saved_region:
                raise ValueError("Region does not match the saved job")
            storage_backend = S3StorageBackend(saved_bucket, region or saved_region, profile)
        destination.mkdir(parents=True, exist_ok=True)
        for file, target in zip(files, targets):
            chunks = db.execute("SELECT * FROM chunks WHERE file_id = ? ORDER BY chunk_index", (file["id"],)).fetchall()
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".restore-", delete=False) as output:
                    temporary = Path(output.name)
                    digest, offset = hashlib.sha256(), 0
                    with tempfile.TemporaryDirectory(prefix="cloudsync-restore-") as staging:
                        chunk_path = Path(staging) / "chunk.bin"
                        for index, chunk in enumerate(chunks):
                            if (chunk["status"] != "uploaded" or chunk["upload_status"] != "completed"
                                    or chunk["chunk_index"] != index or chunk["offset_bytes"] != offset):
                                raise ValueError("Incomplete or invalid chunk metadata")
                            storage_backend.download_chunk(chunk["storage_key"], chunk_path)
                            if chunk_path.stat().st_size != chunk["size_bytes"] or hash_file(chunk_path) != chunk["checksum_sha256"]:
                                raise ValueError("Chunk integrity verification failed")
                            with chunk_path.open("rb") as input_file:
                                while data := input_file.read(1024 * 1024):
                                    output.write(data)
                                    digest.update(data)
                            offset += chunk["size_bytes"]
                    if offset != file["size_bytes"] or digest.hexdigest() != file["checksum_sha256"]:
                        raise ValueError("File integrity verification failed")
                    output.flush()
                    os.fsync(output.fileno())
                safe_path(destination, file["relative_path"])
                # Hard-link publication is atomic and refuses an existing target,
                # including a target created after the initial preflight.
                os.link(temporary, target)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
    return len(files)
