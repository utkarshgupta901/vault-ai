"""Client-controlled input on the document routes gets a 4xx, never a 500: non-Latin-1 filenames on download,
control characters in upload filenames, out-of-range page numbers, and edited pagination cursors. An upload that
fails after its file reached S3 leaves no object behind."""

import base64
import json
import uuid
from datetime import datetime
from urllib.parse import unquote

import pytest
from botocore.exceptions import ClientError
from httpx import AsyncClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.documents import content_disposition
from app.config import settings
from app.core.errors import AppError
from app.core.pagination import decode_keyset, encode_cursor
from app.services import documents as svc
from app.services import storage
from tests.conftest import DEMO, upload

TEXT = b"Larkspur Analytics handbook. Employees receive 18 PTO days per year."


# --- Content-Disposition ------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("filename", "fallback"), [
    ("Q3 report — final.txt", "Q3 report _ final.txt"),            # em dash
    ("契約書_2026.txt", "____2026.txt"),                     # CJK
    ("Résumé 2026.txt", "Resume 2026.txt"),                  # macOS NFD accents
    ("Résumé.txt", "Resume.txt"),                              # NFC accents
    ("plain-name.txt", "plain-name.txt"),
])
async def test_original_file_downloads_whatever_its_name(client: AsyncClient, filename: str, fallback: str) -> None:
    r = await upload(client, filename, TEXT + filename.encode())
    assert r.status_code == 202, r.text
    doc = r.json()["document"]
    assert doc["filename"] == filename

    r = await client.get(f"/api/v1/documents/{doc['id']}/file", headers=DEMO)

    assert r.status_code == 200, r.text
    assert r.content == TEXT + filename.encode()
    header = r.headers["content-disposition"]
    assert header.startswith(f'attachment; filename="{fallback}"; filename*=UTF-8\'\'')
    assert unquote(header.split("UTF-8''", 1)[1]) == filename        # the real name survives intact


def test_content_disposition_fallback_cannot_break_the_header() -> None:
    header = content_disposition("attachment", 'evil"name\\x\r\nSet-Cookie: a=b.pdf')
    header.encode("latin-1")                                          # what Starlette does: must not raise
    assert "\r" not in header and "\n" not in header
    fallback = header.split('filename="', 1)[1].split('";', 1)[0]
    assert '"' not in fallback and "\\" not in fallback


# --- upload filenames ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("raw", "stored"), [
    ("evil\x00name.txt", "evil_name.txt"),                     # NUL: Postgres rejects it in TEXT
    ("a\r\nb\x1b[31m\x7f\x85.txt", "a__b_[31m__.txt"),          # CR/LF, ESC, DEL, C1 NEL
    ("Q3 report — final 契約 Résumé.txt", "Q3 report — final 契約 Résumé.txt"),   # any other Unicode is kept
    ("dir/sub/x.txt", "x.txt"),
    (None, "upload"),
    ("/", "upload"),                                            # no last component at all
    ("   ", "upload"),                                          # nothing to show in the list
    (".", "upload"),
    ("..", "upload"),
    ("dir/..", "upload"),
])
def test_clean_filename(raw: str | None, stored: str) -> None:
    assert svc.clean_filename(raw) == stored


def test_clean_filename_is_capped() -> None:
    assert svc.clean_filename("x" * 300 + ".txt") == "x" * 255


