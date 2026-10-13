"""Compare 2-4 documents (compare.v1, P1 below the cut line): POST /documents/compare.

The full page-marked text of every document goes into ONE structured-output call: no retrieval and no dependency on
insights, so the model really reads everything it compares. That only works while the documents are small; above
COMPARE_MAX_INPUT_TOKENS we refuse with 422 COMPARE_TOO_LARGE and point to multi-document chat, which retrieves
the relevant passages instead of reading everything. As with insights, every value's page is re-checked in code.

Request/response models live here (not app/schemas) because compare is an optional feature owned by this module.
"""

import asyncio
import uuid
from dataclasses import dataclass

import anyio
import structlog
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.ai import get_provider
from app.ai.insights import DocPage, assign_page, content_words, count_tokens
from app.ai.prompts import compare as prompt
from app.ai.provider import ProviderError, Usage
from app.ai.summaries import clean_focus, record_failed_call
from app.ai.usage import usage_row
from app.config import settings
from app.core.db import async_session
from app.core.errors import AppError, ErrorCode
from app.models import Document, DocumentPage

log = structlog.get_logger(__name__)

COMPARE_MAX_INPUT_TOKENS = 60_000
COMPARE_TIMEOUT_S = 45.0
MAX_OUTPUT_TOKENS = 4_000


