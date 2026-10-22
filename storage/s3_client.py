"""Private S3 chunk storage using the standard Boto3 credential chain."""
from contextlib import contextmanager
from pathlib import Path
import os
import re
import tempfile

import boto3
from boto3.exceptions import S3UploadFailedError, RetriesExceededError
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from core.paths import validate_key
from storage.base import StorageBackend


class S3StorageError(RuntimeError):
    """An S3 operation failed; progress must not be marked completed."""


def describe_aws_error(error: Exception) -> str:
    """Safe actionable diagnostics, without echoing credential/provider output."""
    name = type(error).__name__
    if name in {"NoCredentialsError", "PartialCredentialsError", "CredentialRetrievalError"}:
        return "AWS credentials are missing or unavailable. Configure or refresh the selected AWS profile."
    if name == "ProfileNotFound":
        return "AWS profile was not found. Check --profile and your AWS CLI configuration."
    if name in {"TokenRetrievalError", "UnauthorizedSSOTokenError", "SSOTokenLoadError"}:
        return "AWS login has expired or is unavailable. Refresh the selected profile's login."
    if isinstance(error, ClientError):
        code = error.response.get("Error", {}).get("Code", "Unknown")
        if code in {"AccessDenied", "AccessDeniedException", "403"}:
            return "S3 access denied. Check the profile permissions and bucket policy."
        if code in {"ExpiredToken", "InvalidToken", "InvalidAccessKeyId", "SignatureDoesNotMatch"}:
            return "AWS credentials were rejected or expired. Refresh the selected AWS profile."
        if code == "NoSuchBucket":
            return "S3 bucket does not exist or is unavailable. Check --bucket and --region."
        if code in {"NoSuchKey", "NotFound", "404"}:
            return "S3 object is missing. Preserve the chunk objects required by this job."
        if code in {"PermanentRedirect", "AuthorizationHeaderMalformed", "IllegalLocationConstraintException"}:
            return "S3 region does not match the bucket. Check --region."
        return "S3 request failed after bounded SDK retries. Check connectivity and bucket permissions."
    if isinstance(error, S3UploadFailedError):
        # Managed upload wraps ClientError in a safe SDK-generated summary.
        # Do not echo that summary, which can include arbitrary service text.
        if "AccessDenied" in str(error) or "(403)" in str(error):
            return "S3 upload access denied. Check s3:PutObject permissions and bucket policy."
        return "S3 upload failed after bounded SDK retries. Check credentials, permissions, and connectivity."
    if isinstance(error, RetriesExceededError):
        return "S3 download failed after bounded transfer retries. Check connectivity."
    return "AWS SDK operation failed. Check credentials, region, and connectivity."


class S3StorageBackend(StorageBackend):
    def __init__(self, bucket: str, region: str | None = None,
                 profile: str | None = None):
        # Conventional general-purpose bucket names only; no ARNs/endpoints.
        if (not isinstance(bucket, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
                or ".." in bucket or re.fullmatch(r"\d+\.\d+\.\d+\.\d+", bucket)):
            raise ValueError("Provide a valid S3 bucket name, not a URL or path")
        self.bucket = bucket
        self.profile = profile
        self.transfer_config = TransferConfig(
            num_download_attempts=3, max_concurrency=1, use_threads=False,
            preferred_transfer_client="classic",
        )
        with self._operation("initialize"):
            session = boto3.Session(profile_name=profile, region_name=region)
            self.client = session.client("s3", config=Config(
                retries={"total_max_attempts": 3, "mode": "standard"},
                connect_timeout=5, read_timeout=30, signature_version="s3v4",
            ))
            self.region = self.client.meta.region_name

    @contextmanager
    def _operation(self, operation: str):
        """Translate SDK errors at the storage boundary and propagate failure."""
        try:
            yield
        except (BotoCoreError, ClientError, S3UploadFailedError, RetriesExceededError) as error:
            raise S3StorageError(f"S3 {operation}: {describe_aws_error(error)}") from error

    def upload_chunk(self, source: Path, key: str) -> None:
        validate_key(key)
        with self._operation("upload"):
            self.client.upload_file(
                Filename=str(Path(source)), Bucket=self.bucket, Key=key,
                # No ACL or public grants: works with Bucket owner enforced.
                # Privacy also requires the bucket policy to remain private.
                ExtraArgs={"ContentType": "application/octet-stream", "ChecksumAlgorithm": "SHA256"},
                Config=self.transfer_config,
            )

    def download_chunk(self, key: str, destination: Path) -> None:
        validate_key(key)
        destination = Path(destination)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".s3-", delete=False) as output:
                temporary = Path(output.name)
            with self._operation("download"):
                try:
                    self.client.download_file(Bucket=self.bucket, Key=key,
                                              Filename=str(temporary), Config=self.transfer_config)
                except self.client.exceptions.ClientError as error:
                    if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "NotFound", "404"}:
                        raise FileNotFoundError(f"S3 chunk is missing: s3://{self.bucket}/{key}") from error
                    raise
            # Windows _commit/fsync requires a writable file descriptor.
            with temporary.open("rb+") as downloaded:
                os.fsync(downloaded.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        validate_key(key)
        with self._operation("check object"):
            try:
                self.client.head_object(Bucket=self.bucket, Key=key)
            except self.client.exceptions.ClientError as error:
                if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "NotFound", "404"}:
                    return False
                raise
        return True

    def delete_chunk(self, key: str) -> None:
        validate_key(key)
        with self._operation("delete"):
            self.client.delete_object(Bucket=self.bucket, Key=key)
