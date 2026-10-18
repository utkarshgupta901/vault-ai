"""GET /search — retrieval debugging. Runs exactly the chat's retrieval (same SQL, same fusion) with no LLM call and
returns every rank, so "why did the bot cite that?" can be answered from numbers. Used by the eval, by
scripts/explain_retrieval.py and by the dashboard's retrieval drawer."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.config import settings
from app.core.auth import CurrentUser, get_current_user
from app.core.ratelimit import enforce_daily_quota, rate_limit
from app.schemas.common import ERROR_RESPONSES
from app.schemas.search import SearchResponse
from app.services import chat as svc

router = APIRouter(tags=["search"], responses=ERROR_RESPONSES)

User = Annotated[CurrentUser, Depends(get_current_user)]

# Search pays for a query embedding (unless cached) and runs the full retrieval SQL, so it gets the same guards as
# the other AI routes: a per-minute limit and the daily cost quota. Its own rate-limit group, so the retrieval drawer
# or an eval run never eats into the chat or summaries budget.
SEARCH_GUARDS = [Depends(rate_limit("search")), Depends(enforce_daily_quota)]


@router.get("/search", response_model=SearchResponse, summary="Hybrid retrieval only (no LLM call)",
            dependencies=SEARCH_GUARDS)
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
    rrf = 1/(RRF_K + sem_rank) + LEXICAL_WEIGHT / (RRF_K + lex_rank) + (1/(RRF_K + 1) if `id_hit`), a missing rank
    contributing 0. The last term is the identifier boost (D-02, on unless ID_HIT_BOOST is false): a chunk matching
    an exact identifier (LRK-4471, clause 9.2, "Appendix C") also counts as a rank-1 vector hit, because found by the
    keyword leg alone it would lose to chunks that are merely okay on both legs and be cut by the per-document cap."""
    return await svc.search(user.id, q=q, document_ids=document_ids, mode=mode, k=k, per_doc_cap=per_doc_cap)
