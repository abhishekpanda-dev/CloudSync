"""Local integration tests use only pytest temporary directories."""
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from core.chunker import DEFAULT_CHUNK_SIZE, iter_chunks
from core.hasher import hash_chunk, hash_file
from core.restore import restore_job
from core.scanner import scan_files
from core.sync_engine import sync_local
from database.db import connect_database, initialize_database, SCHEMA_PATH
from storage.local_client import LocalStorageBackend


@pytest.fixture
def folders(tmp_path):
    source, storage, restored, database = (tmp_path / name for name in ("source", "storage", "restored", "metadata.sqlite3"))
    source.mkdir()
    return source, storage, restored, database


@pytest.mark.parametrize("relative,data", [
    ("hello.txt", b"Hello CloudSync!\n"),
    ("binary.bin", bytes(range(256)) * 30),
    ("empty.bin", b""),
    ("nested/deeper/text.txt", b"nested text"),
    ("large.bin", b"x" * (3 * DEFAULT_CHUNK_SIZE + 500)),
], ids=["text", "binary", "empty", "nested", "large-four-chunks"])
def test_sync_restore_roundtrip(folders, relative, data):
    source, storage, restored, database = folders
    path = source / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    job = sync_local(source, storage, database)
    with connect_database(database) as db:
        file = db.execute("SELECT * FROM files WHERE sync_job_id = ?", (job,)).fetchone()
        assert file["relative_path"] == relative
        assert file["size_bytes"] == len(data)
        assert file["modified_at_ns"] == path.stat().st_mtime_ns
        assert file["checksum_sha256"] == hashlib.sha256(data).hexdigest()
        assert file["status"] == "completed"
        chunks = db.execute("SELECT * FROM chunks WHERE file_id = ? ORDER BY chunk_index", (file["id"],)).fetchall()
        assert len(chunks) == (len(data) + DEFAULT_CHUNK_SIZE - 1) // DEFAULT_CHUNK_SIZE
        for index, chunk in enumerate(chunks):
            expected = data[index * DEFAULT_CHUNK_SIZE:(index + 1) * DEFAULT_CHUNK_SIZE]
            assert chunk["chunk_index"] == index
            assert chunk["offset_bytes"] == index * DEFAULT_CHUNK_SIZE
            assert chunk["size_bytes"] == len(expected)
            assert chunk["checksum_sha256"] == hash_chunk(expected)
            assert chunk["status"] == "uploaded"
            assert chunk["upload_status"] == "completed"
            assert (storage / chunk["storage_key"]).read_bytes() == expected
        assert db.execute("SELECT status FROM sync_jobs WHERE id = ?", (job,)).fetchone()[0] == "completed"
    assert restore_job(job, restored, database) == 1
    assert (restored / relative).read_bytes() == data


def test_multiple_nested_files_and_empty_source(folders):
    source, storage, restored, database = folders
    empty_job = sync_local(source, storage, database)
    assert restore_job(empty_job, restored, database) == 0
    for relative in ("a.txt", "one/b.bin", "one/two/empty"):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"" if path.name == "empty" else relative.encode())
    job = sync_local(source, storage, database)
    assert restore_job(job, restored, database) == 3
    for original in source.rglob("*"):
        if original.is_file():
            assert (restored / original.relative_to(source)).read_bytes() == original.read_bytes()


def test_repeated_sync_uploads_unchanged_files_and_preserves_versions(folders, monkeypatch):
    source, storage, restored, database = folders
    path = source / "a.txt"
    path.write_bytes(b"old")
    uploads = []
    original = LocalStorageBackend.upload_chunk
    def upload(self, source, key):
        uploads.append(key)
        original(self, source, key)
    monkeypatch.setattr(LocalStorageBackend, "upload_chunk", upload)
    first = sync_local(source, storage, database)
    second = sync_local(source, storage, database)
    path.write_bytes(b"new")
    third = sync_local(source, storage, database)
    assert len(set(uploads)) == len(uploads) == 3
    assert len({first, second, third}) == 3
    restore_job(first, restored, database)
    restore_job(third, restored.with_name("latest"), database)
    assert (restored / "a.txt").read_bytes() == b"old"
    assert (restored.with_name("latest") / "a.txt").read_bytes() == b"new"


