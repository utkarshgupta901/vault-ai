"""Object storage behind the S3 API.

Locally this is RustFS (S3-compatible container); in the cloud it is Cloudflare R2 or AWS S3 — same code, different
env vars. The API and workers never share a disk: the API writes the original file, the worker downloads it.
Keys are uploads/{owner_id}/{document_id}.{ext}: per-document (not content-addressed), so deleting one document can
never race with a re-upload of the same bytes. boto3 is sync, so API code calls these via a thread."""

import tempfile
from functools import cache
from pathlib import Path
from typing import IO, Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import settings


@cache
def _client() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL or None,
        aws_access_key_id=settings.S3_ACCESS_KEY_ID,
        aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
        region_name=settings.S3_REGION,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": settings.S3_ADDRESSING_STYLE},
            # boto3 >= 1.36 sends CRC32 checksums by default; R2 and several S3-compatible stores reject them.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=3,
            read_timeout=30,
        ),
    )


def storage_key(owner_id: Any, document_id: Any, ext: str) -> str:
    return f"uploads/{owner_id}/{document_id}.{ext}"


def ensure_bucket() -> None:
    s3 = _client()
    try:
        s3.head_bucket(Bucket=settings.S3_BUCKET)
    except ClientError:
        s3.create_bucket(Bucket=settings.S3_BUCKET)


def put_file(key: str, fileobj: IO[bytes], content_type: str) -> None:
    fileobj.seek(0)
    _client().upload_fileobj(fileobj, settings.S3_BUCKET, key, ExtraArgs={"ContentType": content_type})


def download_to_temp(key: str, suffix: str = "") -> Path:
    """Worker side: stream the object to a temp file and return its path (caller deletes it).

    If the download fails (missing key, read timeout, soft time limit mid-transfer) the partial file is deleted here:
    the caller never got a path, so nobody else could. Every transient retry would otherwise leave up to 25 MB
    behind in the worker's /tmp."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    path = Path(tmp.name)
    try:
        with tmp:
            _client().download_fileobj(settings.S3_BUCKET, key, tmp)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def open_stream(key: str) -> Any:
    """Returns a botocore StreamingBody for proxying the original file to the client."""
    return _client().get_object(Bucket=settings.S3_BUCKET, Key=key)["Body"]


def delete(key: str) -> None:
    _client().delete_object(Bucket=settings.S3_BUCKET, Key=key)


def ping() -> bool:
    try:
        _client().head_bucket(Bucket=settings.S3_BUCKET)
        return True
    except Exception:  # noqa: BLE001
        return False
