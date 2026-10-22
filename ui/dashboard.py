"""Phase 1 dashboard: views of SQLite metadata and explicit engine actions."""
from dataclasses import dataclass
from pathlib import Path
import os
import sqlite3

import streamlit as st

from core.paths import is_link, reject_overlap
from core.restore import restore_job
from core.sync_engine import sync_local, sync_s3
from database.db import DEFAULT_DATABASE_PATH, initialize_database
from ui.data import summary, job_history, file_metadata, chunk_metadata
from ui.operations import MANAGER

PAGES = ["Dashboard", "New Sync", "Job History", "File Metadata", "Restore"]
STATUS_COLORS = {"pending": "gray", "running": "blue", "completed": "green", "failed": "red"}


@dataclass(frozen=True)
class Settings:
    database: Path
    source: str
    local_destination: str
    bucket: str
    region: str
    profile: str


def settings() -> Settings:
    path = Path(os.environ.get("CLOUDSYNC_DATABASE_PATH", str(DEFAULT_DATABASE_PATH)))
    if not path.is_absolute():
        path = DEFAULT_DATABASE_PATH.parents[1] / path
    return Settings(path.resolve(),
                    os.environ.get("CLOUDSYNC_SOURCE_DIR", r"C:\Users\KIIT\CloudSyncTest\Folder_A"),
                    os.environ.get("CLOUDSYNC_LOCAL_DESTINATION", r"C:\Users\KIIT\CloudSyncTest\Cloud_Storage"),
                    os.environ.get("CLOUDSYNC_S3_BUCKET", "cloudsync-dev-2026-431223004207-ap-southeast-2-an"),
                    os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-2"),
                    os.environ.get("AWS_PROFILE", "cloudsync-dev"))


def format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{int(size):,} {unit}" if unit == "B" else f"{size:,.2f} {unit}"
        size /= 1024
    return str(value)


def status_label(status: str) -> str:
    return f":{STATUS_COLORS.get(status, 'gray')}[{status.title()}]"


def validate_sync(source_text: str, destination_text: str | None, database: Path) -> Path:
    if not source_text.strip():
        raise ValueError("Enter a source folder")
    source = Path(source_text.strip())
    if not source.is_dir() or is_link(source):
        raise ValueError("Source must be an existing folder, not a symbolic link or junction")
    if destination_text is not None:
        if not destination_text.strip():
            raise ValueError("Enter a local destination folder")
        destination = Path(destination_text.strip())
        reject_overlap(source, destination)
        if destination.exists() and (not destination.is_dir() or is_link(destination)):
            raise ValueError("Local destination must be a folder, not a link")
        if database.resolve().is_relative_to(destination.resolve()):
            raise ValueError("Local destination must not contain the application database")
    return source.resolve()


def draw_metrics(database: Path) -> None:
    values = summary(database)
    latest = values["latest"]
    columns = st.columns(5)
    for column, label, value in zip(columns, ["Total sync jobs", "Completed jobs", "Files uploaded", "Bytes transferred", "Latest job status"],
                                     [values["total_jobs"], values["completed_jobs"], values["files_uploaded"], format_bytes(values["bytes_transferred"]),
                                      latest["status"].title() if latest else "No jobs"]):
        with column:
            st.metric(label, value, border=True)
    st.caption(f"Uploaded file versions and successfully stored chunk bytes, counted from SQLite across all jobs. Exact bytes: {values['bytes_transferred']:,}. Repeated full copies count again.")


def draw_job_table(jobs: list[dict]) -> None:
    if not jobs:
        st.info("No sync jobs yet. Start your first full copy from New Sync.")
        return
    st.dataframe([{"Job": j["id"], "Status": j["status"], "Backend": j["backend"],
                   "Files": j["file_count"], "Transferred bytes": j["transferred_bytes"],
                   "Created (UTC)": j["created_at"], "Completed (UTC)": j["completed_at"],
                   "Source": j["source_root"], "Destination": j["destination"]} for j in jobs],
                 hide_index=True, width="stretch")


