CREATE TABLE IF NOT EXISTS sync_jobs (
    id INTEGER PRIMARY KEY,
    source_root TEXT NOT NULL,
    destination TEXT NOT NULL,
    backend TEXT NOT NULL CHECK (backend IN ('local', 's3')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    sync_job_id INTEGER NOT NULL REFERENCES sync_jobs(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    modified_at_ns INTEGER NOT NULL CHECK (modified_at_ns >= 0),
    checksum_sha256 TEXT,
    UNIQUE (sync_job_id, relative_path)
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    offset_bytes INTEGER NOT NULL CHECK (offset_bytes >= 0),
    size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
    checksum_sha256 TEXT NOT NULL,
    storage_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'uploaded', 'failed')),
    UNIQUE (file_id, chunk_index),
    UNIQUE (file_id, offset_bytes)
);