def test_hash_helpers_and_chunk_boundaries(tmp_path):
    path = tmp_path / "binary"
    data = bytes(range(256)) * 20 + b"end"
    path.write_bytes(data)
    chunks = list(iter_chunks(path, 1024))
    assert b"".join(c.data for c in chunks) == data
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert [c.offset_bytes for c in chunks] == [i * 1024 for i in range(len(chunks))]
    assert chunks[-1].size_bytes == 3
    assert hash_file(path, 7) == hashlib.sha256(data).hexdigest()
    assert hash_chunk(data) == hash_file(path)
    path.write_bytes(b"")
    assert list(iter_chunks(path)) == []
    assert hash_file(path) == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize("size", [0, -1, True, 1.5])
def test_invalid_chunk_size(folders, size):
    source, storage, _, database = folders
    path = source / "a"
    path.write_bytes(b"a")
    with pytest.raises(ValueError):
        list(iter_chunks(path, size))
    with pytest.raises(ValueError):
        sync_local(source, storage, database, size)
    with pytest.raises(ValueError):
        hash_file(path, 0)


def test_scanner_excludes_database_and_sidecars(folders):
    source, storage, _, _ = folders
    database = source / "metadata.sqlite3"
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(database) + suffix).write_bytes(b"db")
    (source / "ok").write_bytes(b"ok")
    assert [f.relative_path for f in scan_files(source, storage, database)] == ["ok"]
    job = sync_local(source, storage, database.with_name("actual.sqlite3"))
    assert job > 0


@pytest.mark.parametrize("relation", ["same", "inside", "parent"])
def test_overlap_rejected(folders, relation):
    source, _, _, database = folders
    destination = {"same": source, "inside": source / "nested", "parent": source.parent}[relation]
    with pytest.raises(ValueError):
        list(scan_files(source, destination, database))
    with pytest.raises(ValueError):
        sync_local(source, destination, database)


def test_invalid_source_and_database_location(folders):
    source, storage, _, database = folders
    with pytest.raises(ValueError):
        sync_local(source / "missing", storage, database)
    with pytest.raises(ValueError):
        sync_local(source, storage, storage / "db.sqlite3")


@pytest.mark.parametrize("key", ["../escape", "/absolute", "C:/drive", "a\\b", "a/../b", "a//b", "a:stream", "CON", "a.", ""])
def test_storage_rejects_invalid_keys(tmp_path, key):
    backend = LocalStorageBackend(tmp_path / "storage")
    source = tmp_path / "chunk"
    source.write_bytes(b"chunk")
    for operation in (
        lambda: backend.upload_chunk(source, key),
        lambda: backend.download_chunk(key, tmp_path / "download"),
        lambda: backend.exists(key),
        lambda: backend.delete_chunk(key),
    ):
        with pytest.raises(ValueError):
            operation()


def test_local_storage_contract_and_atomic_failure(tmp_path, monkeypatch):
    backend = LocalStorageBackend(tmp_path / "storage")
    source = tmp_path / "chunk"
    source.write_bytes(b"original")
    backend.upload_chunk(source, "nested/chunk")
    assert backend.exists("nested/chunk")
    downloaded = tmp_path / "downloaded"
    backend.download_chunk("nested/chunk", downloaded)
    assert downloaded.read_bytes() == b"original"
    import storage.local_client as local
    def fail_replace(*args):
        raise OSError("disk failure")
    monkeypatch.setattr(local.os, "replace", fail_replace)
    source.write_bytes(b"changed")
    with pytest.raises(OSError, match="disk failure"):
        backend.upload_chunk(source, "nested/chunk")
    assert (backend.root / "nested/chunk").read_bytes() == b"original"
    assert not list(backend.root.rglob(".chunk-*"))
    backend.delete_chunk("nested/chunk")
    backend.delete_chunk("nested/chunk")
    assert not backend.exists("nested/chunk")
    with pytest.raises(FileNotFoundError):
        backend.download_chunk("missing", downloaded)