def select_job(jobs: list[dict], key: str) -> dict:
    mapping = {job["id"]: job for job in jobs}
    selected = st.selectbox("Sync job", list(mapping), key=key,
                            format_func=lambda identifier: f"Job {identifier} | {mapping[identifier]['backend'].upper()} | {mapping[identifier]['status'].title()}")
    return mapping[selected]


def draw_dashboard(database: Path) -> None:
    draw_metrics(database)
    jobs = job_history(database)
    st.divider()
    st.header("Your synchronization activity")
    counts = {state: sum(j["status"] == state for j in jobs) for state in STATUS_COLORS}
    columns = st.columns(4)
    for column, (state, count) in zip(columns, counts.items()):
        with column:
            st.markdown(f"{status_label(state)} · **{count}**")
    st.subheader("Recent jobs")
    draw_job_table(jobs[:8])
    with st.container(border=True):
        st.markdown("**One complete copy. One verified restore.**")
        st.write("Every run copies all files into a separate job version. Restore checks SHA-256 hashes before publishing each file.")


def draw_new_sync(config: Settings, busy: bool) -> None:
    st.header("Start a new full copy")
    st.write("Choose a source and destination. Each run uploads every chunk, including unchanged files.")
    backend = st.radio("Storage backend", ["Local folder", "AWS S3"], horizontal=True, key="sync_backend", disabled=busy)
    with st.form("new_sync"):
        source = st.text_input("Source folder", value=config.source, key="source_folder", disabled=busy)
        if backend == "Local folder":
            destination = st.text_input("Local destination folder", value=config.local_destination, key="local_destination", disabled=busy)
            bucket = region = profile = None
        else:
            destination = None
            bucket = st.text_input("S3 bucket", value=config.bucket, key="s3_bucket", disabled=busy)
            left, right = st.columns(2)
            region = left.text_input("AWS region", value=config.region, key="s3_region", disabled=busy)
            profile = right.text_input("AWS profile", value=config.profile, key="s3_profile", disabled=busy)
            st.caption("Uses your configured AWS profile. Clicking Start Sync uploads real objects to this bucket.")
        chunk_mib = st.selectbox("Chunk size", [1, 2, 4, 8, 16], format_func=lambda n: f"{n} MiB", key="chunk_size", disabled=busy)
        submitted = st.form_submit_button("Start Sync", type="primary", disabled=busy, width="stretch")
    if submitted:
        try:
            source_path = validate_sync(source, destination, config.database)
            if backend == "AWS S3" and not bucket.strip():
                raise ValueError("Enter an S3 bucket")
            if backend == "Local folder":
                destination_path = Path(destination.strip())
                action = lambda callback: sync_local(source_path, destination_path, config.database, chunk_mib * 1048576, progress_callback=callback)
            else:
                action = lambda callback: sync_s3(source_path, bucket.strip(), config.database, chunk_mib * 1048576,
                                                  region=region.strip() or None, profile=profile.strip() or None,
                                                  progress_callback=callback)
            operation = MANAGER.start(config.database, "sync", action)
            st.session_state.operation_id = operation.id
            st.rerun()
        except (OSError, ValueError, RuntimeError) as error:
            st.error(str(error))


def draw_history(database: Path) -> None:
    st.header("Job history")
    jobs = job_history(database)
    state = st.selectbox("Filter by status", ["All", "pending", "running", "completed", "failed"])
    filtered = jobs if state == "All" else [j for j in jobs if j["status"] == state]
    draw_job_table(filtered)
    if filtered:
        job = select_job(filtered, "history_job")
        st.markdown(f"**Job {job['id']}** · {status_label(job['status'])}")
        st.write("Source:", job["source_root"])
        st.write("Destination:", job["destination"])
        if job["error_message"]:
            st.error(job["error_message"])
        if job["status"] in ("pending", "running"):
            st.caption("This is the persisted status. Jobs interrupted by a server or CLI shutdown can remain pending or running; they are not restorable.")


