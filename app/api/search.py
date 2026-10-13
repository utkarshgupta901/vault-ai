"""GET /search — retrieval debugging. Runs exactly the chat's retrieval (same SQL, same fusion) with no LLM call and
returns every rank, so "why did the bot cite that?" can be answered from numbers. Used by the eval, by
scripts/explain_retrieval.py and by the dashboard's retrieval drawer."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.config import settings
from app.core.auth import CurrentUser, get_current_user
from app.schemas.common import ERROR_RESPONSES
from app.schemas.search import SearchResponse
from app.services import chat as svc

router = APIRouter(tags=["search"], responses=ERROR_RESPONSES)

User = Annotated[CurrentUser, Depends(get_current_user)]


@router.get("/search", response_model=SearchResponse, summary="Hybrid retrieval only (no LLM call)")
async def search(
    user: User,
    q: Annotated[str, Query(min_length=1, max_length=2000, description="The question to retrieve for")],
    document_ids: Annotated[list[uuid.UUID] | None, Query(
        description="Repeat to pass several. Intersected with your ready documents; default = all of them")] = None,
    mode: Literal["hybrid", "vector", "keyword"] = "hybrid",
    k: Annotated[int, Query(ge=1, le=40)] = settings.RETRIEVAL_K,
    per_doc_cap: Annotated[int | None, Query(
        ge=1, le=40, description="Max hits per document (default: k for one document, else PER_DOC_CAP)")] = None,
) -> SearchResponse:
    """Hits carry `rrf`, `sem_rank`, `lex_rank`, `similarity` and `id_hit`:
    rrf = 1/(RRF_K + sem_rank) + LEXICAL_WEIGHT / (RRF_K + lex_rank), a missing rank contributing 0."""
    return await svc.search(user.id, q=q, document_ids=document_ids, mode=mode, k=k, per_doc_cap=per_doc_cap)
