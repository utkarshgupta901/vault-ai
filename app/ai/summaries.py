"""Custom summaries (summary.v1): POST /documents/{document_id}/summaries {length, tone, focus}.

Synchronous for the caller (a few seconds) and cached per input. The cache key hashes every input that changes the
output: the file's bytes (documents.sha256), length, tone, normalized focus, prompt version and model. Asking again
costs nothing; the hit is still logged in ai_usage (cache_hit=true, zero tokens) so the hit rate is measurable.

Session discipline (CLAUDE.md rule 3): one short session to load (and to answer cache hits), NO session while the
model is working, then a second short session to save the summary and its ai_usage row together.
"""

import asyncio
import hashlib
import uuid

import anyio
import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import get_provider
from app.ai.insights import DocPage, build_prompt_pages
from app.ai.prompts import summary as prompt
from app.ai.provider import ProviderError, Usage
from app.ai.usage import usage_row
from app.config import settings
from app.core.db import async_session
from app.core.errors import AppError, ErrorCode, not_found
from app.core.textnorm import normalize
from app.models import Document, DocumentPage, DocumentSummary
from app.schemas.documents import SummaryOut, SummaryRequest
from app.services.documents import get_owned

log = structlog.get_logger(__name__)

SUMMARY_TIMEOUT_S = 30.0
MAX_OUTPUT_TOKENS = 2_000


def clean_focus(focus: str | None) -> str | None:
    cleaned = " ".join((focus or "").split())
    return cleaned or None


def cache_key(sha256: str, length: str, tone: str, focus: str | None, *, prompt_version: str, model: str) -> str:
    """Focus is normalized (case, quotes, whitespace) so "Termination " and "termination" share one cache entry."""
    parts = [sha256, length, tone, normalize(focus), prompt_version, model]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def require_ready(doc: Document) -> None:
    if doc.status != "ready":
        raise AppError(ErrorCode.DOCUMENT_NOT_READY, 409, f"Document is not ready yet (status: {doc.status}).",
                       {"status": doc.status, "stage": doc.stage})


def _to_out(row: DocumentSummary, *, cached: bool) -> SummaryOut:
    return SummaryOut(document_id=row.document_id, length=row.length, tone=row.tone, focus=row.focus,
                      content=row.content, cached=cached, model=row.model, prompt_version=row.prompt_version)


async def _load_pages(db: AsyncSession, document_id: uuid.UUID) -> list[DocPage]:
    rows = await db.execute(select(DocumentPage.page_no, DocumentPage.label, DocumentPage.text)
                            .where(DocumentPage.document_id == document_id).order_by(DocumentPage.page_no))
    return [DocPage(r.page_no, r.label, r.text) for r in rows]


def _build_input(filename: str, pages: list[DocPage], req: SummaryRequest, focus: str | None) -> str:
    """CPU work (tokenizing for the budget check), so callers run it in a thread, off the event loop."""
    chosen, coverage = build_prompt_pages(pages)
    return prompt.render(filename, chosen, coverage, page_count=len(pages), length=req.length, tone=req.tone,
                         focus=focus)


async def record_failed_call(*, operation: str, usage: Usage, prompt_version: str, owner_id: uuid.UUID,
                             document_id: uuid.UUID | None, error_code: ErrorCode) -> None:
    """A failed call still belongs in the ledger (success=false), so error rates per operation are visible.
    Tokens are recorded when the provider reported them (ProviderError.usage: the reply was billed but unusable);
    when the request itself failed they are unknown and recorded as zero (Usage(model=...))."""
    async with async_session() as db:
        db.add(usage_row(operation=operation, usage=usage, owner_id=owner_id, prompt_version=prompt_version,
                         document_id=document_id, success=False, error_code=error_code))
        await db.commit()


async def _generate(prompt_input: str, *, owner_id: uuid.UUID, document_id: uuid.UUID) -> tuple[str, Usage]:
    model = settings.INSIGHTS_MODEL
    failed = {"operation": "summary", "prompt_version": prompt.VERSION, "owner_id": owner_id,
              "document_id": document_id}
    try:
        async with asyncio.timeout(SUMMARY_TIMEOUT_S):
            content, usage = await get_provider().acomplete_text(
                model=model, effort=settings.INSIGHTS_EFFORT, instructions=prompt.INSTRUCTIONS, input=prompt_input,
                max_output_tokens=MAX_OUTPUT_TOKENS,
            )
    except TimeoutError as exc:
        await record_failed_call(usage=Usage(model=model), error_code=ErrorCode.UPSTREAM_TIMEOUT, **failed)
        raise AppError(ErrorCode.UPSTREAM_TIMEOUT, 504, "The summary took too long; please try again.") from exc
    except ProviderError as exc:
        # exc.usage: a truncated/refused/unparsable reply was still billed, so record its real tokens.
        await record_failed_call(usage=exc.usage or Usage(model=model), error_code=ErrorCode.UPSTREAM_AI_ERROR,
                                 **failed)
        raise AppError(ErrorCode.UPSTREAM_AI_ERROR, 502, "The AI provider could not produce a summary.",
                       {"kind": exc.kind}) from exc
    if not content.strip():
        await record_failed_call(usage=usage, error_code=ErrorCode.UPSTREAM_AI_ERROR, **failed)
        raise AppError(ErrorCode.UPSTREAM_AI_ERROR, 502, "The AI provider returned an empty summary.")
    return content.strip(), usage


async def create_summary(owner_id: uuid.UUID, document_id: uuid.UUID, req: SummaryRequest) -> SummaryOut:
    focus = clean_focus(req.focus)
    model = settings.INSIGHTS_MODEL
    async with async_session() as db:
        doc = await get_owned(db, owner_id, document_id)
        require_ready(doc)
        key = cache_key(doc.sha256, req.length, req.tone, focus, prompt_version=prompt.VERSION, model=model)
        hit = await db.scalar(select(DocumentSummary)
                              .where(DocumentSummary.document_id == doc.id, DocumentSummary.cache_key == key))
        if hit is not None:
            db.add(usage_row(operation="summary", usage=Usage(model=hit.model), owner_id=owner_id,
                             prompt_version=hit.prompt_version, document_id=doc.id, cache_hit=True))
            await db.commit()
            return _to_out(hit, cached=True)
        filename = doc.filename
        pages = await _load_pages(db, doc.id)
    # --- no DB session from here until the model has answered ---
    prompt_input = await anyio.to_thread.run_sync(_build_input, filename, pages, req, focus)
    content, usage = await _generate(prompt_input, owner_id=owner_id, document_id=document_id)

    out = SummaryOut(document_id=document_id, length=req.length, tone=req.tone, focus=focus, content=content,
                     cached=False, model=model, prompt_version=prompt.VERSION)
    async with async_session() as db:
        saved = True
        try:
            # Two identical requests racing: both pay, the first insert wins, the second is a no-op.
            await db.execute(pg_insert(DocumentSummary).values(
                cache_key=key, **out.model_dump(exclude={"cached"}),
            ).on_conflict_do_nothing(index_elements=[DocumentSummary.document_id, DocumentSummary.cache_key]))
        except IntegrityError:          # FK violation: the document was deleted while the model was writing
            await db.rollback()
            saved = False
        db.add(usage_row(operation="summary", usage=usage, owner_id=owner_id, prompt_version=prompt.VERSION,
                         document_id=document_id))
        await db.commit()
    if not saved:
        raise not_found(ErrorCode.DOCUMENT_NOT_FOUND, "Document")
    log.info("summary_created", document_id=str(document_id), length=req.length, tone=req.tone,
             focus=bool(focus), latency_ms=usage.latency_ms)
    return out