def draw_metadata(database: Path) -> None:
    st.header("File metadata")
    jobs = job_history(database)
    if not jobs:
        st.info("File metadata will appear after a sync job is created.")
        return
    job = select_job(jobs, "metadata_job")
    files = file_metadata(database, job["id"])
    if not files:
        st.info("This job has no discovered files.")
        return
    st.dataframe([{"File ID": f["id"], "Relative path": f["relative_path"], "Size (bytes)": f["size_bytes"],
                   "Modified (ns)": f["modified_at_ns"], "SHA-256": f["checksum_sha256"],
                   "Chunks": f["chunk_count"], "Upload status": f["status"]} for f in files], hide_index=True, width="stretch")
    mapping = {f["id"]: f for f in files}
    identifier = st.selectbox("File", list(mapping), key="metadata_file", format_func=lambda key: mapping[key]["relative_path"])
    file = mapping[identifier]
    st.markdown(f"**Upload status:** {status_label(file['status'])}")
    st.text(f"SHA-256: {file['checksum_sha256'] or 'Not completed'}")
    st.subheader("Chunk metadata")
    chunks = chunk_metadata(database, identifier)
    if chunks:
        st.dataframe([{"Chunk index": c["chunk_index"], "Byte offset": c["offset_bytes"], "Size (bytes)": c["size_bytes"],
                       "SHA-256": c["checksum_sha256"], "Upload status": c["upload_status"], "Storage key": c["storage_key"]} for c in chunks],
                     hide_index=True, width="stretch")
    else:
        st.info("Zero-byte files have no chunks." if file["size_bytes"] == 0 else "No chunks have been generated for this file yet.")
    if file["error_message"]:
        st.error(file["error_message"])


def draw_restore(config: Settings, busy: bool) -> None:
    st.header("Restore a verified version")
    jobs = [j for j in job_history(config.database) if j["status"] == "completed"]
    if not jobs:
        st.info("A completed sync job is required before restoration.")
        return
    job = select_job(jobs, "restore_job")
    with st.container(border=True):
        st.write(f"Backend: {'AWS S3' if job['backend'] == 's3' else 'Local folder'}")
        st.write("Storage:", job["destination"])
        if job["backend"] == "s3":
            st.write("Region:", job["s3_region"] or "AWS configured default")
        st.write(f"Files recorded: {job['file_count']}")
    with st.form("restore_form"):
        destination = st.text_input("Restore destination folder", value=r"C:\Users\KIIT\CloudSyncTest\Restored_Dashboard", key="restore_destination", disabled=busy)
        profile = st.text_input("AWS profile for restore", value=config.profile, disabled=busy) if job["backend"] == "s3" else None
        st.caption("Choose a separate folder. Existing files are never overwritten. S3 restoration downloads real objects only when you click Restore Files.")
        submitted = st.form_submit_button("Restore Files", type="primary", disabled=busy, width="stretch")
    if submitted:
        try:
            if not destination.strip():
                raise ValueError("Enter a restore destination folder")
            target = Path(destination.strip())
            reject_overlap(target, Path(job["source_root"]))
            if job["backend"] == "local":
                reject_overlap(target, Path(job["destination"]))
            job_id = job["id"]
            selected_profile = profile.strip() or None if profile is not None else None
            action = lambda callback: restore_job(job_id, target, config.database, profile=selected_profile)
            operation = MANAGER.start(config.database, "restore", action)
            st.session_state.operation_id = operation.id
            st.rerun()
        except (OSError, ValueError, RuntimeError) as error:
            st.error(str(error))


