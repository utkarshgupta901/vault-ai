"""Document AI endpoints: structured insights (read), custom summaries, and multi-document compare (P1).

Routes that call a model (summaries, compare) deliberately do NOT take the request-scoped `get_db` session: it would
stay open for the whole LLM call. Their services open short sessions themselves (CLAUDE.md rule 3).
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import summaries
from app.ai.compare import CompareOut, CompareRequest, compare_documents
from app.core.auth import CurrentUser, get_current_user
from app.core.db import get_db
from app.core.ratelimit import AI_GUARDS
from app.models import DocumentInsight
from app.schemas.common import ERROR_RESPONSES
from app.schemas.documents import SummaryOut, SummaryRequest
from app.schemas.insights import DocumentInsights, InsightsOut
from app.services import documents as svc

router = APIRouter(tags=["insights"], responses=ERROR_RESPONSES)

User = Annotated[CurrentUser, Depends(get_current_user)]
Db = Annotated[AsyncSession, Depends(get_db)]

_INSIGHT_FIELDS = (*DocumentInsights.model_fields, "coverage", "model", "prompt_version")


def insights_payload(row: DocumentInsight) -> dict[str, Any]:
    """The stored (already verified) insights: DocumentInsights fields + coverage/model/prompt_version."""
    payload = {name: getattr(row, name) for name in _INSIGHT_FIELDS}
    payload["document_date"] = row.document_date.isoformat() if row.document_date else None
    return payload


# Registered before the /documents/{document_id}/... routes so the literal path can never be read as an id.
@router.post("/documents/compare", response_model=CompareOut, summary="Compare 2-4 documents side by side",
             responses={409: {}, 502: {}, 504: {}}, dependencies=AI_GUARDS)
async def compare(body: CompareRequest, user: User) -> CompareOut:
    """Structured comparison over the **full text** of each document (no retrieval): dimensions with one value
    per document (with a verified page), similarities, differences and a summary. Documents totalling more than
    60k tokens are rejected with `COMPARE_TOO_LARGE`; use multi-document chat for those."""
    return await compare_documents(user.id, body)


@router.get("/documents/{document_id}/insights", response_model=InsightsOut, summary="Structured AI insights")
async def get_insights(document_id: uuid.UUID, user: User, db: Db) -> InsightsOut:
    """`status` follows the document's insights pipeline (pending, processing, ready, failed). `insights` is the
    last stored result (it stays visible while a reprocess is running) or null if none exists yet."""
    doc = await svc.get_owned(db, user.id, document_id)
    row = await db.get(DocumentInsight, doc.id)
    return InsightsOut(document_id=doc.id, status=doc.insights_status, error_code=doc.insights_error_code,
                       insights=insights_payload(row) if row else None, updated_at=row.updated_at if row else None)


@router.post("/documents/{document_id}/summaries", response_model=SummaryOut, summary="Custom summary (cached)",
             responses={409: {}, 502: {}, 504: {}}, dependencies=AI_GUARDS)
async def create_summary(document_id: uuid.UUID, body: SummaryRequest, user: User) -> SummaryOut:
    """Summary with a chosen length (short ~80 words, medium ~200, detailed ~450), tone (neutral, executive,
    technical, simple) and optional focus. Identical requests are served from the cache (`cached: true`)."""
    return await summaries.create_summary(user.id, document_id, body)
