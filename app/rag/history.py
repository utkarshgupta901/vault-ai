"""Conversation history for multi-turn chat.

Rules (design §2c):
  - the last HISTORY_TURNS turns (user question + its answer), ordered by `seq` — never by created_at, because rows
    inserted in one transaction share the same now();
  - only finished answers: a half-streamed or failed answer would teach the model its own broken output;
  - [S#] markers stripped, because source numbers restart every turn ("S2" now is a different chunk);
  - capped at HISTORY_MAX_TOKENS by dropping the OLDEST messages first (recent context matters most); a newest
    message that alone exceeds the cap is truncated, never dropped (see cap_tokens).
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.provider import Msg
from app.config import settings
from app.models import ChatMessage, ChatSession
from app.rag.citations import strip_citations
from app.rag.tokens import count_tokens, truncate_tokens


@dataclass(frozen=True)
class History:
    messages: list[Msg]                 # oldest first, [S#] stripped, token-capped
    previous_query: str | None = None   # what retrieval used last turn (standalone query, else the raw question)


async def load_history(db: AsyncSession, *, owner_id: uuid.UUID, session_id: uuid.UUID, before_seq: int) -> History:
    rows = list(await db.scalars(
        select(ChatMessage)
        .join(ChatSession, ChatSession.id == ChatMessage.session_id)
        .where(ChatSession.owner_id == owner_id, ChatMessage.session_id == session_id,
               ChatMessage.seq < before_seq,
               or_(ChatMessage.role == "user", ChatMessage.status == "complete"))
        .order_by(ChatMessage.seq.desc())
        .limit(settings.HISTORY_TURNS * 2)
    ))
    rows.reverse()
    messages = [Msg(role=m.role, content=strip_citations(m.content)) for m in rows if m.content]   # type: ignore[typeddict-item]
    return History(messages=cap_tokens(messages, settings.HISTORY_MAX_TOKENS), previous_query=_previous_query(rows))


def cap_tokens(messages: list[Msg], max_tokens: int) -> list[Msg]:
    """Keep the newest messages that fit in max_tokens, dropping the OLDEST first.

    Exception: if the newest message ALONE is over the cap (a long, detailed answer), dropping it would leave no
    history at all, which silently turns off condense and the conversation context: "And after three years?" would
    then be searched and answered as typed. So it is cut to its first half-cap of tokens instead, leaving the other
    half for the messages before it (normally the question it answered)."""
    kept: list[Msg] = []
    budget = max_tokens
    for m in reversed(messages):            # newest first, so the oldest are the ones dropped
        n = count_tokens(m["content"])
        if n > budget and not kept:
            m = Msg(role=m["role"], content=truncate_tokens(m["content"], max_tokens // 2))
            n = count_tokens(m["content"])
        if n > budget:
            break
        kept.append(m)
        budget -= n
    kept.reverse()
    return kept


def _previous_query(rows: list[ChatMessage]) -> str | None:
    """The last retrieval query: the previous answer's standalone_query if the question was rewritten, otherwise the
    previous question itself. Used by the condense fallback for very short follow-ups ("and contractors?")."""
    for i in range(len(rows) - 1, -1, -1):
        m = rows[i]
        if m.role == "assistant" and m.standalone_query:
            return m.standalone_query
        if m.role == "user":
            return m.content
    return None