def draw_operation() -> None:
    task = MANAGER.get(st.session_state.get("operation_id"))
    if task is None:
        if st.session_state.get("operation_id"):
            st.info("The live operation session is unavailable. Check Job History for the persisted sync result.")
        return
    progress = task.snapshot()
    with st.container(border=True):
        st.subheader("Upload progress" if task.kind == "sync" else "Restore status")
        if progress is not None:
            st.markdown(f"**Job {progress.job_id}** · {status_label(progress.status)}")
            if progress.status == "completed":
                fraction = 1.0
            else:
                fraction = (progress.transferred_bytes / progress.total_bytes if progress.total_bytes
                            else progress.completed_files / progress.total_files if progress.total_files else 0.0)
                fraction = min(0.99, fraction)
            st.progress(fraction, text=f"{progress.transferred_bytes:,} / {progress.total_bytes:,} bytes stored successfully")
            left, right = st.columns(2)
            left.write(f"Files completed: {progress.completed_files} / {progress.total_files}")
            right.write(f"Chunks uploaded: {progress.uploaded_chunks} / {progress.total_chunks}")
            st.write("Current file:", progress.current_file or ("Finished" if progress.status == "completed" else "Scanning / preparing"))
            if progress.current_chunk is not None:
                st.caption(f"Current chunk index: {progress.current_chunk} (indexes start at zero)")
        elif not task.future.done():
            st.info("Preparing synchronization…" if task.kind == "sync" else "Restoring and verifying files…")
        if task.future.done():
            try:
                result = task.future.result()
                st.success(f"Sync job {result} completed successfully." if task.kind == "sync" else f"Restored and verified {result} files successfully.")
            except BaseException as error:
                st.error(f"{'Synchronization' if task.kind == 'sync' else 'Restoration'} failed: {error}")
            if st.session_state.get("refreshed_operation") != task.id:
                st.session_state.refreshed_operation = task.id
                st.rerun()


def show_dashboard() -> None:
    st.set_page_config(page_title="CloudSync", page_icon="☁️", layout="wide")
    st.html("""<style>
        .stApp {background: linear-gradient(135deg, #f8fafc 0%, #eef4ff 100%); color: #172033;}
        [data-testid="stSidebar"] {background: #eaf0fb;}
        [data-testid="stMetric"] {background: white; border-radius: 14px;}
        .block-container {padding-top: 2rem; padding-bottom: 3rem; max-width: 1500px;}
        h1, h2, h3 {letter-spacing: -0.03em;}
        .stButton button[kind="primary"], .stFormSubmitButton button[kind="primary"] {background: #2563eb; border-color: #2563eb; border-radius: 10px;}
        </style>""")
    config = settings()
    with st.sidebar:
        st.markdown("## CloudSync")
        st.caption("FULL-COPY SYNC · PHASE 1")
        page = st.radio("Navigation", PAGES, key="navigation", label_visibility="collapsed")
        st.divider()
        st.caption("Local and AWS S3 storage")
        st.button("Refresh metadata", width="stretch")
        with st.expander("Database"):
            st.text(str(config.database))
    st.title("CloudSync")
    st.subheader("Phase 1: Full-copy file synchronization")
    st.caption("Copy every file. Track each chunk. Restore with SHA-256 verification.")
    try:
        if st.session_state.get("initialized_database") != str(config.database):
            initialize_database(config.database)
            st.session_state.initialized_database = str(config.database)
        busy = MANAGER.busy(config.database)
        if busy:
            st.info("A dashboard operation is in progress. You can browse metadata; new sync and restore actions are temporarily disabled.")
        if page == "Dashboard":
            draw_dashboard(config.database)
        elif page == "New Sync":
            draw_new_sync(config, busy)
        elif page == "Job History":
            draw_history(config.database)
        elif page == "File Metadata":
            draw_metadata(config.database)
        else:
            draw_restore(config, busy)
        # Only the monitor reruns while active; it never starts an operation.
        monitor = st.fragment(run_every=0.5 if busy else None)(draw_operation)
        monitor()
    except (OSError, ValueError, sqlite3.Error) as error:
        st.error(f"CloudSync could not load the dashboard: {error}")
