"""Chat API models, including the SSE contract for POST /chat/sessions/{id}/messages/stream:

  event: meta       data: MetaEvent        (once, before any tokens)
  event: token      data: TokenEvent       (repeated)
  event: citations  data: CitationsEvent   (once, after the answer is complete and checked)
  event: followups  data: FollowupsEvent   (optional; skipped if the time budget is missed)
  event: done       data: DoneEvent        (once, last)
  event: error      data: ErrorEvent       (terminal, instead of done)

Clients must ignore unknown event types and unknown fields, so adding events later is non-breaking."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import ApiModel


class SessionCreate(BaseModel):
    document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    all_documents: bool = False
    title: str | None = Field(default=None, max_length=200)


class SessionOut(ApiModel):
    id: uuid.UUID
    title: str | None
    all_documents: bool
    document_ids: list[uuid.UUID]
    created_at: datetime
    last_message_at: datetime | None
    starter_questions: list[str] = []
    warnings: list[str] = []


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    client_message_id: str | None = Field(default=None, max_length=64, description="Idempotency key for retries")


class Citation(BaseModel):
    ref: str                       # "S1"
    chunk_id: int
    document_id: uuid.UUID
    filename: str
    label: str                     # "p. 4"
    page_start: int
    page_end: int
    snippet: str | None = None
    weak_support: bool = False
    source_deleted: bool = False


class SourceRef(BaseModel):
    ref: str
    chunk_id: int
    document_id: uuid.UUID
    filename: str
    label: str


class Signals(BaseModel):
    top_similarity: float | None = None
    id_hit: bool = False
    lexical_match: bool = False
    valid_citations: int = 0
    invalid_citations: int = 0
    uncited_claims: int = 0
    ungrounded_facts: int = 0
    gate: bool = False


class UsageOut(BaseModel):
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    estimated: bool = False


class MessageOut(ApiModel):
    id: uuid.UUID
    seq: int
    session_id: uuid.UUID
    role: Literal["user", "assistant"]
    content: str
    status: Literal["streaming", "complete", "interrupted", "failed"]
    reply_to_id: uuid.UUID | None = None
    standalone_query: str | None = None
    answer_type: Literal["answered", "not_found", "ungrounded"] | None = None
    confidence: Literal["high", "medium", "low"] | None = None
    signals: dict[str, Any] | None = None
    retrieval: dict[str, Any] | None = None
    citations: list[dict[str, Any]] = []
    followups: list[str] = []
    warnings: list[Any] = []
    model: str | None = None
    prompt_version: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: Decimal | None = None
    ttft_ms: int | None = None
    latency_ms: int | None = None
    timings: dict[str, int] | None = None   # {condense?, embed?, retrieve, ttft?, total} in ms
    created_at: datetime


class AskResponse(BaseModel):
    """Non-streaming ask: the user message and the assistant answer."""

    user_message: MessageOut
    answer: MessageOut


# --- SSE event payloads ---------------------------------------------------------------------------------
class MetaEvent(BaseModel):
    message_id: uuid.UUID
    user_message_id: uuid.UUID
    standalone_query: str | None
    sources: list[SourceRef]
    retrieval_mode: str
    warnings: list[str] = []


class TokenEvent(BaseModel):
    delta: str


class CitationsEvent(BaseModel):
    citations: list[Citation]
    answer_type: Literal["answered", "not_found", "ungrounded"]
    confidence: Literal["high", "medium", "low"]
    signals: Signals


class FollowupsEvent(BaseModel):
    questions: list[str]


class DoneEvent(BaseModel):
    message_id: uuid.UUID
    usage: UsageOut
    model: str | None
    ttft_ms: int | None
    latency_ms: int
    timings: dict[str, int]


class ErrorEvent(BaseModel):
    code: str
    message: str
