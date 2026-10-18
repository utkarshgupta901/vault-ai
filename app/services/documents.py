"""Document use-cases: upload (validate -> dedup -> store -> commit -> enqueue), list/get, reprocess, delete.

Every function takes owner_id; nothing here can read another owner's data."""

import hashlib
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePath
from typing import IO

import anyio
import structlog
from fastapi import UploadFile
from sqlalchemy import and_, delete, or_, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors import AppError, ErrorCode, not_found
from app.core.pagination import decode_keyset, encode_cursor
from app.core.redis import get_sync_broker
from app.core.request_context import get_request_id
from app.ingestion.sniff import sniff
from app.ingestion.titles import filename_title
from app.models import Document, DocumentPage
from app.schemas.documents import DocumentError, DocumentOut
from app.services import state, storage
from app.worker.dispatch import QueueUnavailable, aenqueue_ingest, aenqueue_insights

log = structlog.get_logger(__name__)
READ_CHUNK = 1024 * 1024


@dataclass
class UploadResult:
    document: Document
    duplicate: bool
    requeued: bool


def to_out(doc: Document) -> DocumentOut:
    now = datetime.now(UTC)
    error = (
        DocumentError(code=doc.error_code, message=doc.error_message or doc.error_code,
                      retryable=bool(doc.error_retryable))
        if doc.status == "failed" and doc.error_code else None
    )
    out = DocumentOut.model_validate(doc)
    out.error = error
    out.stale = state.is_stale(doc.status, doc.updated_at, now, settings.STALE_PROCESSING_MIN)
    return out


def clean_filename(raw: str | None) -> str:
    """The client's filename as stored and shown: its last path component, at most 255 characters, with control
    characters replaced by '_'. Postgres refuses NUL in TEXT (the upload failed as a 500), and CR/LF or ESC would
    forge or garble log lines; any other Unicode is kept as typed. A name with nothing to show ("/", "   ", "..")
    becomes "upload", like a missing one: it would otherwise list as a blank row."""
    name = PurePath(raw or "upload").name
    cleaned = "".join("_" if unicodedata.category(c) == "Cc" else c for c in name)[:255]
    return cleaned if cleaned.strip() not in ("", ".", "..") else "upload"


async def _delete_orphan(key: str) -> None:
    """Delete an object no row will ever point at. Best effort: the caller's own outcome matters more."""
    try:
        await anyio.to_thread.run_sync(storage.delete, key)
    except Exception as exc:  # noqa: BLE001
        log.warning("storage_delete_failed", key=key, error=str(exc))


def _hash_and_size(f: IO[bytes]) -> tuple[str, int]:
    f.seek(0)
    h, size = hashlib.sha256(), 0
    while block := f.read(READ_CHUNK):
        h.update(block)
        size += len(block)
    f.seek(0)
    return h.hexdigest(), size


async def _check_backpressure() -> None:
    def llen() -> int:
        return int(get_sync_broker().llen("ingest"))

    try:
        depth = await anyio.to_thread.run_sync(llen)
    except Exception:  # noqa: BLE001 - Redis down is handled at publish time
        return
    if depth > settings.QUEUE_BACKPRESSURE_DEPTH:
        raise AppError(ErrorCode.QUEUE_BACKPRESSURE, 503, "Processing queue is full; try again shortly.",
                       {"queue_depth": depth}, headers={"Retry-After": "30"})


async def _enqueue_or_fail(db: AsyncSession, doc: Document, run_id: uuid.UUID) -> None:
    """Publish the ingest task; if the broker is unavailable, fail the run as retryable (and answer 503).

    The publish can wait up to its timeout (PUBLISH_TIMEOUT_S), so the read transaction the caller's db.refresh(doc)
    opened is ended first: a pool connection must not sit idle in transaction meanwhile. expire_on_commit=False keeps
    `doc` loaded."""
    await db.commit()
    try:
        await aenqueue_ingest(doc.id, run_id, get_request_id())
    except QueueUnavailable as exc:
        log.error("enqueue_failed", document_id=str(doc.id), error=str(exc))
        await db.execute(state.fail_stmt(doc.id, run_id, ErrorCode.QUEUE_UNAVAILABLE,
                                         "Could not queue the document for processing.", retryable=True))
        await db.commit()
        raise AppError(ErrorCode.QUEUE_UNAVAILABLE, 503, "Processing queue unavailable; the upload was saved and can "
                       "be retried with POST /documents/{id}/reprocess.", {"document_id": str(doc.id)},
                       headers={"Retry-After": "10"}) from exc


