# CloudSync

A university project using Python 3.11, Streamlit, SQLite, Boto3, and pytest.

Phase 1 will perform full-copy file synchronization: scan every file in a source
folder, split files into fixed-size binary chunks, record metadata in SQLite,
store chunks in a second local folder or AWS S3, and show upload progress.
Phase 2 will add incremental synchronization later.

The Phase 1 engine scans files, streams 1 MiB binary chunks, stores chunk
objects and SQLite metadata, and restores verified completed versions. The
The Streamlit dashboard provides sync setup, live upload progress, persisted
metrics/history, file/chunk metadata, and verified restore. Local and AWS S3
backends support full-copy sync and restore. Incremental synchronization is future work.

## Structure

```text
app.py                 Streamlit entry point
core/scanner.py        Recursive file metadata scanning
core/chunker.py        Streaming fixed-size binary chunks
core/hasher.py         Reusable SHA-256 helpers
core/sync_engine.py    Shared full-copy local/S3 synchronization
core/restore.py        Verified restoration without overwriting
core/cli.py            Local sync and restore commands
core/paths.py          Shared path safety rules
storage/base.py        Abstract storage backend contract
storage/local_client.py Atomic local chunk storage
storage/s3_client.py   Private S3 uploads and atomic staged downloads
database/db.py         Connection lifecycle and schema initialization
database/schema.sql    SQLite tables and constraints
ui/dashboard.py        Five-page Phase 1 dashboard
ui/data.py             Read-only metadata queries and actual metrics
ui/operations.py       Rerun-safe background operation manager
.streamlit/config.toml Light theme with the CloudSync blue accent
core/progress.py       Optional immutable engine progress events
ui/welcome.py          Retained original welcome component
tests/                 Isolated database and UI checks
data/                  Default runtime database location (ignored)
```

## Run in PowerShell

Use the existing environment at `C:\Users\KIIT\cloudsync\.venv`.
Do not create another environment or reinstall Python. Dependencies are already
installed; `requirements.txt` records their validated versions. SQLite and
pathlib are part of Python's standard library.

```powershell
Set-Location C:\Users\KIIT\cloudsync
& .\.venv\Scripts\python.exe -m database.db
& .\.venv\Scripts\python.exe -m streamlit run app.py
```

Open the local URL printed by Streamlit. Stop the server with Ctrl+C.
The dashboard initializes missing tables and applies the existing additive
migrations without deleting data. Opening the dashboard does not synchronize,
restore, create AWS sessions, or contact AWS. CLI commands also initialize the
database as needed.

```powershell
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m pip check
```

## Database design

`sync_jobs` records source, destination, backend, timestamps, and job status.
`files` belongs to a job and records relative paths, size, modification time in
nanoseconds, and an optional SHA-256 checksum. `chunks` belongs to a file and
records zero-based chunk index, byte offset, length, SHA-256 checksum, storage
key, and upload status. Empty files will have no chunks.

Paths are unique within a job; chunk indices and offsets are unique within a
file; storage keys are globally unique. Future storage keys should include the
job and file identity. Foreign keys are enabled on every managed connection.
Deleting a job cascades to its files and chunks. Check constraints reject invalid
sizes, indices, and statuses. Initialization is repeatable and preserves data.
An additive, transactional migration adds `files.status`, `files.error_message`,
and `chunks.upload_status`; it retains all original tables and constraints.
The S3 integration adds nullable `sync_jobs.s3_region`; local jobs remain valid.
S3 jobs store their bucket as `s3://bucket` in the existing destination field.
Legacy uploaded chunks become completed, but legacy files stay pending because
the foundation never recorded verified completed file versions.

Use `connect_database(Path(...))` for application queries. It commits on success,
rolls back on error, and closes the connection. Bind user values with `?`
placeholders, as demonstrated in the tests. The initializer executes the
bundled static schema script and additive migrations. Filesystem paths use `pathlib.Path`.

## Extension points and configuration

`LocalStorageBackend` implements the existing `StorageBackend` interface. Local
uploads stage a temporary file in the object directory and atomically replace
the object after flushing it. `S3StorageBackend` implements the same contract
using managed Boto3 transfers. Both reuse the same sync and verified restore
pipeline. Errors propagate to the sync service and failed jobs stay unavailable.

`.env.example` documents reference settings. The CLI does not load `.env`
automatically; the default database is anchored to
the project's `data/` folder, and callers can pass a different Path explicitly.
Do not store AWS credentials in source code. Boto3 uses
its standard credential provider chain, including named profiles. The ignore rules exclude environments,
secrets, SQLite files, caches, and temporary test files.