def test_failed_upload_is_not_restorable(folders, monkeypatch):
    source, storage, restored, database = folders
    (source / "a").write_bytes(b"abcdef")
    original = LocalStorageBackend.upload_chunk
    calls = 0
    def failing_upload(self, source, key):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated disk full")
        original(self, source, key)
    monkeypatch.setattr(LocalStorageBackend, "upload_chunk", failing_upload)
    with pytest.raises(OSError, match="disk full"):
        sync_local(source, storage, database, chunk_size=3)
    with connect_database(database) as db:
        job = db.execute("SELECT * FROM sync_jobs").fetchone()
        assert job["status"] == "failed"
        assert "disk full" in job["error_message"]
        assert db.execute("SELECT status FROM files").fetchone()[0] == "failed"
        assert [r[0] for r in db.execute("SELECT upload_status FROM chunks ORDER BY chunk_index")] == ["completed", "failed"]
        assert db.execute("SELECT checksum_sha256 FROM files").fetchone()[0] is None
    with pytest.raises(ValueError):
        restore_job(job["id"], restored, database)
    assert not restored.exists()


@pytest.mark.parametrize("damage", ["corrupt", "missing", "file_hash", "offset", "incomplete", "path", "missing_chunk"])
def test_restore_rejects_corruption(folders, damage):
    source, storage, restored, database = folders
    (source / "a").write_bytes(b"abcdef")
    job = sync_local(source, storage, database, chunk_size=3)
    with connect_database(database) as db:
        chunk = db.execute("SELECT * FROM chunks ORDER BY chunk_index").fetchone()
        if damage == "corrupt":
            (storage / chunk["storage_key"]).write_bytes(b"BAD")
        elif damage == "missing":
            (storage / chunk["storage_key"]).unlink()
        elif damage == "file_hash":
            db.execute("UPDATE files SET checksum_sha256 = ?", ("0" * 64,))
        elif damage == "offset":
            db.execute("UPDATE chunks SET offset_bytes = ? WHERE id = ?", (1, chunk["id"]))
        elif damage == "incomplete":
            db.execute("UPDATE chunks SET upload_status = ? WHERE id = ?", ("uploading", chunk["id"]))
        elif damage == "path":
            db.execute("UPDATE files SET relative_path = ?", ("../escape",))
        else:
            db.execute("DELETE FROM chunks WHERE id = ?", (chunk["id"],))
    with pytest.raises((ValueError, FileNotFoundError)):
        restore_job(job, restored, database)
    assert not (restored / "a").exists()
    assert not list(restored.rglob(".restore-*"))
    assert not (restored.parent / "escape").exists()


def test_restore_refuses_overwrite_and_overlapping_folders(folders):
    source, storage, restored, database = folders
    (source / "a").write_bytes(b"original")
    job = sync_local(source, storage, database)
    restored.mkdir()
    (restored / "a").write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        restore_job(job, restored, database)
    assert (restored / "a").read_bytes() == b"keep"
    for target in (source, storage, source / "child", storage.parent):
        with pytest.raises(ValueError):
            restore_job(job, target, database)
    with pytest.raises(ValueError):
        restore_job(999, restored, database)


def test_source_change_fails_job(folders, monkeypatch):
    source, storage, _, database = folders
    path = source / "a"
    path.write_bytes(b"original")
    original = LocalStorageBackend.upload_chunk
    def changing_upload(self, chunk, key):
        original(self, chunk, key)
        path.write_bytes(b"changed and longer")
    monkeypatch.setattr(LocalStorageBackend, "upload_chunk", changing_upload)
    with pytest.raises(OSError, match="Source changed"):
        sync_local(source, storage, database)
    with connect_database(database) as db:
        assert db.execute("SELECT status FROM files").fetchone()[0] == "failed"
        assert db.execute("SELECT status FROM sync_jobs").fetchone()[0] == "failed"