async def upload(db: AsyncSession, owner_id: uuid.UUID, file: UploadFile) -> UploadResult:
    await _check_backpressure()
    f = file.file  # Starlette has already spooled the part to a temp file
    sha256, size = await anyio.to_thread.run_sync(_hash_and_size, f)
    if size == 0:
        raise AppError(ErrorCode.EMPTY_FILE, 422, "The uploaded file is empty.")
    if size > settings.max_upload_bytes:
        raise AppError(ErrorCode.FILE_TOO_LARGE, 413, f"File exceeds the {settings.MAX_UPLOAD_MB} MB limit.")
    filename = clean_filename(file.filename)
    sniffed = await anyio.to_thread.run_sync(sniff, f, filename)

    existing = await db.scalar(select(Document).where(Document.owner_id == owner_id, Document.sha256 == sha256))
    if existing is not None:
        return await _handle_duplicate(db, existing)
    await db.rollback()  # end the dedup read: its pool connection must not sit idle in transaction during the S3 PUT

    document_id, run_id = uuid.uuid4(), uuid.uuid4()
    key = storage.storage_key(owner_id, document_id, sniffed.ext)
    await anyio.to_thread.run_sync(storage.put_file, key, f, sniffed.mime_type)

    doc = Document(
        id=document_id, owner_id=owner_id, filename=filename, title=filename_title(filename),
        mime_type=sniffed.mime_type, size_bytes=size, sha256=sha256, storage_key=key,
        status="queued", current_run_id=run_id,
    )
    db.add(doc)
    try:
        await db.commit()  # commit FIRST, then publish: a task must never reference a row that doesn't exist
    except IntegrityError:
        # A concurrent upload of the same bytes won the race on UNIQUE(owner_id, sha256).
        await db.rollback()
        await _delete_orphan(key)
        winner = await db.scalar(select(Document).where(Document.owner_id == owner_id, Document.sha256 == sha256))
        if winner is None:
            raise
        return await _handle_duplicate(db, winner)
    except Exception as exc:
        # Any other failure (e.g. a value Postgres refuses): the row was not written and nothing points at the
        # object, so it would stay in the bucket forever. Kept only when the outcome is unknown, a dropped connection
        # or a cancellation (not an Exception) during COMMIT: the row may exist, and a row without its file is worse.
        # Rolled back first, so the failed session gives its pool connection back before the S3 DELETE (and retries).
        await db.rollback()
        if not (isinstance(exc, DBAPIError) and exc.connection_invalidated):
            await _delete_orphan(key)
        raise
    await db.refresh(doc)
    await _enqueue_or_fail(db, doc, run_id)
    log.info("document_uploaded", document_id=str(doc.id), mime=sniffed.mime_type, size=size)
    return UploadResult(doc, duplicate=False, requeued=False)


async def _handle_duplicate(db: AsyncSession, doc: Document) -> UploadResult:
    """Same bytes again: no new work, no new cost. Exception: a retryable failure gets a fresh run.

    `doc` was read before this call and may be stale: a concurrent re-upload or reprocess can have requeued it since.
    So the requeue repeats "failed and retryable" in its WHERE (zero rows = another request already started a run,
    which must not be replaced), and upload_count is incremented in SQL so concurrent duplicates are all counted."""
    run_id, requeued = uuid.uuid4(), False
    if doc.status == "failed" and doc.error_retryable:
        result = await db.execute(state.requeue_stmt(doc.id, run_id)
                                  .where(Document.status == "failed", Document.error_retryable.is_(True)))
        requeued = result.first() is not None
    await db.execute(update(Document).where(Document.id == doc.id).values(upload_count=Document.upload_count + 1))
    await db.commit()
    await db.refresh(doc)
    if requeued:
        await _enqueue_or_fail(db, doc, run_id)
    return UploadResult(doc, duplicate=True, requeued=requeued)


async def get_owned(db: AsyncSession, owner_id: uuid.UUID, document_id: uuid.UUID) -> Document:
    doc = await db.scalar(select(Document).where(Document.id == document_id, Document.owner_id == owner_id))
    if doc is None:
        raise not_found(ErrorCode.DOCUMENT_NOT_FOUND, "Document")
    return doc


async def list_documents(
    db: AsyncSession, owner_id: uuid.UUID, *, status: str | None, category: str | None, tag: str | None,
    q: str | None, limit: int, cursor: str | None,
) -> tuple[list[Document], str | None]:
    stmt = select(Document).where(Document.owner_id == owner_id)
    if status:
        stmt = stmt.where(Document.status == status)
    if category:
        stmt = stmt.where(Document.category == category)
    if tag:
        stmt = stmt.where(Document.tags.contains([tag.lower()]))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Document.filename.ilike(like), Document.title.ilike(like)))
    after = decode_keyset(cursor, created_at=datetime.fromisoformat, id=uuid.UUID)  # tampered -> 400, not 500
    if after:
        ts, last_id = after
        stmt = stmt.where(or_(Document.created_at < ts, and_(Document.created_at == ts, Document.id < last_id)))
    rows = list(await db.scalars(stmt.order_by(Document.created_at.desc(), Document.id.desc()).limit(limit + 1)))
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = encode_cursor({"created_at": last.created_at.isoformat(), "id": str(last.id)})
    return rows, next_cursor


