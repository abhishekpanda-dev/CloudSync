"""Mocked S3 tests: no credentials, AWS requests, or billable operations."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest
from boto3.exceptions import S3UploadFailedError, RetriesExceededError
from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError, ProfileNotFound, EndpointConnectionError

from core.cli import main
from core.restore import restore_job
from core.sync_engine import sync_local, sync_s3
from database.db import connect_database, initialize_database
from storage.s3_client import S3StorageBackend, S3StorageError

BUCKET = "mock-cloudsync-bucket"
REGION = "ap-southeast-2"


def aws_error(code, operation="GetObject"):
    return ClientError({"Error": {"Code": code, "Message": "Do not echo service text"},
                        "ResponseMetadata": {"HTTPStatusCode": 403 if code == "AccessDenied" else 404}}, operation)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Real AWS/network request attempted during mocked tests")
    monkeypatch.setattr("botocore.httpsession.URLLib3Session.send", forbidden)


@pytest.fixture
def fake_s3(monkeypatch):
    objects = {}
    client = Mock()
    client.meta = SimpleNamespace(region_name=REGION)
    client.exceptions = SimpleNamespace(ClientError=ClientError)
    def upload(Filename, Bucket, Key, ExtraArgs, Config):
        objects[(Bucket, Key)] = Path(Filename).read_bytes()
    def download(Bucket, Key, Filename, Config):
        if (Bucket, Key) not in objects:
            raise aws_error("404", "HeadObject")
        Path(Filename).write_bytes(objects[(Bucket, Key)])
    def head(Bucket, Key):
        if (Bucket, Key) not in objects:
            raise aws_error("404", "HeadObject")
        return {"ContentLength": len(objects[(Bucket, Key)])}
    def delete(Bucket, Key):
        objects.pop((Bucket, Key), None)
    client.upload_file.side_effect = upload
    client.download_file.side_effect = download
    client.head_object.side_effect = head
    client.delete_object.side_effect = delete
    session = Mock()
    session.client.return_value = client
    constructor = Mock(return_value=session)
    monkeypatch.setattr("storage.s3_client.boto3.Session", constructor)
    return SimpleNamespace(objects=objects, client=client, session=session, constructor=constructor)


@pytest.fixture
def folders(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    return source, tmp_path / "restored", tmp_path / "db.sqlite3"


def test_session_profile_retries_and_private_upload(fake_s3, tmp_path):
    backend = S3StorageBackend(BUCKET, REGION, "cloudsync-dev")
    fake_s3.constructor.assert_called_once_with(profile_name="cloudsync-dev", region_name=REGION)
    args, kwargs = fake_s3.session.client.call_args
    assert args == ("s3",)
    config = kwargs["config"]
    assert config.retries == {"total_max_attempts": 3, "mode": "standard"}
    assert config.connect_timeout == 5 and config.read_timeout == 30
    assert config.signature_version == "s3v4"
    assert backend.transfer_config.num_download_attempts == 3
    assert backend.transfer_config.use_threads is False
    assert backend.transfer_config.preferred_transfer_client == "classic"
    source = tmp_path / "chunk"
    source.write_bytes(b"contents")
    backend.upload_chunk(source, "jobs/1/chunk.bin")
    call = fake_s3.client.upload_file.call_args.kwargs
    assert call["ExtraArgs"] == {"ContentType": "application/octet-stream", "ChecksumAlgorithm": "SHA256"}
    assert not any(key in call["ExtraArgs"] for key in ("ACL", "GrantRead", "GrantFullControl"))
    assert fake_s3.objects[(BUCKET, "jobs/1/chunk.bin")] == b"contents"


def test_default_credential_chain_uses_no_explicit_credentials(fake_s3):
    S3StorageBackend(BUCKET)
    fake_s3.constructor.assert_called_once_with(profile_name=None, region_name=None)


def test_sync_restore_nested_binary_empty_and_large_files(fake_s3, folders):
    source, restored, database = folders
    contents = {"a.txt": b"hello", "nested/bin": bytes(range(256)) * 17,
                "empty": b"", "large": b"x" * (3 * 1024 * 1024 + 500)}
    for relative, data in contents.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    job = sync_s3(source, BUCKET, database, region=REGION, profile="cloudsync-dev")
    with connect_database(database) as db:
        saved = db.execute("SELECT * FROM sync_jobs WHERE id = ?", (job,)).fetchone()
        assert saved["backend"] == "s3"
        assert saved["destination"] == f"s3://{BUCKET}"
        assert saved["s3_region"] == REGION and saved["status"] == "completed"
        assert {r[0] for r in db.execute("SELECT status FROM files")} == {"completed"}
        assert {r[0] for r in db.execute("SELECT upload_status FROM chunks")} == {"completed"}
        large = db.execute("SELECT id FROM files WHERE relative_path = ?", ("large",)).fetchone()[0]
        assert [r[0] for r in db.execute("SELECT size_bytes FROM chunks WHERE file_id = ? ORDER BY chunk_index", (large,))] == [1048576, 1048576, 1048576, 500]
    assert fake_s3.client.upload_file.call_count == 6
    assert restore_job(job, restored, database, profile="cloudsync-dev") == 4
    assert fake_s3.constructor.call_args.kwargs == {"profile_name": "cloudsync-dev", "region_name": REGION}
    for relative, data in contents.items():
        assert (restored / relative).read_bytes() == data


def test_repeated_s3_sync_is_full_copy(fake_s3, folders):
    source, restored, database = folders
    (source / "a").write_bytes(b"original")
    first = sync_s3(source, BUCKET, database, chunk_size=4, region=REGION)
    first_keys = set(fake_s3.objects)
    second = sync_s3(source, BUCKET, database, chunk_size=4, region=REGION)
    assert second != first
    assert len(fake_s3.objects) == 4 and fake_s3.client.upload_file.call_count == 4
    assert len(set(fake_s3.objects) - first_keys) == 2
    (source / "a").write_bytes(b"changed")
    restore_job(first, restored, database)
    assert (restored / "a").read_bytes() == b"original"


@pytest.mark.parametrize("damage", ["chunk", "final_hash", "missing", "path"])
def test_s3_restore_rejects_corruption(fake_s3, folders, damage):
    source, restored, database = folders
    (source / "a").write_bytes(b"original")
    job = sync_s3(source, BUCKET, database, region=REGION)
    key = next(iter(fake_s3.objects))
    if damage == "chunk":
        fake_s3.objects[key] = b"corrupted"
    elif damage == "missing":
        del fake_s3.objects[key]
    else:
        with connect_database(database) as db:
            if damage == "final_hash":
                db.execute("UPDATE files SET checksum_sha256 = ?", ("0" * 64,))
            else:
                db.execute("UPDATE files SET relative_path = ?", ("../escape",))
    with pytest.raises((ValueError, FileNotFoundError)):
        restore_job(job, restored, database)
    assert not (restored / "a").exists()
    assert not list(restored.rglob(".restore-*"))
    assert not list(restored.rglob(".s3-*"))


@pytest.mark.parametrize("operation", ["upload", "download", "exists", "delete"])
@pytest.mark.parametrize("key", ["../escape", "/absolute", "C:/drive", "a:stream", "a\\b"])
def test_s3_key_safety(fake_s3, tmp_path, operation, key):
    backend = S3StorageBackend(BUCKET, REGION)
    source = tmp_path / "a"
    source.write_bytes(b"a")
    with pytest.raises(ValueError):
        if operation == "upload":
            backend.upload_chunk(source, key)
        elif operation == "download":
            backend.download_chunk(key, tmp_path / "download")
        elif operation == "exists":
            backend.exists(key)
        else:
            backend.delete_chunk(key)
    assert fake_s3.client.method_calls == []


@pytest.mark.parametrize("bucket", ["", "s3://bucket", "../bucket", "UPPERCASE", "127.0.0.1", "a..b"])
def test_invalid_bucket_before_session(fake_s3, bucket):
    with pytest.raises(ValueError):
        S3StorageBackend(bucket, REGION)
    fake_s3.constructor.assert_not_called()


def test_missing_objects_and_delete_contract(fake_s3, tmp_path):
    backend = S3StorageBackend(BUCKET, REGION)
    assert not backend.exists("missing")
    with pytest.raises(FileNotFoundError, match="S3 chunk is missing"):
        backend.download_chunk("missing", tmp_path / "output")
    backend.delete_chunk("missing")
    path = tmp_path / "chunk"
    path.write_bytes(b"a")
    backend.upload_chunk(path, "a")
    assert backend.exists("a")
    backend.delete_chunk("a")
    assert not backend.exists("a")


@pytest.mark.parametrize("error,expected", [
    (NoCredentialsError(), "credentials are missing"),
    (PartialCredentialsError(provider="mock", cred_var="mock"), "credentials are missing"),
    (ProfileNotFound(profile="missing"), "profile was not found"),
    (aws_error("AccessDenied"), "access denied"),
    (aws_error("ExpiredToken"), "expired"),
    (aws_error("NoSuchBucket"), "bucket does not exist"),
    (EndpointConnectionError(endpoint_url="https://mock.invalid"), "connectivity"),
    (S3UploadFailedError("AccessDenied"), "access denied"),
    (S3UploadFailedError("mock failure"), "upload failed"),
], ids=["missing", "partial", "profile", "denied", "expired", "bucket", "endpoint", "wrapped-denied", "wrapped-failure"])
def test_upload_errors_are_recorded(fake_s3, folders, error, expected):
    source, restored, database = folders
    (source / "a").write_bytes(b"a")
    fake_s3.client.upload_file.side_effect = error
    with pytest.raises(S3StorageError, match=expected):
        sync_s3(source, BUCKET, database, region=REGION)
    with connect_database(database) as db:
        job = db.execute("SELECT * FROM sync_jobs").fetchone()
        assert job["status"] == "failed"
        assert expected in job["error_message"]
        assert "Do not echo" not in job["error_message"]
        assert db.execute("SELECT status FROM files").fetchone()[0] == "failed"
        assert db.execute("SELECT upload_status FROM chunks").fetchone()[0] == "failed"
    with pytest.raises(ValueError):
        restore_job(job["id"], restored, database)


def test_session_error_is_clear(fake_s3):
    fake_s3.constructor.side_effect = ProfileNotFound(profile="missing")
    with pytest.raises(S3StorageError, match="profile was not found"):
        S3StorageBackend(BUCKET, REGION, "missing")


@pytest.mark.parametrize("error,expected", [(aws_error("AccessDenied"), "access denied"),
                                            (RetriesExceededError(Exception("mock")), "bounded transfer retries")])
def test_download_failure_preserves_existing_destination(fake_s3, tmp_path, error, expected):
    backend = S3StorageBackend(BUCKET, REGION)
    output = tmp_path / "output"
    output.write_bytes(b"keep")
    def failed_download(**kwargs):
        Path(kwargs["Filename"]).write_bytes(b"partial")
        raise error
    fake_s3.client.download_file.side_effect = failed_download
    with pytest.raises(S3StorageError, match=expected):
        backend.download_chunk("a", output)
    assert output.read_bytes() == b"keep"
    assert not list(tmp_path.glob(".s3-*"))


def test_access_denied_is_not_reported_as_missing(fake_s3):
    backend = S3StorageBackend(BUCKET, REGION)
    fake_s3.client.head_object.side_effect = aws_error("403")
    with pytest.raises(S3StorageError, match="access denied"):
        backend.exists("a")


@pytest.mark.parametrize("option,value", [("backend", "local"), ("bucket", "different-bucket"), ("region", "us-east-1")])
def test_restore_rejects_backend_or_bucket_region_mismatch(fake_s3, folders, option, value):
    source, restored, database = folders
    (source / "a").write_bytes(b"a")
    job = sync_s3(source, BUCKET, database, region=REGION)
    with pytest.raises(ValueError, match="match"):
        restore_job(job, restored, database, **{option: value})
    assert fake_s3.client.download_file.call_count == 0


def test_s3_restore_atomic_overwrite_and_source_protection(fake_s3, folders):
    source, restored, database = folders
    (source / "a").write_bytes(b"a")
    job = sync_s3(source, BUCKET, database, region=REGION)
    with pytest.raises(ValueError):
        restore_job(job, source, database)
    original = fake_s3.client.download_file.side_effect
    def download(**kwargs):
        original(**kwargs)
        (restored / "a").write_bytes(b"keep")
    fake_s3.client.download_file.side_effect = download
    with pytest.raises(FileExistsError):
        restore_job(job, restored, database)
    assert (restored / "a").read_bytes() == b"keep"


def test_s3_scan_excludes_database(fake_s3, folders):
    source, _, _ = folders
    database = source / "metadata.sqlite3"
    (source / "a").write_bytes(b"a")
    job = sync_s3(source, BUCKET, database, region=REGION)
    with connect_database(database) as db:
        assert [r[0] for r in db.execute("SELECT relative_path FROM files WHERE sync_job_id = ?", (job,))] == ["a"]


def test_local_job_survives_s3_migration_and_sync(fake_s3, folders):
    source, restored, database = folders
    (source / "a").write_bytes(b"a")
    local = sync_local(source, source.parent / "storage", database)
    sync_s3(source, BUCKET, database, region=REGION)
    initialize_database(database)
    assert restore_job(local, restored, database) == 1
    assert (restored / "a").read_bytes() == b"a"
    with connect_database(database) as db:
        assert db.execute("SELECT s3_region FROM sync_jobs WHERE id = ?", (local,)).fetchone()[0] is None
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_cli_mocked_s3_sync_and_restore(fake_s3, folders, monkeypatch, capsys):
    source, restored, database = folders
    (source / "a").write_bytes(b"cli")
    options = ["--backend", "s3", "--bucket", BUCKET, "--region", REGION, "--profile", "cloudsync-dev", "--database", str(database)]
    monkeypatch.setattr(sys, "argv", ["cloudsync", "sync", "--source", str(source)] + options)
    main()
    job = int(capsys.readouterr().out.strip().split()[-1])
    monkeypatch.setattr(sys, "argv", ["cloudsync", "restore", "--job-id", str(job), "--destination", str(restored)] + options)
    main()
    assert "Restored 1 files" in capsys.readouterr().out
    assert (restored / "a").read_bytes() == b"cli"


@pytest.mark.parametrize("options", [[], ["--backend", "s3"], ["--destination", "local", "--profile", "cloudsync-dev"],
                                    ["--backend", "s3", "--bucket", BUCKET, "--destination", "local"]])
def test_cli_invalid_options_make_no_aws_calls(fake_s3, folders, monkeypatch, options):
    source, _, _ = folders
    monkeypatch.setattr(sys, "argv", ["cloudsync", "sync", "--source", str(source)] + options)
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    fake_s3.constructor.assert_not_called()
