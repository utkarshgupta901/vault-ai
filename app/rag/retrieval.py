"""Hybrid retrieval: pgvector similarity + Postgres full-text search, fused with Reciprocal Rank Fusion, in ONE SQL
statement.

Why two legs:
  - vector (semantic) finds paraphrases: "time off" matches a chunk about "PTO";
  - keyword (lexical) finds exact tokens embeddings blur: "LRK-4471", "clause 9.2", "$60,000".
Why RRF instead of adding scores: cosine distance and ts_rank_cd live on different, uncalibrated scales. RRF only
uses each chunk's RANK in each leg:  rrf = 1/(RRF_K + sem_rank) + LEXICAL_WEIGHT * 1/(RRF_K + lex_rank).
A chunk found by both legs beats one found by a single leg; RRF_K=60 (the standard constant) flattens the curve so
rank 1 vs rank 2 matters less than "found by both".

Tenant isolation: both legs filter `owner_id = :owner_id` (denormalized onto chunks) AND `document_id = ANY(...)`,
and the final join re-checks documents.owner_id. Passing another owner's document ids returns nothing.

Per-document cap: in a multi-document session one long document could fill all k slots; capping sources per
document (PER_DOC_CAP) keeps comparisons ("how do the two contracts differ?") answerable. With one document in
scope the cap is k, i.e. no cap.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.rag.query_terms import QueryTerms, extract_terms

# hnsw.ef_search: how many graph candidates the HNSW scan keeps (default 40). We ask for 40 per leg AFTER the
# owner/document filter, so we widen it; iterative_scan lets pgvector keep scanning when the filter discards rows
# (pgvector >= 0.8), so a small document among many still gets its 40 candidates.
_TUNE_HNSW = (
    "SELECT set_config('hnsw.ef_search', '100', true), set_config('hnsw.iterative_scan', 'relaxed_order', true)"
)

_SEMANTIC = """semantic AS (
  SELECT id, dist, ROW_NUMBER() OVER (ORDER BY dist, id) AS rnk
  FROM (SELECT c.id, c.embedding <=> CAST(:qvec AS vector) AS dist
        FROM chunks c
        WHERE c.owner_id = :owner_id AND c.document_id = ANY(CAST(:doc_ids AS uuid[]))
          AND c.embedding IS NOT NULL
        ORDER BY dist LIMIT :n) s)"""
_NO_SEMANTIC = "semantic AS (SELECT NULL::bigint AS id, NULL::float8 AS dist, NULL::bigint AS rnk WHERE false)"

# Identifier tier: chunks matching an identifier-like term (LRK-4471, 9.2) rank above everything else in this leg.
_LEXICAL = """lexical AS (
  SELECT id, id_hit, ROW_NUMBER() OVER (ORDER BY id_hit DESC, score DESC, id) AS rnk
  FROM (SELECT c.id,
               (CAST(:has_ids AS boolean) AND c.tsv @@ to_tsquery('english', :q_ids)) AS id_hit,
               ts_rank_cd(c.tsv, to_tsquery('english', :q_or)) AS score
        FROM chunks c
        WHERE c.owner_id = :owner_id AND c.document_id = ANY(CAST(:doc_ids AS uuid[]))
          AND c.tsv @@ to_tsquery('english', :q_or)
        ORDER BY id_hit DESC, score DESC, c.id LIMIT :n) l)"""
_NO_LEXICAL = "lexical AS (SELECT NULL::bigint AS id, false AS id_hit, NULL::bigint AS rnk WHERE false)"

_FUSE_AND_SELECT = """fused AS (
  SELECT COALESCE(s.id, l.id) AS chunk_id,
         COALESCE(1.0::float8 / (:rrf_k + s.rnk), 0)
           + CAST(:w_lex AS float8) * COALESCE(1.0::float8 / (:rrf_k + l.rnk), 0) AS rrf,
         s.rnk AS sem_rank, l.rnk AS lex_rank, s.dist AS sem_dist, COALESCE(l.id_hit, false) AS id_hit
  FROM semantic s FULL OUTER JOIN lexical l ON s.id = l.id),
ranked AS (
  SELECT c.id, c.document_id, c.page_start, c.page_end, c.header, c.content,
         f.rrf, f.sem_rank, f.lex_rank, f.sem_dist, f.id_hit,
         ROW_NUMBER() OVER (PARTITION BY c.document_id ORDER BY f.rrf DESC, c.id) AS doc_rank
  FROM fused f JOIN chunks c ON c.id = f.chunk_id)
SELECT r.id, r.document_id, r.page_start, r.page_end, r.header, r.content,
       r.rrf, r.sem_rank, r.lex_rank, r.sem_dist, r.id_hit,
       d.filename, d.title, d.document_date, p.label,
       (SELECT min(dist) FROM semantic) AS top_dist,
       (SELECT COALESCE(bool_or(id_hit), false) FROM lexical) AS any_id_hit
