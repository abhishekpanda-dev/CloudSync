import sqlite3
import pytest
from database.db import connect_database, initialize_database


def add_job(db):
    return db.execute(
        "INSERT INTO sync_jobs (source_root, destination, backend) VALUES (?, ?, ?)",
        ("source", "destination", "local"),
    ).lastrowid


def add_file(db, job):
    return db.execute(
        "INSERT INTO files (sync_job_id, relative_path, size_bytes, modified_at_ns) VALUES (?, ?, ?, ?)",
        (job, "example.bin", 8, 0),
    ).lastrowid


def add_chunk(db, file, index=0, offset=0, key="jobs/1/chunk/0"):
    db.execute(
        "INSERT INTO chunks (file_id, chunk_index, offset_bytes, size_bytes, checksum_sha256, storage_key) VALUES (?, ?, ?, ?, ?, ?)",
        (file, index, offset, 8, "a" * 64, key),
    )


def test_initialization_is_repeatable_and_preserves_data(tmp_path):
    path = initialize_database(tmp_path / "nested" / "metadata.sqlite3")
    with connect_database(path) as db:
        add_job(db)
    initialize_database(path)
    with connect_database(path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = ?", ("table",))}
        assert {"files", "chunks", "sync_jobs"} <= tables
        assert db.execute("SELECT COUNT(*) FROM sync_jobs").fetchone()[0] == 1
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_foreign_keys_and_cascade(tmp_path):
    path = initialize_database(tmp_path / "test.sqlite3")
    with connect_database(path) as db:
        with pytest.raises(sqlite3.IntegrityError):
            add_file(db, 999)
        job = add_job(db)
        file = add_file(db, job)
        with pytest.raises(sqlite3.IntegrityError):
            add_chunk(db, 999)
        add_chunk(db, file)
        db.execute("DELETE FROM sync_jobs WHERE id = ?", (job,))
        assert db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0


def test_unique_constraints(tmp_path):
    path = initialize_database(tmp_path / "test.sqlite3")
    with connect_database(path) as db:
        job = add_job(db)
        file = add_file(db, job)
        with pytest.raises(sqlite3.IntegrityError):
            add_file(db, job)
        add_chunk(db, file)
        for index, offset, key in [(0, 8, "other"), (1, 0, "other"), (1, 8, "jobs/1/chunk/0")]:
            with pytest.raises(sqlite3.IntegrityError):
                add_chunk(db, file, index, offset, key)
        add_file(db, add_job(db))  # Same path is valid in a different full-copy job.


def test_invalid_metadata_and_rollback(tmp_path):
    path = initialize_database(tmp_path / "test.sqlite3")
    with pytest.raises(sqlite3.IntegrityError):
        with connect_database(path) as db:
            add_job(db)
            db.execute("INSERT INTO sync_jobs (source_root, destination, backend) VALUES (?, ?, ?)", ("a", "b", "invalid"))
    with connect_database(path) as db:
        assert db.execute("SELECT COUNT(*) FROM sync_jobs").fetchone()[0] == 0
        job = add_job(db)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO files (sync_job_id, relative_path, size_bytes, modified_at_ns) VALUES (?, ?, ?, ?)", (job, "bad", -1, 0))