async def test_control_characters_in_the_filename_are_replaced(client: AsyncClient,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """httpx percent-encodes control characters in a multipart filename, so the body is written by hand: this is
    what any other client can send. The NUL used to reach Postgres, which refuses it in TEXT: a 500."""
    monkeypatch.setattr(svc, "aenqueue_ingest", no_publish)       # the row is what matters, not the pipeline
    body = (b'--b0undary\r\nContent-Disposition: form-data; name="file"; filename="evil\x00name.txt"\r\n'
            b"Content-Type: text/plain\r\n\r\n" + TEXT + b"\r\n--b0undary--\r\n")
    r = await client.post("/api/v1/documents", content=body,
                          headers=DEMO | {"Content-Type": "multipart/form-data; boundary=b0undary"})
    assert r.status_code == 202, r.text
    assert r.json()["document"]["filename"] == "evil_name.txt"


async def no_publish(*args: object) -> None:
    pass


def object_exists(key: str) -> bool:
    try:
        storage._client().head_object(Bucket=settings.S3_BUCKET, Key=key)
    except ClientError:
        return False
    return True


async def test_a_failure_after_the_s3_put_deletes_the_object(client: AsyncClient,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """Any error between the PUT and the commit that creates the row (here a title Postgres refuses, as the NUL
    filename used to be) left the object in the bucket with nothing pointing at it."""
    keys: list[str] = []
    real_put = storage.put_file
    monkeypatch.setattr(storage, "put_file", lambda key, f, mime: (keys.append(key), real_put(key, f, mime)))
    monkeypatch.setattr(svc, "filename_title", lambda name: "bad\x00title")

    r = await upload(client, "a.txt", TEXT)

    assert r.status_code == 500
    assert len(keys) == 1 and not object_exists(keys[0])


async def test_a_connection_dropped_during_commit_keeps_the_object(client: AsyncClient,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """The one exception: the COMMIT may have landed before the connection dropped, and deleting the file of a row
    that exists would break a document the user can see. An orphaned object only costs storage."""
    keys: list[str] = []
    real_put = storage.put_file
    monkeypatch.setattr(storage, "put_file", lambda key, f, mime: (keys.append(key), real_put(key, f, mime)))

    async def dropped(self: AsyncSession) -> None:
        raise OperationalError("COMMIT", {}, Exception("server closed the connection unexpectedly"),
                               connection_invalidated=True)

    monkeypatch.setattr(AsyncSession, "commit", dropped)
    r = await upload(client, "a.txt", TEXT)
    monkeypatch.undo()

    assert r.status_code == 500
    assert len(keys) == 1 and object_exists(keys[0])
    storage.delete(keys[0])


# --- page numbers -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("page_no", ["0", "-1", "100001", "2147483648"])
async def test_out_of_range_page_number_is_422(client: AsyncClient, page_no: str) -> None:
    doc_id = (await upload(client, "a.txt", TEXT)).json()["document"]["id"]
    r = await client.get(f"/api/v1/documents/{doc_id}/pages/{page_no}", headers=DEMO)
    assert r.status_code == 422, r.text


# --- cursors ------------------------------------------------------------------------------------------------------
def _raw_cursor(payload: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


TAMPERED = [
    {"created_at": "nope", "id": "x"},
    {"created_at": 123, "id": str(uuid.uuid4())},
    {"created_at": "2026-01-01T00:00:00+00:00", "id": None},
    {"created_at": "2026-01-01T00:00:00+00:00", "id": 123},
    {"created_at": "2026-01-01T00:00:00+00:00", "id": "not-a-uuid"},
    {"created_at": ["2026-01-01"], "id": str(uuid.uuid4())},
    {},                                                                # was silently treated as page 1
    [1, 2],
]


@pytest.mark.parametrize("payload", TAMPERED)
async def test_tampered_document_cursor_is_400(client: AsyncClient, payload: object) -> None:
    r = await client.get(f"/api/v1/documents?cursor={_raw_cursor(payload)}", headers=DEMO)
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "INVALID_CURSOR"


def test_decode_keyset_converts_a_genuine_cursor() -> None:
    doc_id, ts = uuid.uuid4(), "2026-10-13T09:30:00+00:00"
    cursor = encode_cursor({"created_at": ts, "id": str(doc_id)})
    assert decode_keyset(cursor, created_at=datetime.fromisoformat, id=uuid.UUID) == \
        (datetime.fromisoformat(ts), doc_id)
    assert decode_keyset(encode_cursor({"seq": 41}), seq=int) == (41,)
    assert decode_keyset(None, id=uuid.UUID) is None and decode_keyset("", id=uuid.UUID) is None


@pytest.mark.parametrize("payload", [{"seq": None}, {"seq": 1.5}, {"seq": True}, {"seq": "x"}, {}])
def test_decode_keyset_rejects_wrong_types(payload: dict) -> None:
    with pytest.raises(AppError) as exc_info:
        decode_keyset(_raw_cursor(payload), seq=int)
    assert exc_info.value.status == 400