# --- API models ----------------------------------------------------------------------------------------------------
class CompareRequest(BaseModel):
    document_ids: list[uuid.UUID] = Field(min_length=2, max_length=4)
    focus: str | None = Field(default=None, max_length=200, description="Optional focus, e.g. 'termination terms'")

    @field_validator("document_ids")
    @classmethod
    def _distinct(cls, ids: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(set(ids)) != len(ids):
            raise ValueError("document_ids must be distinct")
        return ids


class ComparedDocument(BaseModel):
    ref: str                 # "D1".. as used in similarities/differences/summary
    document_id: uuid.UUID
    filename: str
    title: str | None


class ComparedValueOut(BaseModel):
    ref: str
    document_id: uuid.UUID
    value: str
    page: int | None         # verified against that document's text; null when it could not be confirmed


class DimensionOut(BaseModel):
    name: str
    values: list[ComparedValueOut]


class CompareOut(BaseModel):
    documents: list[ComparedDocument]
    dimensions: list[DimensionOut]
    similarities: list[str]
    differences: list[str]
    summary: str
    model: str
    prompt_version: str


# --- Structured-output schema (what the model returns) -------------------------------------------------------------
class ComparedValue(BaseModel):
    document: str = Field(description="The document id from its <document id=...> tag, e.g. 'D1'")
    value: str = Field(description="What this document says for the dimension, or 'not stated'")
    page: int | None = Field(description="Page n where it is stated in that document, or null")


class Dimension(BaseModel):
    name: str = Field(description="The aspect being compared, e.g. 'termination notice period'")
    values: list[ComparedValue]


class Comparison(BaseModel):
    dimensions: list[Dimension]
    similarities: list[str]
    differences: list[str]
    summary: str


# --- Steps -----------------------------------------------------------------------------------------------------
@dataclass
class _Doc:
    ref: str
    id: uuid.UUID
    filename: str
    title: str | None
    pages: list[DocPage]


async def _load(owner_id: uuid.UUID, document_ids: list[uuid.UUID]) -> list[_Doc]:
    """404 if any id is missing or someone else's, 409 if any is not ready; one short session."""
    async with async_session() as db:
        found = {d.id: d for d in await db.scalars(
            select(Document).where(Document.owner_id == owner_id, Document.id.in_(document_ids)))}
        missing = [str(i) for i in document_ids if i not in found]
        if missing:
            raise AppError(ErrorCode.DOCUMENT_NOT_FOUND, 404, "Document not found.", {"document_ids": missing})
        not_ready = [str(i) for i in document_ids if found[i].status != "ready"]
        if not_ready:
            raise AppError(ErrorCode.DOCUMENT_NOT_READY, 409, "Some documents are still processing.",
                           {"document_ids": not_ready})
        rows = await db.execute(
            select(DocumentPage.document_id, DocumentPage.page_no, DocumentPage.label, DocumentPage.text)
            .where(DocumentPage.document_id.in_(document_ids)).order_by(DocumentPage.page_no))
        pages: dict[uuid.UUID, list[DocPage]] = {i: [] for i in document_ids}
        for r in rows:
            pages[r.document_id].append(DocPage(r.page_no, r.label, r.text))
    return [_Doc(f"D{n}", i, found[i].filename, found[i].title, pages[i]) for n, i in enumerate(document_ids, 1)]


def _build_input(docs: list[_Doc], focus: str | None) -> tuple[str, int]:
    """CPU work (tokenizing), run in a thread. Returns (prompt input, document tokens)."""
    tokens = sum(count_tokens(p.text) for d in docs for p in d.pages)
    return prompt.render([(d.ref, d.filename, d.pages) for d in docs], focus), tokens


async def _generate(prompt_input: str, owner_id: uuid.UUID) -> tuple[Comparison, Usage]:
    model = settings.ANSWER_MODEL
    failed = {"operation": "compare", "prompt_version": prompt.VERSION, "owner_id": owner_id, "document_id": None}
    try:
        async with asyncio.timeout(COMPARE_TIMEOUT_S):
            return await get_provider().acomplete_json(
                model=model, effort=settings.ANSWER_EFFORT, instructions=prompt.INSTRUCTIONS, input=prompt_input,
                schema=Comparison, max_output_tokens=MAX_OUTPUT_TOKENS,
            )
    except TimeoutError as exc:
        await record_failed_call(usage=Usage(model=model), error_code=ErrorCode.UPSTREAM_TIMEOUT, **failed)
        raise AppError(ErrorCode.UPSTREAM_TIMEOUT, 504, "The comparison took too long; please try again.") from exc
    except ProviderError as exc:
        # exc.usage: a truncated/refused/unparsable reply was still billed, so record its real tokens.
        await record_failed_call(usage=exc.usage or Usage(model=model), error_code=ErrorCode.UPSTREAM_AI_ERROR,
                                 **failed)
        raise AppError(ErrorCode.UPSTREAM_AI_ERROR, 502, "The AI provider could not produce a comparison.",
                       {"kind": exc.kind}) from exc


def postprocess(comparison: Comparison, docs: list[_Doc]) -> list[DimensionOut]:
    """Map D1.. back to document ids, drop values for unknown ids (and duplicates), and re-check each page
    against that document's text (content-word overlap, as for insights key points)."""
    by_ref = {d.ref: d for d in docs}
    page_words = {d.ref: {p.page_no: content_words(p.text) for p in d.pages} for d in docs}
    dimensions = []
    for dim in comparison.dimensions:
        values, seen = [], set()
        for v in dim.values:
            ref = v.document.strip().upper()
            if ref not in by_ref or ref in seen:
                continue
            seen.add(ref)
            values.append(ComparedValueOut(ref=ref, document_id=by_ref[ref].id, value=" ".join(v.value.split()),
                                           page=assign_page(v.value, v.page, page_words[ref])))
        if dim.name.strip() and values:
            dimensions.append(DimensionOut(name=" ".join(dim.name.split()), values=values))
    return dimensions


async def compare_documents(owner_id: uuid.UUID, req: CompareRequest) -> CompareOut:
    focus = clean_focus(req.focus)
    docs = await _load(owner_id, req.document_ids)
    # --- no DB session from here until the model has answered ---
    prompt_input, tokens = await anyio.to_thread.run_sync(_build_input, docs, focus)
    if tokens > COMPARE_MAX_INPUT_TOKENS:
        raise AppError(ErrorCode.COMPARE_TOO_LARGE, 422,
                       f"These documents are too large to compare in full ({tokens} tokens; the limit is "
                       f"{COMPARE_MAX_INPUT_TOKENS}). Use multi-document chat instead.",
                       {"tokens": tokens, "max_tokens": COMPARE_MAX_INPUT_TOKENS, "hint": "use multi-document chat"})
    comparison, usage = await _generate(prompt_input, owner_id)
    out = CompareOut(
        documents=[ComparedDocument(ref=d.ref, document_id=d.id, filename=d.filename, title=d.title) for d in docs],
        dimensions=postprocess(comparison, docs),
        similarities=[s.strip() for s in comparison.similarities if s.strip()],
        differences=[s.strip() for s in comparison.differences if s.strip()],
        summary=comparison.summary.strip(), model=usage.model, prompt_version=prompt.VERSION,
    )
    async with async_session() as db:
        db.add(usage_row(operation="compare", usage=usage, owner_id=owner_id, prompt_version=prompt.VERSION))
        await db.commit()
    log.info("documents_compared", documents=len(docs), tokens=tokens, dimensions=len(out.dimensions))
    return out