Tests use pytest temporary directories and Streamlit's local AppTest runner;
they perform local uploads/restores and mock S3 operations. The tests require no
AWS account, credentials, network, or billable requests. S3 tests mock session
creation and install a guard against accidental SDK HTTP requests.

## Manual local synchronization and restore

Create `C:\Users\KIIT\CloudSyncTest\Folder_A` and put your test files in it.
Include a text file, a binary file, an empty file, and files inside subfolders.
The commands create the storage and restore folders if needed. No dashboard
or environment-variable loading is required; command-line arguments configure
the engine. The database defaults to the project's `data/cloudsync.sqlite3`.

```powershell
Set-Location C:\Users\KIIT\cloudsync
& .\.venv\Scripts\python.exe -m core.cli sync --source "C:\Users\KIIT\CloudSyncTest\Folder_A" --destination "C:\Users\KIIT\CloudSyncTest\Cloud_Storage"
```

The command prints `Completed sync job N`. Restore that exact job by replacing
`N` below with the printed number:

```powershell
& .\.venv\Scripts\python.exe -m core.cli restore --job-id N --destination "C:\Users\KIIT\CloudSyncTest\Restored"
```

Every sync creates a fresh full copy, including unchanged files. Keys contain a
job ID, random namespace, file ID, and chunk index; versions cannot collide even
if a database is recreated. Default chunk size is 1,048,576 bytes; use
`--chunk-size` to override it. A 3 MiB + 500 byte file produces four chunks
(1 MiB, 1 MiB, 1 MiB, 500 bytes). Empty files have zero chunks and the SHA-256
hash of an empty byte string. File hashes are computed from the same stream
used to generate the stored chunks.

Jobs use the original `pending/running/completed/failed` status constraint;
`running` represents the uploading stage. File status and chunk upload status
use `pending/uploading/completed/failed`. The original chunk `status` also stays
in sync (`pending/uploaded/failed`). Metadata changes commit in short transactions.
An upload failure marks the active file/chunk and the job failed. Successfully
written objects from failed jobs may remain on disk; there is no cleanup or
resume feature yet. Interrupted processes may leave running/uploading metadata,
which restore refuses. Restore accepts only wholly completed jobs and files.

Restore checks chunk order, offsets, lengths, hashes, and final file size/hash.
Each file is staged beside its target and published using an atomic hard link
that refuses existing paths (supported by NTFS). Publication on a filesystem
without hard links fails safely. A later failure can leave earlier verified
files in place; restoration is atomic per file, not for the whole directory.
Choose a fresh restore folder for each version; there is no overwrite option.
Restoration preserves relative paths and contents, not original modification
times, permissions, empty directories, or other filesystem attributes.

Source and storage folders must not overlap. The source database and its SQLite
sidecars are excluded from scanning. Symlinks and Windows reparse points are
ignored during scanning and rejected in stored/restored object paths. Restore
folders must be separate from the original source, storage, and database.
Traversal, absolute paths, alternate data streams, and reserved Windows names
are rejected. Use trusted local folders: another process must not replace path
components with links during operations. Source files must remain stable during
sync; detected size/timestamp/identity changes fail the job. This is a full-copy
tool, not a filesystem snapshot service.

To compare a manually restored file with its source:

```powershell
Get-FileHash "C:\Users\KIIT\CloudSyncTest\Folder_A\example.bin" -Algorithm SHA256
Get-FileHash "C:\Users\KIIT\CloudSyncTest\Restored\example.bin" -Algorithm SHA256
& .\.venv\Scripts\python.exe -m pytest -q
```

Keep the SQLite database with the chunk store: restoration needs both. Do not
delete job rows casually, because that deletes metadata needed for restoration.

## Manual AWS S3 full-copy test

These commands make real AWS requests. They are provided for manual execution;
development validation does not run them. Use small files in the source folder
for the initial test. Each repeat uploads every chunk again under new job/file
keys. There is no incremental sync or reuse of previous objects.

```powershell
Set-Location C:\Users\KIIT\cloudsync
& .\.venv\Scripts\python.exe -m core.cli sync --backend s3 --source "C:\Users\KIIT\CloudSyncTest\Folder_A" --bucket "cloudsync-dev-2026-431223004207-ap-southeast-2-an" --region "ap-southeast-2" --profile "cloudsync-dev"
```

Copy the printed completed job number, then restore to a fresh folder:

```powershell
$jobId = Read-Host "Enter the completed S3 sync job number"
& .\.venv\Scripts\python.exe -m core.cli restore --backend s3 --job-id $jobId --destination "C:\Users\KIIT\CloudSyncTest\Restored_S3" --bucket "cloudsync-dev-2026-431223004207-ap-southeast-2-an" --region "ap-southeast-2" --profile "cloudsync-dev"
```

Restore infers the backend, bucket, and region from SQLite when their flags are
omitted. Explicit flags must match the saved job. Profile selection is supplied
at execution time and is not stored with the version; use `--profile cloudsync-dev`
for the configured profile. Without it, Boto3 follows its normal profile and
credential configuration. The CLI never accepts secret keys or writes credentials.
Missing profiles/credentials, access denial, expired login, upload failures,
and missing chunks produce clear failure messages and nonzero exit codes.

The profile needs `s3:PutObject` and `s3:GetObject` on the bucket's `jobs/*`
objects. `s3:ListBucket` permission on the bucket lets S3 distinguish missing
objects from access denial; without it a missing object can appear as a 403.
The backend's delete method needs `s3:DeleteObject` if explicitly used. Default
1 MiB chunks use single-part transfers. Larger custom chunks can use multipart
transfers, which may also need `s3:AbortMultipartUpload` for failure cleanup.
No bucket, policies, ACL settings, or credentials are created or modified by
the CLI. Keep the bucket private with S3 Block Public Access and a private
bucket policy. Uploads set no ACL or public grants, so they support Bucket owner
enforced Object Ownership. A pre-existing public bucket policy can still expose
objects; the CLI does not audit bucket policy. Existing bucket encryption settings
apply. KMS-encrypted buckets can require additional KMS permissions.

The backend uses standard SDK retries with three total attempts per API request,
5-second connection and 30-second read timeouts. Managed download streaming
retries are limited to three attempts as well; these are separate retry layers,
not a three-attempt limit for an entire multipart transfer. The classic transfer
manager runs without transfer threads. Downloads stage a temporary local file
and replace the supplied staging target only after success. Final restoration
keeps the existing SHA-256 verification and atomic, no-overwrite publication.
ETags are not used as file or chunk hashes.

Keep the local SQLite database together with the S3 objects: metadata is not
uploaded to S3. Objects from failed jobs can remain in the bucket; the CLI does
not delete job objects or implement resume/cleanup. Mocked tests verify configuration
and failure handling; they do not verify actual AWS permissions or bucket settings.

Compare a manually restored file, replacing `example.bin` with your filename:

```powershell
Get-FileHash "C:\Users\KIIT\CloudSyncTest\Folder_A\example.bin" -Algorithm SHA256
Get-FileHash "C:\Users\KIIT\CloudSyncTest\Restored_S3\example.bin" -Algorithm SHA256
```

SDK reference: https://docs.aws.amazon.com/boto3/latest/guide/s3.html

## Phase 1 Streamlit dashboard

Launch using the existing Python environment:

```powershell
Set-Location C:\Users\KIIT\cloudsync
& .\.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501
```

Open `http://127.0.0.1:8501` in your browser. Stop the server with Ctrl+C.

The sidebar contains five pages:

- **Dashboard:** actual total jobs, completed jobs, uploaded file versions,
  successfully stored chunk bytes, latest job status, and recent activity.
- **New Sync:** source validation, local/S3 backend selection, destination or
  bucket/region/profile fields, and 1/2/4/8/16 MiB chunk sizes (default 1 MiB).
  Only clicking **Start Sync** begins a new full copy.
- **Job History:** SQLite history, status filters, source/destination details,
  and persisted errors. Pending/running jobs are never displayed as completed.
- **File Metadata:** select a job and file to inspect relative paths, byte sizes,
  modification timestamps, file hashes, chunk counts, and ordered chunk details.
- **Restore:** select a completed local or S3 job and a separate destination.
  Only clicking **Restore Files** starts restoration; existing files are refused.

For a local manual UI test, use these fields on **New Sync**:

```text
Source folder: C:\Users\KIIT\CloudSyncTest\Folder_A
Storage backend: Local folder
Local destination folder: C:\Users\KIIT\CloudSyncTest\Cloud_Storage
Chunk size: 1 MiB
```

Click **Start Sync**. Inspect the current file, zero-based chunk index, uploaded
chunk count, transferred bytes, and final result. Small jobs may finish before
the first refresh; a larger binary file makes intermediate progress easier to
see. Check **Job History** and **File Metadata**, then choose that completed job
on **Restore** and use a fresh `C:\Users\KIIT\CloudSyncTest\Restored_Dashboard`
folder. Click **Restore Files**, then compare hashes, substituting your filename:

```powershell
Get-FileHash "C:\Users\KIIT\CloudSyncTest\Folder_A\sample.bin" -Algorithm SHA256
Get-FileHash "C:\Users\KIIT\CloudSyncTest\Restored_Dashboard\sample.bin" -Algorithm SHA256
```

For an explicit real S3 UI test, select **AWS S3** on **New Sync** and use:

```text
S3 bucket: cloudsync-dev-2026-431223004207-ap-southeast-2-an
AWS region: ap-southeast-2
AWS profile: cloudsync-dev
```

Clicking **Start Sync** uploads real objects. Existing completed S3 jobs can also
be selected for restoration; choose a new folder such as
`C:\Users\KIIT\CloudSyncTest\Restored_S3_Dashboard` and click **Restore Files**.
The dashboard never requests secret access keys. Profiles and credentials remain
in the standard SDK credential chain. Viewing S3 history/metadata never contacts S3.

Dashboard defaults can be configured with PowerShell environment variables before
starting Streamlit. Relative database paths resolve from the project folder:

```powershell
$env:CLOUDSYNC_DATABASE_PATH = "C:\Users\KIIT\cloudsync\data\cloudsync.sqlite3"
$env:CLOUDSYNC_SOURCE_DIR = "C:\Users\KIIT\CloudSyncTest\Folder_A"
$env:CLOUDSYNC_LOCAL_DESTINATION = "C:\Users\KIIT\CloudSyncTest\Cloud_Storage"
$env:CLOUDSYNC_S3_BUCKET = "cloudsync-dev-2026-431223004207-ap-southeast-2-an"
$env:AWS_DEFAULT_REGION = "ap-southeast-2"
$env:AWS_PROFILE = "cloudsync-dev"
& .\.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501
```

The `.env.example` remains a reference; `.env` is not loaded automatically.

### Progress, execution, and metric definitions

The UI calls the existing sync/restore services. Optional immutable sync events
are emitted after the corresponding metadata transaction commits. Byte/chunk
counts advance only after successful uploads, and 100% is displayed only after
the job is persisted as completed. Observer failures do not alter sync outcomes.
Zero-byte jobs use completed file counts for intermediate progress.

A process-local worker manager runs operations outside Streamlit's script thread.
Workers never call Streamlit or share SQLite connections. A session remembers
its operation ID; forms and reruns do not resubmit an active job. A lock prevents
multiple dashboard operations on the same database across browser sessions in
this server process. A Streamlit fragment polls live progress every 0.5 seconds;
navigation remains usable. Completed/failed sync results persist in SQLite.
Each new explicit **Start Sync** submission creates a separate full copy.

This is a single-server dashboard for trusted local use. It does not coordinate
locks with separate CLI processes or other server instances; avoid launching
simultaneous operations against the same database from those processes. Restarting
the server loses live operation handles; jobs interrupted by shutdown can retain
pending/running metadata and cannot be restored. The dashboard does not resume,
cancel, or automatically restart them. Restore outcomes are shown in the current
session; sync history is the durable database record.

Metrics count persisted completed file versions and completed/uploaded chunk
bytes across all jobs, including successful portions of a later-failed job.
Repeated full copies count again. These are logical payload bytes confirmed by
the engine, not network retransmissions or AWS billing totals. Empty files count
as uploaded file versions but contribute no bytes. SQLite timestamps are UTC.

### Screenshots for the university project

After a manual test, capture these browser views at a desktop width:

1. **Dashboard** with persisted metrics and completed job status.
2. **New Sync** with an active larger-file upload and genuine progress.
3. **Job History** showing completed jobs and backend selection.
4. **File Metadata** showing a file hash and its chunk index/offset/size/hash table.
5. **Restore** with the verified success message.

Use Windows Snipping Tool (`Win+Shift+S`) and save screenshots with descriptive
names for your report. Crop unrelated windows and account details. Do not include
credential files or secret keys. Screenshots should show actual job results;
development tests do not fabricate or perform real S3 uploads.

Run automated validation with the existing environment:

```powershell
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m pip check
```

UI tests use Streamlit AppTest, temporary databases, mocked local/S3 operations,
and an AWS network/session guard. They cover submission, reruns, concurrency,
committed progress, failures, metadata, and restoration.

Streamlit fragments: https://docs.streamlit.io/develop/concepts/architecture/fragments
