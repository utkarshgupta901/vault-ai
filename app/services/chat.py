"""Chat use-cases: sessions, message history, retrieval debugging (/search) and the ask pipeline.

The ask pipeline is ONE async generator, `answer(turn)`, that yields typed events (MetaEvent, TokenEvent, ...).
The SSE endpoint forwards them as they come; the JSON endpoint drains the same generator and returns the saved row.
So both endpoints run identical retrieval, prompting, checks and persistence.

Two phases, because HTTP status codes can only be chosen before the first byte of a stream is sent:
  1. `start_turn()` runs as a FastAPI dependency BEFORE the stream starts: ownership (404), idempotency
     (replay / 409 MESSAGE_IN_PROGRESS), scope (409 NO_READY_DOCUMENTS) and the insert of the user row plus a
     'streaming' assistant placeholder (a second concurrent ask hits the partial unique index -> 409 SESSION_BUSY).
  2. `answer(turn)` does everything that can only fail mid-stream: condense, embed, retrieve, gate, stream the answer,
     check citations, persist, follow-ups. Failures here become an `error` event.

DB rule: no session is held across a model call or while streaming. Every DB touch is a short `async_session()`
block; the answer is saved once at the end, together with its ai_usage rows, in one transaction.
"""

import time
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import anyio
import structlog
from sqlalchemy import delete, func, insert, select, text, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import get_provider
from app.ai.pricing import cost_usd
from app.ai.prompts import condense as condense_prompt
from app.ai.prompts import followups as followups_prompt
from app.ai.provider import LLMProvider, ProviderError, StreamEnd, Usage
from app.ai.usage import usage_row
from app.config import settings
from app.core.db import async_session
from app.core.errors import AppError, ErrorCode, not_found
from app.core.pagination import decode_cursor, encode_cursor
from app.core.request_context import get_request_id
from app.models import AIUsage, ChatMessage, ChatSession, ChatSessionDocument, Document, DocumentInsight
from app.rag.cache import QueryEmbedding, embed_query
from app.rag.citations import AnswerCheck, best_sentence, check_answer, content_tokens
from app.rag.condense import condense_question
from app.rag.condense import estimated_usage as estimated_condense_usage
from app.rag.confidence import confidence, gate_reply, should_gate
from app.rag.followups import estimated_usage as estimated_followups_usage
from app.rag.followups import suggest_followups
from app.rag.generation import AnswerStalled, FallbackUsed, stream_answer
from app.rag.history import History, load_history
from app.rag.prompting import AnswerPrompt, build_answer_prompt, number_sources
from app.rag.retrieval import RetrievalResult, RetrievedChunk, retrieve
from app.rag.tokens import count_tokens
from app.schemas.chat import (
    AskResponse,
    Citation,
    CitationsEvent,
    DoneEvent,
    ErrorEvent,
    FollowupsEvent,
    MessageCreate,
    MessageOut,
    MetaEvent,
    SessionCreate,
    SessionOut,
    Signals,
    SourceRef,
    TokenEvent,
    UsageOut,
)
from app.schemas.search import SearchHit, SearchResponse

log = structlog.get_logger(__name__)

ChatEvent = MetaEvent | TokenEvent | CitationsEvent | FollowupsEvent | DoneEvent | ErrorEvent

MAX_STARTERS = 5
TITLE_CHARS = 60
IN_FLIGHT_STATUSES = frozenset({"queued", "processing"})   # documents.status values that will still become 'ready'
BIGINT_MAX = 2**63 - 1                                       # chat_messages.seq is a bigint identity


def _ms(since: float) -> int:
    return int((time.perf_counter() - since) * 1000)


# =============================================================================================================
# Sessions and scope
# =============================================================================================================
@dataclass(frozen=True)
class ScopeDoc:
    id: uuid.UUID
    status: str
    title: str


@dataclass(frozen=True)
class Scope:
    docs: list[ScopeDoc]

    @property
    def ready_ids(self) -> list[uuid.UUID]:
        return [d.id for d in self.docs if d.status == "ready"]

    @property
    def ready_titles(self) -> list[str]:
        return [d.title for d in self.docs if d.status == "ready"]

    @property
    def warnings(self) -> list[str]:
        """'docs_not_ready' only while an in-scope document is still queued or processing, i.e. will become
        searchable on its own. A FAILED document never will: warning "still processing" on every answer would be
        false (and permanent in an all-documents session); the documents page already shows the failure."""
        return ["docs_not_ready"] if any(d.status in IN_FLIGHT_STATUSES for d in self.docs) else []


async def _owned_session(db: AsyncSession, owner_id: uuid.UUID, session_id: uuid.UUID) -> ChatSession:
    chat = await db.scalar(select(ChatSession).where(ChatSession.id == session_id, ChatSession.owner_id == owner_id))
    if chat is None:
        raise not_found(ErrorCode.SESSION_NOT_FOUND, "Chat session")
    return chat


async def _scope(db: AsyncSession, owner_id: uuid.UUID, chat: ChatSession) -> Scope:
    """A session's scope is evaluated on every question: an all-documents session sees documents uploaded later,
    and a deleted document drops out (chat_session_documents cascades)."""
    stmt = select(Document.id, Document.status, Document.title, Document.filename).where(Document.owner_id == owner_id)
    if not chat.all_documents:
        stmt = stmt.join(ChatSessionDocument, ChatSessionDocument.document_id == Document.id).where(
            ChatSessionDocument.session_id == chat.id)
    rows = (await db.execute(stmt.order_by(Document.created_at, Document.id))).all()
    return Scope([ScopeDoc(r.id, r.status, r.title or r.filename) for r in rows])


