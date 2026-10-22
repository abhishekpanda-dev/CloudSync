"""Run with python -m core.cli; local operations need no AWS connection."""
import argparse
import sqlite3
from pathlib import Path
from core.chunker import DEFAULT_CHUNK_SIZE
from core.restore import restore_job
from core.sync_engine import sync_local, sync_s3
from database.db import DEFAULT_DATABASE_PATH


def main() -> None:
    parser = argparse.ArgumentParser(description="CloudSync local/S3 full-copy sync and restore")
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", help="Upload a new full-copy local or S3 job")
    sync.add_argument("--source", type=Path, required=True)
    sync.add_argument("--destination", type=Path, help="Local chunk storage folder (local backend only)")
    sync.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    restore = commands.add_parser("restore", help="Restore a completed job without overwriting")
    restore.add_argument("--job-id", type=int, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    for command in (sync, restore):
        command.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
        command.add_argument("--backend", choices=("local", "s3"),
                             default="local" if command is sync else None)
        command.add_argument("--bucket", help="S3 bucket; restore defaults to the saved bucket")
        command.add_argument("--region", help="AWS region; restore defaults to the saved region")
        command.add_argument("--profile", help="Named AWS CLI profile; otherwise use the standard credential chain")
    args = parser.parse_args()
    try:
        if args.command == "sync":
            if args.backend == "local":
                if args.destination is None:
                    parser.error("--destination is required for local sync")
                if any(value is not None for value in (args.bucket, args.region, args.profile)):
                    parser.error("--bucket, --region, and --profile require --backend s3")
                job_id = sync_local(args.source, args.destination, args.database, args.chunk_size)
            else:
                if not args.bucket:
                    parser.error("--bucket is required for S3 sync")
                if args.destination is not None:
                    parser.error("Use --bucket instead of --destination for S3 sync")
                job_id = sync_s3(args.source, args.bucket, args.database, args.chunk_size,
                                 region=args.region, profile=args.profile)
            print(f"Completed sync job {job_id}")
        else:
            count = restore_job(args.job_id, args.destination, args.database,
                                backend=args.backend, bucket=args.bucket,
                                region=args.region, profile=args.profile)
            print(f"Restored {count} files from job {args.job_id}")
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        parser.exit(1, f"CloudSync failed: {error}\n")


if __name__ == "__main__":
    main()
