"""GET /api/v1/search — retrieval debugging (no LLM call). Shows exactly what the chat would retrieve and why."""

import uuid

from pydantic import BaseModel


class SearchHit(BaseModel):
    chunk_id: int
    document_id: uuid.UUID
    filename: str
    label: str                    # "p. 4"
    page_start: int
    page_end: int
    rrf: float                    # fused score
    sem_rank: int | None          # rank in the vector leg (None = not retrieved by it)
    lex_rank: int | None          # rank in the keyword leg
    similarity: float | None      # cosine similarity (1 - distance)
    id_hit: bool                  # matched an identifier-like term (e.g. LRK-4471, 9.2)
    snippet: str                  # best-matching sentence (for display)
    content: str                  # full chunk text (eval checks gold substrings against it)


class SearchResponse(BaseModel):
    query: str
    mode: str                     # hybrid | vector | keyword
    terms: list[str]
    id_terms: list[str]
    top_similarity: float | None
    hits: list[SearchHit]