async def resolve_scope(db: AsyncSession, owner_id: uuid.UUID, session_id: uuid.UUID) -> tuple[list[uuid.UUID], list[str]]:
    """(ready document ids, warnings) for a session — the WP0 contract other packages call."""
    scope = await _scope(db, owner_id, await _owned_session(db, owner_id, session_id))
    return scope.ready_ids, scope.warnings


async def _starter_questions(db: AsyncSession, owner_id: uuid.UUID, ready_ids: list[uuid.UUID]) -> list[str]:
    """Up to 5 suggested questions from the insights of in-scope documents, taken round-robin so every document is
    represented (the first document's five questions shouldn't crowd out the others)."""
    if not ready_ids:
        return []
    rows = (await db.execute(
        select(DocumentInsight.document_id, DocumentInsight.suggested_questions)
        .join(Document, Document.id == DocumentInsight.document_id)
        .where(Document.owner_id == owner_id, DocumentInsight.document_id.in_(ready_ids))
    )).all()
    by_doc = {r.document_id: [q for q in (r.suggested_questions or []) if isinstance(q, str) and q.strip()]
              for r in rows}
    queues = [by_doc[d] for d in ready_ids if by_doc.get(d)]
    out: list[str] = []
    seen: set[str] = set()
    for i in range(max((len(q) for q in queues), default=0)):
        for q in queues:
            if i < len(q) and q[i].strip().casefold() not in seen and len(out) < MAX_STARTERS:
                seen.add(q[i].strip().casefold())
                out.append(q[i].strip())
    return out


