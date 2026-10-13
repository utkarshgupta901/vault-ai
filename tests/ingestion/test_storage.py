"""storage.download_to_temp never leaves a partial copy behind when the download fails. Pure unit test: the S3
client is replaced, and temp files go to a per-test directory so leftovers can be counted."""

import tempfile
from pathlib import Path
from typing import IO

import pytest
from botocore.exceptions import ClientError

from app.services import storage


class _FailingS3:
    """Writes part of the object, then fails the way a missing key / dropped connection does."""

    def download_fileobj(self, bucket: str, key: str, fileobj: IO[bytes]) -> None:
        fileobj.write(b"%PDF-1.7 partial bytes")
        raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")


class _WorkingS3:
    def download_fileobj(self, bucket: str, key: str, fileobj: IO[bytes]) -> None:
        fileobj.write(b"hello")


def test_failed_download_deletes_its_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(storage, "_client", _FailingS3)
    with pytest.raises(ClientError):
        storage.download_to_temp("uploads/o/d.pdf", suffix=".pdf")
    assert list(tmp_path.iterdir()) == []


def test_successful_download_returns_the_file_for_the_caller_to_delete(tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(storage, "_client", _WorkingS3)
    path = storage.download_to_temp("uploads/o/d.txt", suffix=".txt")
    assert path.parent == tmp_path and path.suffix == ".txt" and path.read_bytes() == b"hello"