def test_backward_compatible_migration(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA_PATH.read_text())
        db.execute("INSERT INTO sync_jobs (source_root, destination, backend) VALUES (?, ?, ?)", ("source", "destination", "local"))
        db.execute("INSERT INTO files (sync_job_id, relative_path, size_bytes, modified_at_ns) VALUES (?, ?, ?, ?)", (1, "a", 1, 0))
        db.execute("INSERT INTO chunks (file_id, chunk_index, offset_bytes, size_bytes, checksum_sha256, storage_key, status) VALUES (?, ?, ?, ?, ?, ?, ?)", (1, 0, 0, 1, "a" * 64, "legacy-key", "uploaded"))
    initialize_database(path)
    initialize_database(path)
    with connect_database(path) as db:
        assert db.execute("SELECT status FROM files").fetchone()[0] == "pending"
        assert db.execute("SELECT upload_status FROM chunks").fetchone()[0] == "completed"
        assert db.execute("SELECT storage_key FROM chunks").fetchone()[0] == "legacy-key"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_symlinks_are_ignored_and_storage_links_rejected(folders):
    source, storage, _, database = folders
    outside = source.parent / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"secret")
    (source / "ok").write_bytes(b"ok")
    def directory_link(link):
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if sys.platform != "win32":
                raise
            # Junctions exercise reparse-point handling without requiring the
            # Windows privilege needed to create symbolic links.
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                           capture_output=True, text=True, check=True)
    directory_link(source / "linked")
    assert [f.relative_path for f in scan_files(source, storage, database)] == ["ok"]
    backend = LocalStorageBackend(storage)
    directory_link(storage / "linked")
    with pytest.raises(ValueError):
        backend.exists("linked/secret")


def test_progress_is_committed_before_upload(folders, monkeypatch):
    source, storage, _, database = folders
    (source / "a").write_bytes(b"abc")
    (source / "b").write_bytes(b"def")
    original = LocalStorageBackend.upload_chunk
    observed = []
    def upload(self, chunk, key):
        with connect_database(database) as db:
            assert db.execute("SELECT status FROM sync_jobs").fetchone()[0] == "running"
            active = db.execute("SELECT file_id, upload_status FROM chunks WHERE storage_key = ?", (key,)).fetchone()
            assert active["upload_status"] == "uploading"
            assert db.execute("SELECT status FROM files WHERE id = ?", (active["file_id"],)).fetchone()[0] == "uploading"
            observed.append([row[0] for row in db.execute("SELECT status FROM files ORDER BY id")])
        original(self, chunk, key)
    monkeypatch.setattr(LocalStorageBackend, "upload_chunk", upload)
    sync_local(source, storage, database)
    assert observed == [["uploading", "pending"], ["completed", "uploading"]]


def test_restore_publication_refuses_late_overwrite(folders, monkeypatch):
    source, storage, restored, database = folders
    (source / "a").write_bytes(b"original")
    job = sync_local(source, storage, database)
    original = LocalStorageBackend.download_chunk
    def download(self, key, target):
        original(self, key, target)
        (restored / "a").write_bytes(b"created after preflight")
    monkeypatch.setattr(LocalStorageBackend, "download_chunk", download)
    with pytest.raises(FileExistsError):
        restore_job(job, restored, database)
    assert (restored / "a").read_bytes() == b"created after preflight"
    assert not list(restored.rglob(".restore-*"))


def test_cli_sync_and_restore(folders):
    source, storage, restored, database = folders
    (source / "cli.txt").write_bytes(b"cli contents")
    project = Path(__file__).resolve().parents[1]
    sync = subprocess.run([sys.executable, "-m", "core.cli", "sync", "--source", str(source), "--destination", str(storage), "--database", str(database)], cwd=project, capture_output=True, text=True, check=True)
    job_id = int(sync.stdout.strip().split()[-1])
    restore = subprocess.run([sys.executable, "-m", "core.cli", "restore", "--job-id", str(job_id), "--destination", str(restored), "--database", str(database)], cwd=project, capture_output=True, text=True, check=True)
    assert "Restored 1 files" in restore.stdout
    assert (restored / "cli.txt").read_bytes() == b"cli contents"
