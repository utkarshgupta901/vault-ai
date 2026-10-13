"""Copy every object of the app's S3 bucket to a local folder (download) and back (upload).

    uv run python scripts/s3_sync.py download demo_snapshot/s3
    uv run python scripts/s3_sync.py upload   demo_snapshot/s3 [--bucket vault-uploads]

Used by snapshot_demo.sh / restore_demo.sh: the database dump holds every row, but the original files live in object
storage, so a snapshot needs both. Object keys become relative paths (uploads/{owner}/{document}.pdf), and each
object's Content-Type is kept in .manifest.json so a restore serves files exactly as before.

The boto3 client comes from app.services.storage, so this works against RustFS locally and R2/S3 in the cloud with
the same settings (S3_* env vars) as the app."""

import argparse
import json
import mimetypes
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.services import storage  # noqa: E402

MANIFEST = ".manifest.json"
CHUNK = 1024 * 1024


def _client() -> Any:
    return storage._client()  # the app's configured client (endpoint, path style, checksum settings)


def _target(dest: Path, key: str) -> Path:
    """Map an object key to a path under dest, refusing keys that would escape it (e.g. '../../etc/x')."""
    path = (dest / key).resolve()
    if dest.resolve() not in path.parents:
        raise ValueError(f"refusing unsafe object key {key!r}")
    return path


def download(dest: Path, bucket: str) -> int:
    s3 = _client()
    dest.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for item in page.get("Contents", []):
            key = item["Key"]
            obj = s3.get_object(Bucket=bucket, Key=key)
            path = _target(dest, key)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as f:
                for chunk in obj["Body"].iter_chunks(CHUNK):
                    f.write(chunk)
            manifest[key] = obj.get("ContentType") or "application/octet-stream"
    (dest / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return len(manifest)


def upload(src: Path, bucket: str) -> int:
    """Re-upload every file under src (overwriting same-named objects). Objects not in the snapshot are left alone:
    after a database restore nothing references them, so they are harmless orphans."""
    s3 = _client()
    manifest_path = src / MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    if bucket == settings.S3_BUCKET:
        storage.ensure_bucket()
    else:
        _ensure_bucket(s3, bucket)
    count = 0
    for path in sorted(p for p in src.rglob("*") if p.is_file() and p.name != MANIFEST):
        key = path.relative_to(src).as_posix()
        content_type = manifest.get(key) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        s3.upload_file(str(path), bucket, key, ExtraArgs={"ContentType": content_type})
        count += 1
    return count


def _ensure_bucket(s3: Any, bucket: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket)
    except Exception:  # noqa: BLE001 - botocore raises ClientError with a 404 code
        s3.create_bucket(Bucket=bucket)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Mirror the S3 bucket to/from a local folder.")
    p.add_argument("action", choices=["download", "upload"])
    p.add_argument("path", type=Path)
    p.add_argument("--bucket", default=settings.S3_BUCKET)
    args = p.parse_args(argv)
    if args.action == "download":
        n = download(args.path, args.bucket)
        print(f"downloaded {n} objects from s3://{args.bucket} to {args.path}")
    else:
        if not args.path.is_dir():
            print(f"no such directory: {args.path}", file=sys.stderr)
            return 1
        n = upload(args.path, args.bucket)
        print(f"uploaded {n} objects from {args.path} to s3://{args.bucket}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