async def get_page(db: AsyncSession, owner_id: uuid.UUID, document_id: uuid.UUID, page_no: int) -> DocumentPage:
    doc = await get_owned(db, owner_id, document_id)
    page = await db.get(DocumentPage, (doc.id, page_no))
    if page is None:
        if doc.status != "ready":
            raise AppError(ErrorCode.DOCUMENT_NOT_READY, 409, "Document is still processing.", {"status": doc.status})
        raise not_found(ErrorCode.PAGE_NOT_FOUND, "Page")
    return page


def _check_reprocess_allowed(doc: Document, scope: str) -> None:
    """The friendly, specific 409s. The same rules are enforced again atomically by state.reprocess_stmt, which is
    what actually protects against two requests racing; this read only chooses the message."""
    now, stale_min = datetime.now(UTC), settings.STALE_PROCESSING_MIN
    if scope == "insights":
        if doc.status != "ready":
            raise AppError(ErrorCode.DOCUMENT_NOT_READY, 409, "Insights can only be regenerated for ready documents.")
        # Insights have their own staleness: the document is 'ready' (never stale) while its insights task may
        # have been lost. Without this, a stuck 'processing' would answer 409 forever.
        if (doc.insights_status in state.INSIGHTS_IN_FLIGHT
                and not state.is_insights_stale(doc.insights_status, doc.updated_at, now, stale_min)):
            raise AppError(ErrorCode.ALREADY_PROCESSING, 409, "Insights are already being generated.",
                           {"insights_status": doc.insights_status})
    elif doc.status in state.IN_FLIGHT and not state.is_stale(doc.status, doc.updated_at, now, stale_min):
        raise AppError(ErrorCode.ALREADY_PROCESSING, 409, "Document is already being processed.",
                       {"status": doc.status, "stage": doc.stage})


async def _enqueue_insights_or_fail(db: AsyncSession, doc: Document, run_id: uuid.UUID) -> None:
    """Like _enqueue_or_fail, for an insights-only run. Without marking the run failed, insights would stay
    'pending' under a run that no task will ever claim, and every retry would be refused as already in progress.
    Ends the caller's read transaction first, for the same reason."""
    await db.commit()
    try:
        await aenqueue_insights(doc.id, run_id, get_request_id())
    except QueueUnavailable as exc:
        log.error("insights_enqueue_failed", document_id=str(doc.id), error=str(exc))
        await db.execute(state.insights_fail_stmt(doc.id, run_id, ErrorCode.QUEUE_UNAVAILABLE))
        await db.commit()
        raise AppError(ErrorCode.QUEUE_UNAVAILABLE, 503, "Processing queue unavailable; insights can be regenerated "
                       "again once it is back.", {"document_id": str(doc.id)},
                       headers={"Retry-After": "10"}) from exc


async def reprocess(db: AsyncSession, owner_id: uuid.UUID, document_id: uuid.UUID, scope: str) -> tuple[Document, uuid.UUID]:
    doc = await get_owned(db, owner_id, document_id)
    _check_reprocess_allowed(doc, scope)
    run_id = uuid.uuid4()
    requeued = await db.execute(state.reprocess_stmt(doc.id, run_id, scope=scope,
                                                     stale_minutes=settings.STALE_PROCESSING_MIN))
    if requeued.first() is None:  # a concurrent reprocess committed between our read and this UPDATE
        await db.rollback()
        raise AppError(ErrorCode.ALREADY_PROCESSING, 409, "Another request has just started processing this "
                       "document.")
    await db.commit()
    await db.refresh(doc)
    if scope == "insights":
        await _enqueue_insights_or_fail(db, doc, run_id)
    else:
        await _enqueue_or_fail(db, doc, run_id)
    return doc, run_id


_REDACT_CITATIONS = text("""
UPDATE chat_messages m
SET citations = (
  SELECT coalesce(jsonb_agg(
           CASE WHEN c->>'document_id' = CAST(:doc_id AS text)
                THEN (c - 'snippet') || '{"source_deleted": true}'::jsonb
                ELSE c END), '[]'::jsonb)
  FROM jsonb_array_elements(m.citations) c)
WHERE m.session_id IN (SELECT id FROM chat_sessions WHERE owner_id = :owner_id)
  AND m.citations @> jsonb_build_array(jsonb_build_object('document_id', CAST(:doc_id AS text)))
""")


async def delete_document(db: AsyncSession, owner_id: uuid.UUID, document_id: uuid.UUID) -> None:
    doc = await get_owned(db, owner_id, document_id)
    key = doc.storage_key
    # Same transaction: past answers keep their text, but cited snippets of the deleted file are redacted.
    await db.execute(_REDACT_CITATIONS, {"doc_id": str(doc.id), "owner_id": owner_id})
    await db.execute(delete(Document).where(Document.id == doc.id))   # cascades pages, chunks, insights
    await db.commit()
    try:
        await anyio.to_thread.run_sync(storage.delete, key)  # after commit: a failed unlink leaves an orphan, not a dangling row
    except Exception as exc:  # noqa: BLE001
        log.warning("storage_delete_failed", key=key, error=str(exc))

