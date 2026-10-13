"""The ingestion pipeline: download -> extract -> chunk -> embed -> ready, then enqueue insights.

The database is the checkpoint, so any stage can be re-run safely after a crash, a retry or a duplicate delivery:
  - extract and chunk are delete-then-insert (re-running them gives the same rows, never duplicates);
  - chunking first copies vectors that this owner already paid for (same embed_hash), so a re-run re-embeds nothing
    that was already embedded (headers use the filename-derived title, which insights never change, so the hashes
    of an unchanged file stay the same across reprocesses);
  - embedding only touches chunks WHERE embedding IS NULL, one committed batch at a time.

Every write transaction starts with a run_id-fenced UPDATE of the document row (state.progress_stmt/ready_stmt).
It does two jobs: zero rows back means a reprocess or delete took over (raise RunSuperseded and stop), and the row
lock it takes makes the rest of the transaction atomic with respect to a concurrent reprocess.
No transaction is open while a file downloads or the embedding API is called.

Progress: `stage` names the stage being worked on, `progress_pct` the work completed (extract 0-20, chunk 20-30,
embed 30-100 per batch).
"""

from dataclasses import dataclass
from pathlib import Path, PurePath

import structlog
from botocore.exceptions import ClientError
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session

from app.ai import get_provider
from app.ai.usage import usage_row
from app.config import settings
from app.core.db import sync_session
from app.core.errors import ErrorCode, PermanentError
from app.ingestion.chunking import ChunkDraft, chunk_pages, count_tokens, embed_text
from app.ingestion.extract import ExtractedDoc, PageText, extract
from app.ingestion.titles import filename_title
from app.models import Chunk, Document, DocumentPage
from app.services import state, storage
from app.worker.attempt import Attempt, RunSuperseded
from app.worker.dispatch import QueueUnavailable, enqueue_insights

log = structlog.get_logger(__name__)

EXTRACT_DONE_PCT = 20
CHUNK_DONE_PCT = 30


@dataclass(frozen=True)
class _Source:
    storage_key: str
    mime_type: str
    header_title: str   # from the filename, NOT documents.title (insights rewrite it; see titles.py)


@dataclass(frozen=True)
class _Pending:
    """A chunk still waiting for its vector."""

    chunk_id: int
    text: str       # header + "\n" + content: exactly what embed_hash was computed over
    tokens: int


def run_ingest(attempt: Attempt) -> None:
    source = _load_source(attempt)
    extracted = _download_and_extract(attempt, source)
    with attempt.timed("extract"):
        _save_pages(attempt, extracted.pages)
    with attempt.timed("chunk"):
        chunk_count = _save_chunks(attempt, extracted.pages, source.header_title)
    with attempt.timed("embed"):
        _embed_pending(attempt)
    _mark_ready(attempt, extracted, chunk_count)
    _enqueue_insights(attempt)


def _advance(s: Session, attempt: Attempt, stage: str, pct: int) -> None:
    """The fence at the start of every write transaction (see module docstring)."""
    if s.execute(state.progress_stmt(attempt.document_id, attempt.run_id, stage, pct)).first() is None:
        raise RunSuperseded()


def _load_source(attempt: Attempt) -> _Source:
    with sync_session() as s:
        doc = s.get(Document, attempt.document_id)
        if doc is None:
            raise RunSuperseded()  # deleted after the claim
        return _Source(storage_key=doc.storage_key, mime_type=doc.mime_type,
                       header_title=filename_title(doc.filename))


# --- Download + extract ---------------------------------------------------------------------------------------------


def _download_and_extract(attempt: Attempt, source: _Source) -> ExtractedDoc:
    with attempt.timed("download"):
        path = _download(source.storage_key)
    try:
        with attempt.timed("extract"):
            extracted = extract(path, source.mime_type)
    finally:
        path.unlink(missing_ok=True)  # worker disks are small and shared by many tasks: never leave the copy behind
    log.info("extracted", pages=len(extracted.pages), warnings=extracted.warnings)
    return extracted


def _download(storage_key: str) -> Path:
    try:
        return storage.download_to_temp(storage_key, suffix=PurePath(storage_key).suffix)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
            raise PermanentError(ErrorCode.STORAGE_ERROR, "The original file is missing from storage.") from exc
        raise


def _save_pages(attempt: Attempt, pages: list[PageText]) -> None:
    """document_pages back the citation click-through ("show me p. 4"). Delete-then-insert = idempotent."""
    with sync_session() as s:
        _advance(s, attempt, "chunking", EXTRACT_DONE_PCT)
        s.execute(delete(DocumentPage).where(DocumentPage.document_id == attempt.document_id))
        s.execute(insert(DocumentPage), [
            {"document_id": attempt.document_id, "page_no": p.page_no, "label": p.label,
             "heading_path": p.heading_path, "text": p.text, "char_count": len(p.text)}
            for p in pages
        ])


# --- Chunk -----------------------------------------------------------------------------------------------------------


def _save_chunks(attempt: Attempt, pages: list[PageText], title: str) -> int:
    """ONE transaction: copy reusable vectors, delete this document's old chunks, insert the new ones.
    Reading the old vectors before the delete is what makes a reprocess (after a failed embed, or of a ready
    document) cost nothing extra."""
    drafts = chunk_pages(pages, title=title, model=settings.EMBED_MODEL, dims=settings.EMBED_DIMS)
    if not drafts:
        raise PermanentError(ErrorCode.NO_EXTRACTABLE_TEXT, "The document contains no extractable text.")
    with sync_session() as s:
        _advance(s, attempt, "embedding", CHUNK_DONE_PCT)
        reusable = _reusable_vectors(s, attempt, {d.embed_hash for d in drafts})
        s.execute(delete(Chunk).where(Chunk.document_id == attempt.document_id))
        s.execute(insert(Chunk), [_chunk_row(attempt, d, reusable.get(d.embed_hash)) for d in drafts])
    log.info("chunks_saved", chunks=len(drafts), reused_vectors=sum(d.embed_hash in reusable for d in drafts))
    return len(drafts)