FROM ranked r
JOIN documents d ON d.id = r.document_id AND d.owner_id = :owner_id
LEFT JOIN document_pages p ON p.document_id = r.document_id AND p.page_no = r.page_start
WHERE r.doc_rank <= :per_doc_cap
ORDER BY r.rrf DESC, r.id
LIMIT :k"""


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: int
    document_id: uuid.UUID
    filename: str
    title: str | None
    document_date: date | None
    label: str                  # human location from document_pages.label of page_start, e.g. "p. 4"
    page_start: int
    page_end: int
    header: str
    content: str
    rrf: float
    sem_rank: int | None        # None = not found by the vector leg
    lex_rank: int | None        # None = not found by the keyword leg
    sem_dist: float | None      # cosine distance (0 = identical direction)
    id_hit: bool

    @property
    def similarity(self) -> float | None:
        return None if self.sem_dist is None else 1.0 - self.sem_dist


@dataclass(frozen=True)
class RetrievalResult:
    chunks: list[RetrievedChunk]
    top_similarity: float | None    # 1 - distance of semantic rank 1 (None when the vector leg didn't run)
    id_hit: bool                    # some chunk matched an identifier-like query term
    mode: str                       # legs that actually ran: hybrid | vector | keyword | none
    terms: list[str] = field(default_factory=list)
    id_terms: list[str] = field(default_factory=list)


def effective_mode(requested: str, *, has_vector: bool, has_terms: bool) -> str:
    """The requested mode picks the legs; a leg that can't run (no query vector, or no search terms) is dropped, and
    if that leaves nothing we fall back to the other leg rather than return no sources at all."""
    use_vector = has_vector and requested in ("hybrid", "vector")
    use_keyword = has_terms and requested in ("hybrid", "keyword")
    if not (use_vector or use_keyword):
        use_vector, use_keyword = has_vector, has_terms
    if use_vector and use_keyword:
        return "hybrid"
    return "vector" if use_vector else ("keyword" if use_keyword else "none")


def vector_literal(vec: list[float]) -> str:
    """pgvector's text input format: '[0.1,0.2,...]'. Sent as a bound parameter and CAST in SQL."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def build_sql(mode: str) -> str:
    semantic = _SEMANTIC if mode in ("hybrid", "vector") else _NO_SEMANTIC
    lexical = _LEXICAL if mode in ("hybrid", "keyword") else _NO_LEXICAL
    return f"WITH {semantic},\n{lexical},\n{_FUSE_AND_SELECT}"


async def retrieve(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    document_ids: list[uuid.UUID],
    query_text: str,
    query_vec: list[float] | None,
    k: int | None = None,
    candidates: int | None = None,
    per_doc_cap: int | None = None,
    mode: str | None = None,
    w_lex: float | None = None,
    terms: QueryTerms | None = None,
) -> RetrievalResult:
    """Run hybrid retrieval inside the caller's (short) transaction and return the top-k chunks with their ranks."""
    k = k or settings.RETRIEVAL_K
    terms = terms or extract_terms(query_text)
    mode = effective_mode(mode or settings.RETRIEVAL_MODE, has_vector=query_vec is not None,
                          has_terms=bool(terms.terms))
    if not document_ids or mode == "none":
        return RetrievalResult([], None, False, mode, terms.terms, terms.id_terms)
    if per_doc_cap is None:
        per_doc_cap = k if len(document_ids) == 1 else settings.PER_DOC_CAP

    params: dict = {
        "owner_id": owner_id, "doc_ids": list(document_ids), "n": candidates or settings.RETRIEVAL_CANDIDATES,
        "rrf_k": settings.RRF_K, "w_lex": settings.LEXICAL_WEIGHT if w_lex is None else w_lex,
        "per_doc_cap": per_doc_cap, "k": k,
    }
    if mode in ("hybrid", "vector"):
        params["qvec"] = vector_literal(query_vec or [])
        await session.execute(text(_TUNE_HNSW))   # transaction-local (is_local=true): affects only this query
    if mode in ("hybrid", "keyword"):
        params |= {"q_or": terms.q_or, "q_ids": terms.q_ids, "has_ids": terms.has_ids}

    rows = (await session.execute(text(build_sql(mode)), params)).mappings().all()
    chunks = [_to_chunk(r) for r in rows]
    top_dist = rows[0]["top_dist"] if rows else None
    return RetrievalResult(
        chunks=chunks,
        top_similarity=None if top_dist is None else 1.0 - float(top_dist),
        id_hit=bool(rows[0]["any_id_hit"]) if rows else False,
        mode=mode,
        terms=terms.terms,
        id_terms=terms.id_terms,
    )


def _to_chunk(r) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=r["id"], document_id=r["document_id"], filename=r["filename"], title=r["title"],
        document_date=r["document_date"], label=r["label"] or f"p. {r['page_start']}",
        page_start=r["page_start"], page_end=r["page_end"], header=r["header"], content=r["content"],
        rrf=float(r["rrf"]), sem_rank=r["sem_rank"], lex_rank=r["lex_rank"],
        sem_dist=None if r["sem_dist"] is None else float(r["sem_dist"]), id_hit=bool(r["id_hit"]),
    )
