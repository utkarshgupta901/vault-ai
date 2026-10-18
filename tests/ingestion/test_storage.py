"""storage.download_to_temp never leaves a partial copy behind when the download fails, and tells an outage (retry
later) apart from a missing object (permanent). Pure unit test: the S3 client is replaced, and temp files go to a
per-test directory so leftovers can be counted."""

import tempfile
from pathlib import Path
from typing import IO

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError
from s3transfer.exceptions import RetriesExceededError

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


def _client_error(code: str, status: int) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
                       "GetObject")


class _RaisingS3:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def download_fileobj(self, bucket: str, key: str, fileobj: IO[bytes]) -> None:
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        EndpointConnectionError(endpoint_url="http://s3"),
        ConnectTimeoutError(endpoint_url="http://s3"),
        ReadTimeoutError(endpoint_url="http://s3"),
        RetriesExceededError(ReadTimeoutError(endpoint_url="http://s3")),   # s3transfer gave up mid-stream
        _client_error("InternalError", 500),
        _client_error("503", 503),                                           # HEAD responses carry no error body
        _client_error("SlowDown", 503),
        _client_error("Throttling", 400),
        _client_error("RequestTimeout", 400),
    ],
)
def test_outage_errors_become_storage_unavailable(error: Exception, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(storage, "_client", lambda: _RaisingS3(error))
    with pytest.raises(storage.StorageUnavailable) as raised:
        storage.download_to_temp("uploads/o/d.pdf", suffix=".pdf")
    assert raised.value.__cause__ is error               # the original stays attached for the logs
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(("code", "status"), [("NoSuchKey", 404), ("404", 404), ("AccessDenied", 403)])
def test_missing_or_forbidden_object_is_not_an_outage(code: str, status: int, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(storage, "_client", lambda: _RaisingS3(_client_error(code, status)))
    with pytest.raises(ClientError):                    # not retried: a later attempt would get the same answer
        storage.download_to_temp("uploads/o/d.pdf", suffix=".pdf")