def _reusable_vectors(s: Session, attempt: Attempt, hashes: set[str]) -> dict[str, tuple[list[float], str | None]]:
    """Vectors this OWNER already has for identical input. Same owner only: copying another tenant's vector would
    reveal that they uploaded the same text (and their rows are none of this task's business)."""
    rows = s.execute(
        select(Chunk.embed_hash, Chunk.embedding, Chunk.embedding_model)
        .where(Chunk.owner_id == attempt.owner_id, Chunk.embed_hash.in_(hashes), Chunk.embedding.is_not(None))
    )
    return {embed_hash: (vector, model) for embed_hash, vector, model in rows}


def _chunk_row(attempt: Attempt, draft: ChunkDraft, reused: tuple[list[float], str | None] | None) -> dict:
    vector, model = reused if reused is not None else (None, None)
    return {
        "document_id": attempt.document_id, "owner_id": attempt.owner_id, "chunk_index": draft.chunk_index,
        "page_start": draft.page_start, "page_end": draft.page_end, "heading_path": draft.heading_path,
        "header": draft.header, "content": draft.content, "token_count": draft.token_count,
        "embed_hash": draft.embed_hash, "embedding": vector, "embedding_model": model,
    }


# --- Embed -----------------------------------------------------------------------------------------------------------


def _pending_chunks(attempt: Attempt) -> tuple[int, list[_Pending]]:
    with sync_session() as s:
        total = s.scalar(select(func.count()).select_from(Chunk).where(Chunk.document_id == attempt.document_id))
        rows = s.execute(
            select(Chunk.id, Chunk.header, Chunk.content)
            .where(Chunk.document_id == attempt.document_id, Chunk.embedding.is_(None))
            .order_by(Chunk.chunk_index)
        ).all()
    pending = []
    for chunk_id, header, content in rows:
        text = embed_text(header, content)
        pending.append(_Pending(chunk_id=chunk_id, text=text, tokens=count_tokens(text)))
    return total or 0, pending


def batches(items: list[_Pending], *, max_items: int, max_tokens: int) -> list[list[_Pending]]:
    """Greedy batching under both API limits: at most `max_items` inputs and `max_tokens` tokens per request."""
    result: list[list[_Pending]] = []
    batch: list[_Pending] = []
    tokens = 0
    for item in items:
        if batch and (len(batch) >= max_items or tokens + item.tokens > max_tokens):
            result.append(batch)
            batch, tokens = [], 0
        batch.append(item)
        tokens += item.tokens
    if batch:
        result.append(batch)
    return result


def _embed_pending(attempt: Attempt) -> None:
    """Embed every chunk that has no vector yet. Each batch commits its vectors AND its ai_usage row together, so
    after a crash the finished batches are kept (and paid for exactly once) and only the rest is redone."""
    provider = get_provider(for_worker=True)
    total, pending = _pending_chunks(attempt)
    done = total - len(pending)
    for batch in batches(pending, max_items=settings.EMBED_BATCH_SIZE, max_tokens=settings.EMBED_BATCH_MAX_TOKENS):
        result = provider.embed([item.text for item in batch])  # no transaction is open during the API call
        done += len(batch)
        with sync_session() as s:
            _advance(s, attempt, "embedding", CHUNK_DONE_PCT + (100 - CHUNK_DONE_PCT) * done // total)
            s.execute(update(Chunk), [
                {"id": item.chunk_id, "embedding": vector, "embedding_model": settings.EMBED_MODEL}
                for item, vector in zip(batch, result.vectors, strict=True)
            ])
            s.add(usage_row(operation="embed_chunks", usage=result.usage, owner_id=attempt.owner_id,
                            document_id=attempt.document_id, task_id=attempt.task_id,
                            request_id=attempt.request_id))
    log.info("embedded", chunks=total, embedded_now=len(pending), reused=total - len(pending))


# --- Ready + hand-off -------------------------------------------------------------------------------------------------


def _mark_ready(attempt: Attempt, extracted: ExtractedDoc, chunk_count: int) -> None:
    with sync_session() as s:
        missing = s.scalar(select(func.count()).select_from(Chunk)
                           .where(Chunk.document_id == attempt.document_id, Chunk.embedding.is_(None)))
        if missing:  # invariant: 'ready' means every chunk is searchable by vector
            raise RuntimeError(f"{missing} chunks still have no embedding")
        row = s.execute(state.ready_stmt(
            attempt.document_id, attempt.run_id,
            page_count=len(extracted.pages), chunk_count=chunk_count,
            token_count=sum(count_tokens(p.text) for p in extracted.pages), warnings=extracted.warnings,
        )).first()
        if row is None:
            raise RunSuperseded()


def _enqueue_insights(attempt: Attempt) -> None:
    """Chat already works at this point; insights are a separate task on the `ai` queue. If the broker is down the
    document stays ready and insights are marked failed/QUEUE_UNAVAILABLE, which the user can reprocess."""
    try:
        enqueue_insights(attempt.document_id, attempt.run_id, attempt.request_id)
    except QueueUnavailable as exc:
        log.error("insights_enqueue_failed", error=str(exc))
        with sync_session() as s:
            s.execute(state.insights_fail_stmt(attempt.document_id, attempt.run_id, ErrorCode.QUEUE_UNAVAILABLE))
