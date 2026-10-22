"""Headless dashboard tests, isolated databases, and mocked storage only."""
from pathlib import Path
from threading import Event
import pytest
from streamlit.testing.v1 import AppTest

from core.sync_engine import sync_local
from core.progress import SyncProgress
from database.db import connect_database, initialize_database
from storage.local_client import LocalStorageBackend
from storage.s3_client import S3StorageBackend
from ui.data import summary, job_history, file_metadata, chunk_metadata
from ui.operations import MANAGER, OperationManager

APP = Path(__file__).resolve().parents[1] / "app.py"


@pytest.fixture(autouse=True)
def guard_aws(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Dashboard tests must not create a real AWS session or request")
    monkeypatch.setattr("boto3.Session", forbidden)
    monkeypatch.setattr("botocore.httpsession.URLLib3Session.send", forbidden)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    source, storage, restored, database = [tmp_path / p for p in ("source", "storage", "restored", "metadata.sqlite3")]
    source.mkdir()
    (source / "a.txt").write_bytes(b"abcdef")
    monkeypatch.setenv("CLOUDSYNC_DATABASE_PATH", str(database))
    monkeypatch.setenv("CLOUDSYNC_SOURCE_DIR", str(source))
    monkeypatch.setenv("CLOUDSYNC_LOCAL_DESTINATION", str(storage))
    monkeypatch.setenv("CLOUDSYNC_S3_BUCKET", "mock-cloudsync-bucket")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-2")
    monkeypatch.setenv("AWS_PROFILE", "mock-profile")
    yield source, storage, restored, database
    # Finish worker tasks before fixture mocks disappear.
    for task in list(MANAGER.tasks.values()):
        if task.database == str(database.resolve()).casefold() and task.future is not None:
            try:
                task.future.result(timeout=15)
            except Exception:
                pass


@pytest.fixture
def mock_local(monkeypatch, tmp_path):
    objects = {}
    def upload(self, source, key):
        objects[(str(self.root), key)] = Path(source).read_bytes()
    def download(self, key, destination):
        Path(destination).write_bytes(objects[(str(self.root), key)])
    monkeypatch.setattr(LocalStorageBackend, "upload_chunk", upload)
    monkeypatch.setattr(LocalStorageBackend, "download_chunk", download)
    return objects


@pytest.fixture
def mock_s3(monkeypatch):
    objects = {}
    calls = []
    def initialize(self, bucket, region=None, profile=None):
        self.bucket, self.region = bucket, region or "ap-southeast-2"
        calls.append((bucket, region, profile))
    def upload(self, source, key):
        objects[key] = Path(source).read_bytes()
    def download(self, key, destination):
        Path(destination).write_bytes(objects[key])
    monkeypatch.setattr(S3StorageBackend, "__init__", initialize)
    monkeypatch.setattr(S3StorageBackend, "upload_chunk", upload)
    monkeypatch.setattr(S3StorageBackend, "download_chunk", download)
    return objects, calls


def start_page(page):
    app = AppTest.from_file(str(APP)).run(timeout=10)
    app.radio(key="navigation").set_value(page).run(timeout=10)
    assert not app.exception
    return app


def button(app, label):
    return next(item for item in app.button if item.label == label)


def wait_operation(app, raises=False):
    operation = MANAGER.get(app.session_state["operation_id"])
    if raises:
        with pytest.raises(Exception):
            operation.future.result(timeout=10)
    else:
        operation.future.result(timeout=10)
    app.run(timeout=10)
    assert not app.exception
    return operation


def test_empty_dashboard_metrics_and_navigation(environment):
    app = AppTest.from_file(str(APP)).run(timeout=10)
    assert not app.exception
    assert app.title[0].value == "CloudSync"
    assert [m.value for m in app.metric] == ["0", "0", "0", "0 B", "No jobs"]
    assert app.radio(key="navigation").options == ["Dashboard", "New Sync", "Job History", "File Metadata", "Restore"]
    for page in app.radio(key="navigation").options:
        app.radio(key="navigation").set_value(page).run(timeout=10)
        assert not app.exception
    assert summary(environment[3])["total_jobs"] == 0


def test_local_submit_restore_and_reruns_do_not_duplicate(environment, mock_local):
    source, storage, restored, database = environment
    app = start_page("New Sync")
    assert app.selectbox(key="chunk_size").value == 1
    button(app, "Start Sync").click().run(timeout=10)
    task = wait_operation(app)
    assert task.snapshot().status == "completed"
    assert any("completed successfully" in message.value for message in app.success)
    for _ in range(3):
        app.run(timeout=10)
    assert summary(database)["total_jobs"] == 1
    app.radio(key="navigation").set_value("Restore").run(timeout=10)
    app.text_input(key="restore_destination").set_value(str(restored))
    button(app, "Restore Files").click().run(timeout=10)
    wait_operation(app)
    assert (restored / "a.txt").read_bytes() == b"abcdef"
    assert any("Restored and verified 1 files" in message.value for message in app.success)
    app.run(timeout=10)
    assert summary(database)["total_jobs"] == 1


@pytest.mark.parametrize("invalid", ["missing", "overlap", "empty"])
def test_invalid_source_or_destination_does_not_create_jobs(environment, invalid):
    source, _, _, database = environment
    app = start_page("New Sync")
    if invalid == "missing":
        app.text_input(key="source_folder").set_value(str(source / "missing"))
    elif invalid == "overlap":
        app.text_input(key="local_destination").set_value(str(source / "nested"))
    else:
        app.text_input(key="source_folder").set_value("")
    button(app, "Start Sync").click().run(timeout=10)
    assert app.error and not app.exception
    assert summary(database)["total_jobs"] == 0


def test_ui_s3_upload_and_restore_are_explicit(environment, mock_s3):
    source, _, restored, database = environment
    objects, calls = mock_s3
    app = start_page("New Sync")
    app.radio(key="sync_backend").set_value("AWS S3").run(timeout=10)
    assert app.text_input(key="s3_bucket").value == "mock-cloudsync-bucket"
    assert not calls and not objects
    app.run(timeout=10)
    assert not calls
    button(app, "Start Sync").click().run(timeout=10)
    wait_operation(app)
    assert len(objects) == 1
    assert calls == [("mock-cloudsync-bucket", "ap-southeast-2", "mock-profile")]
    app.run(timeout=10)
    assert len(calls) == 1
    app.radio(key="navigation").set_value("Restore").run(timeout=10)
    app.text_input(key="restore_destination").set_value(str(restored))
    assert len(calls) == 1
    button(app, "Restore Files").click().run(timeout=10)
    wait_operation(app)
    assert (restored / "a.txt").read_bytes() == b"abcdef"
    assert len(calls) == 2


def test_failed_upload_persists_failure_and_ui_error(environment, monkeypatch):
    def fail(*args):
        raise OSError("Mock disk full")
    monkeypatch.setattr(LocalStorageBackend, "upload_chunk", fail)
    app = start_page("New Sync")
    button(app, "Start Sync").click().run(timeout=10)
    wait_operation(app, raises=True)
    assert any("Mock disk full" in item.value for item in app.error)
    jobs = job_history(environment[3])
    assert jobs[0]["status"] == "failed" and jobs[0]["transferred_bytes"] == 0
    app.radio(key="navigation").set_value("Restore").run(timeout=10)
    assert not app.selectbox


def test_metadata_history_and_actual_metrics(environment, mock_local):
    source, storage, _, database = environment
    job = sync_local(source, storage, database, chunk_size=3)
    assert summary(database) == {"total_jobs": 1, "completed_jobs": 1, "files_uploaded": 1,
                                 "bytes_transferred": 6, "latest": {"id": job, "status": "completed"}}
    app = start_page("Dashboard")
    assert [m.value for m in app.metric] == ["1", "1", "1", "6 B", "Completed"]
    app.radio(key="navigation").set_value("Job History").run(timeout=10)
    assert app.dataframe[0].value.iloc[0]["Transferred bytes"] == 6
    app.radio(key="navigation").set_value("File Metadata").run(timeout=10)
    assert app.dataframe[0].value.iloc[0]["Relative path"] == "a.txt"
    assert app.dataframe[0].value.iloc[0]["Chunks"] == 2
    assert list(app.dataframe[1].value["Byte offset"]) == [0, 3]
    assert len(app.dataframe[0].value.iloc[0]["SHA-256"]) == 64


def test_progress_counts_only_committed_successful_chunks(environment, monkeypatch):
    source, storage, _, database = environment
    events = []
    uploaded = 0
    def upload(self, path, key):
        nonlocal uploaded
        # Callback has reported the in-flight chunk without increasing bytes.
        assert events[-1].transferred_bytes == uploaded
        uploaded += Path(path).stat().st_size
    monkeypatch.setattr(LocalStorageBackend, "upload_chunk", upload)
    def observe(event):
        with connect_database(database) as db:
            actual = db.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM chunks WHERE upload_status = ?", ("completed",)).fetchone()[0]
            assert actual == event.transferred_bytes
            assert db.execute("SELECT status FROM sync_jobs WHERE id = ?", (event.job_id,)).fetchone()[0] == event.status
        events.append(event)
    sync_local(source, storage, database, 3, progress_callback=observe)
    assert events[0].status == "pending" and events[-1].status == "completed"
    assert events[-1].total_bytes == events[-1].transferred_bytes == 6
    assert events[-1].uploaded_chunks == events[-1].total_chunks == 2
    assert events[-1].completed_files == 1


def test_observer_failure_does_not_fail_sync(environment, mock_local):
    def broken(event):
        raise ValueError("observer only")
    job = sync_local(environment[0], environment[1], environment[3], progress_callback=broken)
    assert job_history(environment[3])[0]["id"] == job
    assert job_history(environment[3])[0]["status"] == "completed"


def test_failed_progress_does_not_count_failed_chunk(environment, monkeypatch):
    source, storage, _, database = environment
    calls, events = [], []
    def upload(self, path, key):
        calls.append(key)
        if len(calls) == 2:
            raise OSError("failed second chunk")
    monkeypatch.setattr(LocalStorageBackend, "upload_chunk", upload)
    with pytest.raises(OSError):
        sync_local(source, storage, database, 3, progress_callback=events.append)
    assert events[-1].status == "failed"
    assert events[-1].transferred_bytes == 3 and events[-1].uploaded_chunks == 1
    assert events[-1].completed_files == 0
    assert summary(database)["bytes_transferred"] == 3
    assert summary(database)["files_uploaded"] == 0


def test_operation_manager_prevents_concurrent_duplicate_database_tasks(tmp_path):
    manager = OperationManager()
    release, entered = Event(), Event()
    def action(callback):
        entered.set()
        release.wait(timeout=10)
        return 1
    try:
        task = manager.start(tmp_path / "db", "sync", action)
        assert entered.wait(timeout=5)
        assert manager.busy(tmp_path / "db")
        assert manager.get(task.id) is task
        with pytest.raises(RuntimeError, match="already running"):
            manager.start(tmp_path / "db", "sync", action)
        task.observe(SyncProgress(1, "running"))
        assert task.snapshot().status == "running"
        release.set()
        assert task.future.result(timeout=5) == 1
        assert not manager.busy(tmp_path / "db")
    finally:
        release.set()
        manager.executor.shutdown(wait=True)


def test_ui_busy_guard_and_live_inflight_progress(environment, monkeypatch):
    source, _, _, database = environment
    release, entered = Event(), Event()
    def upload(self, path, key):
        entered.set()
        release.wait(timeout=10)
    monkeypatch.setattr(LocalStorageBackend, "upload_chunk", upload)
    app = start_page("New Sync")
    try:
        button(app, "Start Sync").click().run(timeout=10)
        assert entered.wait(timeout=5)
        app.run(timeout=10)
        assert button(app, "Start Sync").disabled
        task = MANAGER.get(app.session_state["operation_id"])
        assert task.snapshot().transferred_bytes == 0
        assert task.snapshot().current_file == "a.txt"
        assert task.snapshot().current_chunk == 0
        assert task.snapshot().uploaded_chunks == 0
        assert summary(database)["total_jobs"] == 1
    finally:
        release.set()
        wait_operation(app)


def test_empty_file_and_restore_error(environment, mock_local):
    source, storage, restored, database = environment
    (source / "a.txt").write_bytes(b"")
    sync_local(source, storage, database)
    app = start_page("File Metadata")
    assert any("Zero-byte" in item.value for item in app.info)
    app.radio(key="navigation").set_value("Restore").run(timeout=10)
    restored.mkdir()
    (restored / "a.txt").write_bytes(b"keep")
    app.text_input(key="restore_destination").set_value(str(restored))
    button(app, "Restore Files").click().run(timeout=10)
    wait_operation(app, raises=True)
    assert app.error and (restored / "a.txt").read_bytes() == b"keep"


def test_status_queries_include_pending_running_failed_without_fake_success(environment):
    database = initialize_database(environment[3])
    with connect_database(database) as db:
        for state in ("pending", "running", "failed"):
            db.execute("INSERT INTO sync_jobs (source_root, destination, backend, status) VALUES (?, ?, ?, ?)",
                       (str(environment[0]), str(environment[1]), "local", state))
    stats = summary(database)
    assert stats["total_jobs"] == 3 and stats["completed_jobs"] == 0
    assert stats["files_uploaded"] == 0 and stats["bytes_transferred"] == 0
    app = start_page("Job History")
    app.selectbox[0].set_value("failed").run(timeout=10)
    assert len(app.dataframe[0].value) == 1
    assert app.dataframe[0].value.iloc[0]["Status"] == "failed"