async def _session_document_ids(db: AsyncSession, session_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
    out: dict[uuid.UUID, list[uuid.UUID]] = {sid: [] for sid in session_ids}
    if session_ids:
        rows = await db.execute(select(ChatSessionDocument.session_id, ChatSessionDocument.document_id)
                                .where(ChatSessionDocument.session_id.in_(session_ids)))
        for sid, did in rows.all():
            out[sid].append(did)
    return out


def _session_out(chat: ChatSession, document_ids: list[uuid.UUID], starters: list[str] | None = None,
                 warnings: list[str] | None = None) -> SessionOut:
    return SessionOut(id=chat.id, title=chat.title, all_documents=chat.all_documents, document_ids=document_ids,
                      created_at=chat.created_at, last_message_at=chat.last_message_at,
                      starter_questions=starters or [], warnings=warnings or [])


async def create_session(db: AsyncSession, owner_id: uuid.UUID, body: SessionCreate) -> SessionOut:
    """Scope = the given documents (each must be the caller's: 404 otherwise) or all of the caller's documents.
    At least one must be ready (409 NO_READY_DOCUMENTS): a chat with nothing to search can only refuse."""
    document_ids = [] if body.all_documents else list(dict.fromkeys(body.document_ids))
    if not body.all_documents and not document_ids:
        raise AppError(ErrorCode.VALIDATION_ERROR, 422, "Provide document_ids or set all_documents=true.")
    if document_ids:
        owned = set(await db.scalars(select(Document.id).where(Document.owner_id == owner_id,
                                                               Document.id.in_(document_ids))))
        if owned != set(document_ids):
            raise not_found(ErrorCode.DOCUMENT_NOT_FOUND, "Document")

    chat = ChatSession(id=uuid.uuid4(), owner_id=owner_id, title=body.title, all_documents=body.all_documents)
    db.add(chat)
    await db.flush()
    for document_id in document_ids:
        db.add(ChatSessionDocument(session_id=chat.id, document_id=document_id))
    await db.flush()
    scope = await _scope(db, owner_id, chat)
    if not scope.ready_ids:
        await db.rollback()
        raise AppError(ErrorCode.NO_READY_DOCUMENTS, 409, "None of the documents in scope is ready yet.",
                       {"documents": [{"id": str(d.id), "status": d.status} for d in scope.docs]})
    starters = await _starter_questions(db, owner_id, scope.ready_ids)
    await db.commit()
    await db.refresh(chat)
    return _session_out(chat, document_ids, starters, scope.warnings)


async def get_session(db: AsyncSession, owner_id: uuid.UUID, session_id: uuid.UUID) -> SessionOut:
    chat = await _owned_session(db, owner_id, session_id)
    scope = await _scope(db, owner_id, chat)
    document_ids = (await _session_document_ids(db, [chat.id]))[chat.id]
    return _session_out(chat, document_ids, await _starter_questions(db, owner_id, scope.ready_ids), scope.warnings)


async def list_sessions(
    db: AsyncSession, owner_id: uuid.UUID, *, limit: int, cursor: str | None
) -> tuple[list[SessionOut], str | None]:
    """Newest activity first: last_message_at, or created_at for a session with no messages yet. Keyset-paginated
    on (activity, id) so concurrent new messages never shift or duplicate a page."""
    activity = func.coalesce(ChatSession.last_message_at, ChatSession.created_at)
    stmt = select(ChatSession, activity.label("activity")).where(ChatSession.owner_id == owner_id)
    c = decode_cursor(cursor)
    if c:
        ts, last_id = _session_cursor(c)
        stmt = stmt.where(tuple_(activity, ChatSession.id) < tuple_(ts, last_id))
    rows = (await db.execute(stmt.order_by(activity.desc(), ChatSession.id.desc()).limit(limit + 1))).all()
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor({"t": rows[-1].activity.isoformat(), "id": str(rows[-1].ChatSession.id)})
    doc_ids = await _session_document_ids(db, [r.ChatSession.id for r in rows])
    return [_session_out(r.ChatSession, doc_ids[r.ChatSession.id]) for r in rows], next_cursor


# A cursor is client input: it is accepted only in exactly the shape we encode, and anything else is a 400
# INVALID_CURSOR. Validating here (not just catching parse errors) is what keeps a crafted value out of the SQL,
# e.g. {"seq": 1e20} used to reach Postgres and fail as "bigint out of range" -> 500.
def _invalid_cursor() -> AppError:
    return AppError(ErrorCode.INVALID_CURSOR, 400, "Invalid pagination cursor.")


def _session_cursor(c: dict[str, Any]) -> tuple[datetime, uuid.UUID]:
    """{"t": ISO timestamp, "id": session uuid}, both strings (as encode_cursor wrote them)."""
    t, last_id = c.get("t"), c.get("id")
    if not isinstance(t, str) or not isinstance(last_id, str):
        raise _invalid_cursor()
    try:
        return datetime.fromisoformat(t), uuid.UUID(last_id)
    except ValueError as exc:
        raise _invalid_cursor() from exc


def _seq_cursor(c: dict[str, Any]) -> int:
    """{"seq": n}: a JSON integer in bigint range (bool is an int subclass in Python, so it is rejected explicitly)."""
    seq = c.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or not 0 < seq <= BIGINT_MAX:
        raise _invalid_cursor()
    return seq


async def delete_session(db: AsyncSession, owner_id: uuid.UUID, session_id: uuid.UUID) -> None:
    chat = await _owned_session(db, owner_id, session_id)
    await db.execute(delete(ChatSession).where(ChatSession.id == chat.id))   # cascades messages + scope rows
    await db.commit()


async def list_messages(
    db: AsyncSession, owner_id: uuid.UUID, session_id: uuid.UUID, *, limit: int, cursor: str | None
) -> tuple[list[MessageOut], str | None]:
    """The newest `limit` messages, returned oldest-first (ready to render). next_cursor holds the smallest seq on
    this page; passing it back returns the `limit` messages before it."""
    await _owned_session(db, owner_id, session_id)
    stmt = select(ChatMessage).where(ChatMessage.session_id == session_id)
    c = decode_cursor(cursor)
    if c:
        stmt = stmt.where(ChatMessage.seq < _seq_cursor(c))
    rows = list(await db.scalars(stmt.order_by(ChatMessage.seq.desc()).limit(limit + 1)))
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor({"seq": rows[-1].seq})
    rows.reverse()
    return [MessageOut.model_validate(m) for m in rows], next_cursor


# =============================================================================================================
# Phase 1: start a turn (runs before the response starts, so errors get real HTTP status codes)
# =============================================================================================================
@dataclass(frozen=True)
class Turn:
    owner_id: uuid.UUID
    session_id: uuid.UUID
    user_message_id: uuid.UUID
    user_seq: int
    question: str
    started: float                              # perf_counter() when the request arrived (ttft / latency base)
    assistant_message_id: uuid.UUID | None = None
    document_ids: list[uuid.UUID] = field(default_factory=list)     # ready documents in scope
    document_titles: list[str] = field(default_factory=list)       # for the gate's "Your documents cover: ..."
    warnings: list[str] = field(default_factory=list)              # docs_not_ready
    replay: ChatMessage | None = None           # a completed answer to the same client_message_id: re-send it


_EXPIRE_STALE_STREAMS = text("""
UPDATE chat_messages SET status = 'interrupted'
WHERE session_id = :session_id AND status = 'streaming'
  AND created_at < now() - make_interval(secs => :stale_s)
""")


async def _latest_reply(db: AsyncSession, user_message_id: uuid.UUID) -> ChatMessage | None:
    return await db.scalar(select(ChatMessage).where(ChatMessage.reply_to_id == user_message_id)
                           .order_by(ChatMessage.seq.desc()).limit(1))


def _conflict(exc: IntegrityError) -> AppError | None:
    """Map the two unique constraints a turn can hit to their API errors (None = some other integrity error)."""
    constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    if constraint == "uq_chat_messages_client_id":    # the same client_message_id raced us
        return AppError(ErrorCode.MESSAGE_IN_PROGRESS, 409, "This message is already being answered.")
    if constraint == "ux_chat_messages_one_streaming":
        return AppError(ErrorCode.SESSION_BUSY, 409, "Another answer is still streaming in this session.")
    return None


async def start_turn(owner_id: uuid.UUID, session_id: uuid.UUID, body: MessageCreate) -> Turn:
    """One short transaction: expire stale streams, apply idempotency, insert user row + assistant placeholder.

    Idempotency on client_message_id (a client retrying after a network blip must not pay twice):
      key reused for a different question -> 422 VALIDATION_ERROR (never answer question B with question A's reply)
      previous answer complete    -> replay it (no model call, no new rows)
      previous answer streaming   -> 409 MESSAGE_IN_PROGRESS
      failed / interrupted / none -> regenerate: new assistant row replying to the SAME user row
    """
    started = time.perf_counter()
    async with async_session() as db:
        chat = await _owned_session(db, owner_id, session_id)
        # A placeholder still 'streaming' after STREAMING_STALE_S belongs to a crashed or abandoned request.
        await db.execute(_EXPIRE_STALE_STREAMS, {"session_id": session_id, "stale_s": settings.STREAMING_STALE_S})

        existing_user: ChatMessage | None = None
        if body.client_message_id:
            existing_user = await db.scalar(select(ChatMessage).where(
                ChatMessage.session_id == session_id, ChatMessage.role == "user",
                ChatMessage.client_message_id == body.client_message_id))
        if existing_user is not None:
            if existing_user.content != body.content:
                # Stripe-style "same key, different parameters": a real retry resends the identical body. The user
                # row IS the key's original request, so comparing its content needs no stored hash.
                raise AppError(ErrorCode.VALIDATION_ERROR, 422,
                               "client_message_id was already used for a different question; "
                               "send a new client_message_id for a new question.",
                               {"field": "client_message_id"})
            previous = await _latest_reply(db, existing_user.id)
            if previous is not None and previous.status == "complete":
                await db.commit()
                return Turn(owner_id, session_id, existing_user.id, existing_user.seq, existing_user.content, started,
                            assistant_message_id=previous.id, replay=previous)
            if previous is not None and previous.status == "streaming":
                raise AppError(ErrorCode.MESSAGE_IN_PROGRESS, 409, "This message is already being answered.")

        scope = await _scope(db, owner_id, chat)
        if not scope.ready_ids:
            raise AppError(ErrorCode.NO_READY_DOCUMENTS, 409, "None of the documents in scope is ready.")

        assistant_id = uuid.uuid4()
        try:
            if existing_user is None:
                user_id, question = uuid.uuid4(), body.content
                user_seq = await db.scalar(insert(ChatMessage).values(
                    id=user_id, session_id=session_id, role="user", content=question, status="complete",
                    client_message_id=body.client_message_id).returning(ChatMessage.seq))
            else:
                user_id, question, user_seq = existing_user.id, existing_user.content, existing_user.seq
            await db.execute(insert(ChatMessage).values(
                id=assistant_id, session_id=session_id, role="assistant", content="", status="streaming",
                reply_to_id=user_id, warnings=scope.warnings))
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            conflict = _conflict(exc)
            if conflict is None:
                raise
            raise conflict from exc
    return Turn(owner_id, session_id, user_id, int(user_seq), question, started, assistant_message_id=assistant_id,
                document_ids=scope.ready_ids, document_titles=scope.ready_titles, warnings=list(scope.warnings))


# =============================================================================================================
# Phase 2: answer a turn (one async generator for both endpoints)
# =============================================================================================================
@dataclass
class _Run:
    """Everything known about the turn so far. Lets any exit path (complete, error, disconnect) save a consistent
    row: whatever was spent (condense, embed, partial answer) is recorded even if the client is gone. A condense or
    follow-ups call cut off mid-flight is recorded as an estimate; a query embedding cut off mid-flight is not (it
    can't be told apart from a cancelled cache lookup, and costs ~$0.000001)."""

    turn: Turn
    warnings: list[str]
    timings: dict[str, int] = field(default_factory=dict)
    pending_usage: list[AIUsage] = field(default_factory=list)   # ai_usage rows saved with the message
    standalone: str | None = None
    retrieval: RetrievalResult | None = None
    numbered: dict[str, RetrievedChunk] = field(default_factory=dict)
    gate: bool = False
    prompt: AnswerPrompt | None = None
    model: str | None = None
    content: str = ""
    answer_usage: Usage | None = None
    ttft_ms: int | None = None
    latency_ms: int | None = None
    finished: bool = False

    def warn(self, code: str) -> None:
        if code not in self.warnings:
            self.warnings.append(code)

    def record(self, operation: str, usage: Usage, *, prompt_version: str | None = None, cache_hit: bool = False,
               error_code: str | None = None, success: bool | None = None) -> None:
        """success defaults to "no error code"; a call cut off by a disconnect passes success=False with no code,
        like the interrupted answer row."""
        self.pending_usage.append(usage_row(
            operation=operation, usage=usage, owner_id=self.turn.owner_id, prompt_version=prompt_version,
            message_id=self.turn.assistant_message_id, cache_hit=cache_hit,
            success=error_code is None if success is None else success, error_code=error_code))


async def answer(turn: Turn) -> AsyncIterator[ChatEvent]:
    """Yield meta, token*, citations, followups?, done — or an error event. Saves the message on every exit path."""
    if turn.replay is not None:
        for event in replay_events(turn.replay, turn.user_message_id):
            yield event
        return
    run = _Run(turn, warnings=list(turn.warnings))
    try:
        # aclosing: when the client goes away we are closed at a `yield`; an inner generator is NOT closed
        # automatically, so we close it explicitly (down to the provider's HTTP stream) instead of waiting for GC.
        async with aclosing(_pipeline(run, get_provider())) as events:
            async for event in events:
                yield event
    except Exception as exc:  # noqa: BLE001 - a bug must still end the stream cleanly and free the session
        log.exception("chat_turn_failed", message_id=str(turn.assistant_message_id), error=str(exc))
        if not run.finished:
            await _save(run, "failed")
            yield ErrorEvent(code=ErrorCode.INTERNAL, message="The answer failed unexpectedly.")
    finally:
        if not run.finished:
            # Client disconnected (GeneratorExit) or the task was cancelled (CancelledError): save what we have.
            # Shielded, because a cancelled task would otherwise be cancelled again at the first DB await.
            with anyio.CancelScope(shield=True):
                try:
                    await _save(run, "interrupted")
                except Exception as exc:  # noqa: BLE001 - never mask the original exit reason
                    log.exception("chat_interrupted_save_failed", error=str(exc))


async def _pipeline(run: _Run, provider: LLMProvider) -> AsyncIterator[ChatEvent]:
    turn = run.turn
    async with async_session() as db:
        history = await load_history(db, owner_id=turn.owner_id, session_id=turn.session_id,
                                     before_seq=turn.user_seq)
    query, query_vec = await _understand(run, provider, history)
    await _retrieve(run, query, query_vec)
    yield _meta_event(run)

    if run.gate:
        run.content = gate_reply(turn.document_titles)
        run.ttft_ms = _ms(turn.started)
        yield TokenEvent(delta=run.content)
    else:
        async with aclosing(_stream(run, provider, history)) as events:
            async for event in events:
                yield event
        if run.finished:            # the stream failed: an error event was sent and the partial answer saved
            return

    run.latency_ms = _ms(turn.started)
    check = check_answer(run.content, run.numbered, today=run.prompt.today if run.prompt else None)
    citations_event = _citations_event(run, check)
    # Saved BEFORE the event goes out: a client leaving at that yield must not turn a fully streamed answer into an
    # 'interrupted' one. Shielded, so a disconnect or the JSON deadline arriving mid-commit waits for it instead of
    # leaving a row the cleanup can't know was written.
    with anyio.CancelScope(shield=True):
        await _save(run, "complete", citations_event=citations_event)
    yield citations_event

    followups = await _followups(run, provider, check)
    if followups:
        yield FollowupsEvent(questions=followups)
    yield _done_event(run)


async def _understand(run: _Run, provider: LLMProvider, history: History) -> tuple[str, list[float] | None]:
    """Steps 2-3: condense (only with history) and embed the query (cached). Both degrade instead of failing."""
    try:
        condensed = await condense_question(provider, history, run.turn.question)
    except BaseException:
        # Only cancellation gets here (condense_question absorbs every Exception): the client left while the call was
        # in flight. The provider still bills it, so record an estimate; answer()'s shielded 'interrupted' save
        # writes pending_usage. With no history there was no call to record.
        if history.messages:
            run.record("condense", estimated_condense_usage(history, run.turn.question), success=False,
                       prompt_version=condense_prompt.VERSION)
        raise
    run.standalone = condensed.rewritten
    if condensed.called:            # timings only hold steps that ran: a first turn has no "condense" key
        run.timings["condense"] = condensed.ms
        run.record("condense", condensed.usage or Usage(model=settings.FAST_MODEL), error_code=condensed.error,
                   prompt_version=condense_prompt.VERSION)
    if condensed.fallback:
        run.warn("condense_fallback")

    embedding = await _embed(provider, condensed.query)
    if embedding is not None:
        run.timings["embed"] = embedding.ms
        run.record("embed_query", embedding.usage, cache_hit=embedding.cache_hit, error_code=embedding.error)
        if embedding.vector is None:
            run.warn("keyword_only")
    return condensed.query, embedding.vector if embedding else None


async def _embed(provider: LLMProvider, query: str) -> QueryEmbedding | None:
    if settings.RETRIEVAL_MODE == "keyword":     # the vector leg is switched off: don't pay for an embedding
        return None
    return await embed_query(provider, query)


async def _retrieve(run: _Run, query: str, query_vec: list[float] | None) -> None:
    """Step 4-5: hybrid retrieval in one short transaction, then the gate decision."""
    started = time.perf_counter()
    async with async_session() as db:
        result = await retrieve(db, owner_id=run.turn.owner_id, document_ids=run.turn.document_ids,
                                query_text=query, query_vec=query_vec)
    run.timings["retrieve"] = _ms(started)
    run.retrieval = result
    run.gate = should_gate(result, has_query_vector=query_vec is not None)
    run.numbered = {} if run.gate else number_sources(result.chunks)


async def _stream(run: _Run, provider: LLMProvider, history: History) -> AsyncIterator[ChatEvent]:
    """Step 7: stream the answer model, with the first-token fallback and the idle budget."""
    run.prompt = build_answer_prompt(history=history.messages, question=run.turn.question,
                                     standalone=run.standalone, numbered=run.numbered)
    run.model = settings.ANSWER_MODEL
    try:
        async with aclosing(stream_answer(provider, instructions=run.prompt.instructions,
                                          input=run.prompt.input)) as items:
            async for item in items:
                if isinstance(item, FallbackUsed):
                    log.warning("answer_fallback", failed_model=item.failed_model, reason=item.message)
                    run.record("answer", _abandoned_attempt_usage(run.prompt, item),
                               prompt_version=run.prompt.prompt_version, error_code=item.error_code)
                    run.model = settings.FAST_MODEL
                    run.warn("fallback_model")
                elif isinstance(item, StreamEnd):
                    run.answer_usage = item.usage
                    if item.status == "incomplete":
                        # max_output_tokens was hit (reasoning tokens count too), so the answer is cut off or even
                        # empty. Keep what the user saw, but don't present it as a finished answer.
                        log.warning("answer_truncated", model=run.model, chars=len(run.content))
                        run.warn("truncated")
                elif item:
                    if run.ttft_ms is None:
                        run.ttft_ms = _ms(run.turn.started)
                    run.content += item
                    yield TokenEvent(delta=item)
    except AnswerStalled as exc:
        yield await _fail(run, ErrorCode.UPSTREAM_TIMEOUT, f"The answer stalled: {exc}")
    except ProviderError as exc:
        if exc.usage is not None:       # the model ran and billed but gave no usable answer: its own numbers
            run.answer_usage = exc.usage
        elif not run.content:           # failed before any output, like an abandoned first attempt: zero tokens
            run.answer_usage = Usage(model=run.model or settings.ANSWER_MODEL)
        # exc.message can be the vendor's own text (a 401 explains what is wrong with the API key): logs only.
        yield await _fail(run, ErrorCode.UPSTREAM_AI_ERROR, "The AI provider could not answer. Please try again.",
                          detail=f"{exc.kind}: {exc.message}")
    except TimeoutError:
        yield await _fail(run, ErrorCode.UPSTREAM_TIMEOUT, "No answer from the AI provider in time.")


def _abandoned_attempt_usage(prompt: AnswerPrompt, fallback: FallbackUsed) -> Usage:
    """A first attempt that timed out was still processing (and billing) our input, so estimate it; one that failed
    with an error (5xx, connection) is recorded at zero tokens; one that ran but gave no answer text (refusal, output
    budget spent on reasoning) is recorded with the usage the provider reported."""
    if fallback.usage is not None:
        return fallback.usage
    if fallback.error_code == ErrorCode.UPSTREAM_TIMEOUT:
        return Usage(model=fallback.failed_model, input_tokens=count_tokens(prompt.text()), estimated=True)
    return Usage(model=fallback.failed_model)


async def _fail(run: _Run, code: ErrorCode, message: str, *, detail: str | None = None) -> ErrorEvent:
    """`message` is what the client sees (event, JSON body); `detail` (vendor text) is only logged."""
    log.warning("answer_failed", code=code, message=message, detail=detail, partial_chars=len(run.content))
    await _save(run, "failed", error_code=code)
    return ErrorEvent(code=code, message=message)


async def _followups(run: _Run, provider: LLMProvider, check: AnswerCheck) -> list[str]:
    """Step 10: best effort, after the answer is saved. Stored with a separate tiny UPDATE."""
    if run.gate or run.retrieval is None:
        return []
    cited = {run.numbered[c.ref].chunk_id for c in check.citations}
    try:
        result = await suggest_followups(provider, question=run.turn.question, answer=run.content,
                                         chunks=run.retrieval.chunks, cited_chunk_ids=cited)
    except BaseException:
        # Cancelled mid-call (client pressed Stop / disconnected). The answer was already saved, so answer()'s
        # finally won't save anything: record the billed-but-unseen call here. Shielded, because a cancelled task
        # would otherwise be cancelled again at the first DB await.
        if run.retrieval.chunks:        # with no chunks suggest_followups makes no call
            usage = estimated_followups_usage(run.turn.question, run.content, run.retrieval.chunks, cited)
            with anyio.CancelScope(shield=True):
                await _save_followups(run, [], usage, success=False)
        raise
    if result.usage is None and result.error is None:     # no model call was made: nothing to store or bill
        return result.questions
    await _save_followups(run, result.questions, result.usage or Usage(model=settings.FAST_MODEL),
                          success=result.error is None, error_code=result.error)
    return result.questions


async def _save_followups(run: _Run, questions: list[str], usage: Usage, *, success: bool,
                          error_code: str | None = None) -> None:
    """The chips (if any) and the follow-ups ai_usage row, in one tiny transaction of their own."""
    try:
        async with async_session() as db:
            if questions:
                await db.execute(update(ChatMessage).where(ChatMessage.id == run.turn.assistant_message_id)
                                 .values(followups=questions))
            db.add(usage_row(operation="followups", usage=usage, owner_id=run.turn.owner_id,
                             prompt_version=followups_prompt.VERSION, message_id=run.turn.assistant_message_id,
                             success=success, error_code=error_code))
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - the answer is already saved; chips are optional
        log.warning("followups_save_failed", error=str(exc))


# --- Persistence -----------------------------------------------------------------------------------------------
def _answer_usage(run: _Run) -> Usage | None:
    """The provider's own usage when the stream finished (or zero for one that failed before any output, see
    _stream); otherwise a local tiktoken estimate (estimated=true), because an abandoned stream never delivers its
    final usage event but was still billed."""
    if run.answer_usage is not None:
        return run.answer_usage
    if run.prompt is None or run.model is None:
        return None
    return Usage(model=run.model, input_tokens=count_tokens(run.prompt.text()),
                 output_tokens=count_tokens(run.content), estimated=True)


def _retrieval_json(run: _Run) -> dict[str, Any] | None:
    r = run.retrieval
    if r is None:
        return None
    refs = {c.chunk_id: ref for ref, c in run.numbered.items()}
    return {
        "mode": r.mode, "gate": run.gate, "top_similarity": r.top_similarity, "terms": r.terms,
        "id_terms": r.id_terms,
        "candidates": [
            {"chunk_id": c.chunk_id, "ref": refs.get(c.chunk_id), "rrf": round(c.rrf, 6), "sem_rank": c.sem_rank,
             "lex_rank": c.lex_rank, "sem_dist": None if c.sem_dist is None else round(c.sem_dist, 6),
             "id_hit": c.id_hit, "document_id": str(c.document_id), "filename": c.filename, "label": c.label,
             "page_start": c.page_start, "page_end": c.page_end}
            for c in r.chunks
        ],
    }


async def _save(run: _Run, status: str, *, citations_event: CitationsEvent | None = None,
                error_code: str | None = None) -> None:
    """Step 9 (and every abnormal exit): ONE short transaction writes the assistant row, every ai_usage row of the
    turn and the session's activity time. Committing it is also what releases SESSION_BUSY."""
    turn = run.turn
    total_ms = _ms(turn.started)
    if run.ttft_ms is not None:
        run.timings["ttft"] = run.ttft_ms
    run.timings["total"] = total_ms
    usage = _answer_usage(run)
    values: dict[str, Any] = {
        "status": status, "content": run.content, "standalone_query": run.standalone,
        "retrieval": _retrieval_json(run), "warnings": run.warnings, "model": run.model,
        "prompt_version": run.prompt.prompt_version if run.prompt else None,
        "input_tokens": usage.input_tokens if usage else 0,
        "cached_input_tokens": usage.cached_input_tokens if usage else 0,
        "output_tokens": usage.output_tokens if usage else 0,
        "cost_usd": cost_usd(usage) if usage else 0,
        "ttft_ms": run.ttft_ms, "latency_ms": run.latency_ms or total_ms, "timings": dict(run.timings),
    }
    if citations_event is not None:          # only a complete answer has been checked
        values |= {
            "answer_type": citations_event.answer_type, "confidence": citations_event.confidence,
            "signals": citations_event.signals.model_dump(),
            "citations": [c.model_dump(mode="json", exclude={"source_deleted"}) for c in citations_event.citations],
        }
    rows = list(run.pending_usage)
    if usage is not None:
        rows.append(usage_row(operation="answer", usage=usage, owner_id=turn.owner_id,
                              prompt_version=values["prompt_version"], message_id=turn.assistant_message_id,
                              success=status == "complete", error_code=error_code))
    async with async_session() as db:
        await db.execute(update(ChatMessage).where(ChatMessage.id == turn.assistant_message_id).values(**values))
        db.add_all(rows)
        await db.execute(
            update(ChatSession).where(ChatSession.id == turn.session_id, ChatSession.owner_id == turn.owner_id)
            .values(last_message_at=func.now(),
                    title=func.coalesce(func.nullif(ChatSession.title, ""), turn.question[:TITLE_CHARS].strip()))
        )
        await db.commit()
        # Saved means committed, with no await in between: a cancel landing while the session closes must not let
        # answer()'s finally save the turn again (overwriting it) and insert its ai_usage rows a second time.
        run.pending_usage.clear()
        run.finished = True
    log.info("chat_answer_saved", message_id=str(turn.assistant_message_id), status=status,
             answer_type=values.get("answer_type"), model=run.model, total_ms=total_ms)


# --- Events ----------------------------------------------------------------------------------------------------
def _meta_event(run: _Run) -> MetaEvent:
    return MetaEvent(
        message_id=run.turn.assistant_message_id, user_message_id=run.turn.user_message_id,
        standalone_query=run.standalone,
        sources=[SourceRef(ref=ref, chunk_id=c.chunk_id, document_id=c.document_id, filename=c.filename,
                           label=c.label) for ref, c in run.numbered.items()],
        retrieval_mode=run.retrieval.mode if run.retrieval else "none", warnings=list(run.warnings),
    )


def _citations_event(run: _Run, check: AnswerCheck) -> CitationsEvent:
    citations = []
    for c in check.citations:
        chunk = run.numbered[c.ref]
        citations.append(Citation(ref=c.ref, chunk_id=chunk.chunk_id, document_id=chunk.document_id,
                                  filename=chunk.filename, label=chunk.label, page_start=chunk.page_start,
                                  page_end=chunk.page_end, snippet=c.snippet, weak_support=c.weak_support))
    cited_lexical = any(run.numbered[c.ref].lex_rank is not None for c in check.citations)
    top = run.retrieval.top_similarity if run.retrieval else None
    signals = Signals(
        top_similarity=None if top is None else round(top, 4), id_hit=bool(run.retrieval and run.retrieval.id_hit),
        lexical_match=cited_lexical, valid_citations=check.valid_citations,
        invalid_citations=check.invalid_citations, uncited_claims=check.uncited_claims,
        ungrounded_facts=check.ungrounded_facts, gate=run.gate,
    )
    level = confidence(answer_type=check.answer_type, valid_citations=check.valid_citations,
                       invalid_citations=check.invalid_citations, uncited_claims=check.uncited_claims,
                       any_weak_support=any(c.weak_support for c in check.citations), top_similarity=top,
                       lexical_match=cited_lexical)
    return CitationsEvent(citations=citations, answer_type=check.answer_type, confidence=level, signals=signals)


def _usage_out(input_tokens: int | None, cached: int | None, output_tokens: int | None, cost: Any,
               estimated: bool = False) -> UsageOut:
    return UsageOut(input_tokens=input_tokens or 0, cached_input_tokens=cached or 0,
                    output_tokens=output_tokens or 0, cost_usd=cost or 0, estimated=estimated)


def _done_event(run: _Run) -> DoneEvent:
    usage = _answer_usage(run)
    return DoneEvent(
        message_id=run.turn.assistant_message_id,
        usage=_usage_out(usage.input_tokens, usage.cached_input_tokens, usage.output_tokens, cost_usd(usage),
                         usage.estimated) if usage else UsageOut(),
        model=run.model, ttft_ms=run.ttft_ms, latency_ms=run.latency_ms or _ms(run.turn.started),
        timings=dict(run.timings), warnings=list(run.warnings),
    )


def replay_events(msg: ChatMessage, user_message_id: uuid.UUID) -> list[ChatEvent]:
    """Idempotent retry of a completed answer: the same event sequence, rebuilt from the saved row. No model call."""
    retrieval = msg.retrieval or {}
    sources = [] if retrieval.get("gate") else [
        SourceRef(ref=c["ref"], chunk_id=c["chunk_id"], document_id=c["document_id"], filename=c["filename"],
                  label=c["label"])
        for c in retrieval.get("candidates", []) if c.get("ref") and c.get("document_id")
    ]
    events: list[ChatEvent] = [
        MetaEvent(message_id=msg.id, user_message_id=user_message_id, standalone_query=msg.standalone_query,
                  sources=sources, retrieval_mode=retrieval.get("mode", "none"), warnings=list(msg.warnings or [])),
        TokenEvent(delta=msg.content),
        CitationsEvent(citations=[Citation(**c) for c in msg.citations or []],
                       answer_type=msg.answer_type or "ungrounded", confidence=msg.confidence or "low",
                       signals=Signals(**(msg.signals or {}))),
    ]
    if msg.followups:
        events.append(FollowupsEvent(questions=list(msg.followups)))
    events.append(DoneEvent(message_id=msg.id,
                            usage=_usage_out(msg.input_tokens, msg.cached_input_tokens, msg.output_tokens,
                                             msg.cost_usd),
                            model=msg.model, ttft_ms=msg.ttft_ms, latency_ms=msg.latency_ms or 0,
                            timings=dict(msg.timings or {}), warnings=list(msg.warnings or [])))
    return events


# --- JSON endpoint ---------------------------------------------------------------------------------------------
_ERROR_STATUS = {ErrorCode.UPSTREAM_TIMEOUT: 504, ErrorCode.UPSTREAM_AI_ERROR: 502}


async def answer_json(turn: Turn) -> AskResponse:
    """Drain the same generator as the SSE endpoint under one overall deadline, then return the saved rows.

    The deadline is an anyio scope, not asyncio.timeout: only an anyio cancellation waits for the shielded save of a
    complete answer instead of cutting it mid-commit."""
    error: ErrorEvent | None = None
    try:
        with anyio.fail_after(settings.JSON_ANSWER_TIMEOUT_S):
            async with aclosing(answer(turn)) as events:
                async for event in events:
                    if isinstance(event, ErrorEvent):
                        error = event
    except TimeoutError as exc:
        if not await _is_complete(turn):
            # The generator's finally already saved the partial answer as 'interrupted'; for the client it failed.
            await _mark_failed(turn)
            raise AppError(ErrorCode.UPSTREAM_TIMEOUT, 504,
                           f"No complete answer within {settings.JSON_ANSWER_TIMEOUT_S:g}s.") from exc
        # Time ran out in the best-effort follow-ups, after the answer was saved complete (a retry would replay it):
        # return it without chips rather than call it a failure.
    if error is not None:
        raise AppError(ErrorCode(error.code), _ERROR_STATUS.get(error.code, 500), error.message,
                       {"message_id": str(turn.assistant_message_id)})
    async with async_session() as db:
        user_msg = await _owned_message(db, turn, turn.user_message_id)
        answer_msg = await _owned_message(db, turn, turn.assistant_message_id)
    return AskResponse(user_message=MessageOut.model_validate(user_msg), answer=MessageOut.model_validate(answer_msg))


async def _owned_message(db: AsyncSession, turn: Turn, message_id: uuid.UUID | None) -> ChatMessage:
    msg = await db.scalar(select(ChatMessage).join(ChatSession, ChatSession.id == ChatMessage.session_id)
                          .where(ChatMessage.id == message_id, ChatSession.owner_id == turn.owner_id))
    if msg is None:
        raise not_found(ErrorCode.SESSION_NOT_FOUND, "Chat session")
    return msg


async def _is_complete(turn: Turn) -> bool:
    async with async_session() as db:
        return (await _owned_message(db, turn, turn.assistant_message_id)).status == "complete"


async def _mark_failed(turn: Turn) -> None:
    async with async_session() as db:
        await db.execute(update(ChatMessage).where(ChatMessage.id == turn.assistant_message_id,
                                                   ChatMessage.status.in_(["interrupted", "streaming"]))
                         .values(status="failed"))
        await db.commit()


# =============================================================================================================
# Retrieval debugging: GET /search (no LLM call)
# =============================================================================================================
async def ready_document_ids(db: AsyncSession, owner_id: uuid.UUID,
                             requested: Sequence[uuid.UUID] | None = None) -> list[uuid.UUID]:
    """The caller's ready documents, optionally intersected with `requested` (foreign ids silently drop out)."""
    stmt = select(Document.id).where(Document.owner_id == owner_id, Document.status == "ready")
    if requested:
        stmt = stmt.where(Document.id.in_(list(requested)))
    return list(await db.scalars(stmt.order_by(Document.created_at, Document.id)))


async def search(
    owner_id: uuid.UUID, *, q: str, document_ids: Sequence[uuid.UUID] | None, mode: str, k: int,
    per_doc_cap: int | None = None,
) -> SearchResponse:
    """Exactly the retrieval the chat would run for `q` (same SQL, same fusion), with every rank exposed."""
    async with async_session() as db:
        doc_ids = await ready_document_ids(db, owner_id, document_ids)
    embedding = None
    if doc_ids and mode != "keyword":
        embedding = await embed_query(get_provider(), q)
    async with async_session() as db:
        result = await retrieve(db, owner_id=owner_id, document_ids=doc_ids, query_text=q,
                                query_vec=embedding.vector if embedding else None, k=k, per_doc_cap=per_doc_cap,
                                mode=mode)
        if embedding is not None:
            db.add(usage_row(operation="embed_query", usage=embedding.usage, owner_id=owner_id,
                             cache_hit=embedding.cache_hit, success=embedding.error is None,
                             error_code=embedding.error, request_id=get_request_id()))
            await db.commit()
    query_tokens = content_tokens(q)
    hits = [
        SearchHit(chunk_id=c.chunk_id, document_id=c.document_id, filename=c.filename, label=c.label,
                  page_start=c.page_start, page_end=c.page_end, rrf=round(c.rrf, 6), sem_rank=c.sem_rank,
                  lex_rank=c.lex_rank, similarity=None if c.similarity is None else round(c.similarity, 6),
                  id_hit=c.id_hit, snippet=best_sentence(query_tokens, c.content)[0] or c.content[:300],
                  content=c.content)
        for c in result.chunks
    ]
    return SearchResponse(query=q, mode=result.mode, terms=result.terms, id_terms=result.id_terms,
                          top_similarity=None if result.top_similarity is None else round(result.top_similarity, 6),
                          hits=hits)

