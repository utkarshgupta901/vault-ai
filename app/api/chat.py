"""Chat API: sessions scoped to documents, message history, and asking questions (JSON or Server-Sent Events).

Both ask endpoints share one pipeline (`services.chat.answer`). Everything that can be rejected with a proper HTTP
status — auth, session ownership, idempotency, NO_READY_DOCUMENTS, SESSION_BUSY — runs in the `_start_turn`
dependency, i.e. BEFORE the 200 and the first SSE byte are sent. After that, failures arrive as an `error` event.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.sse import EventSourceResponse, ServerSentEvent
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.db import get_db
from app.core.ratelimit import CHAT_GUARDS
from app.schemas.chat import (
    AskResponse,
    CitationsEvent,
    DoneEvent,
    ErrorEvent,
    FollowupsEvent,
    MessageCreate,
    MessageOut,
    MetaEvent,
    SessionCreate,
    SessionOut,
    TokenEvent,
)
from app.schemas.common import ERROR_RESPONSES, Page
from app.services import chat as svc

router = APIRouter(prefix="/chat", tags=["chat"], responses=ERROR_RESPONSES)

User = Annotated[CurrentUser, Depends(get_current_user)]
Db = Annotated[AsyncSession, Depends(get_db)]

EVENT_NAMES: dict[type, str] = {MetaEvent: "meta", TokenEvent: "token", CitationsEvent: "citations",
                                FollowupsEvent: "followups", DoneEvent: "done", ErrorEvent: "error"}
ASK_ERRORS = {409: {"description": "SESSION_BUSY / MESSAGE_IN_PROGRESS / NO_READY_DOCUMENTS"}}


@router.post("/sessions", response_model=SessionOut, status_code=status.HTTP_201_CREATED,
             summary="Start a chat session over some or all of your documents", responses={409: {}})
async def create_session(body: SessionCreate, user: User, db: Db) -> SessionOut:
    """Pass `document_ids` (up to 20) or `all_documents: true`. Returns up to 5 `starter_questions` from the
    documents' insights, and `warnings: ["docs_not_ready"]` when some documents in scope are still processing."""
    return await svc.create_session(db, user.id, body)


@router.get("/sessions", response_model=Page[SessionOut], summary="List sessions (most recent activity first)")
async def list_sessions(
    user: User, db: Db, limit: Annotated[int, Query(ge=1, le=100)] = 20, cursor: str | None = None
) -> Page[SessionOut]:
    items, next_cursor = await svc.list_sessions(db, user.id, limit=limit, cursor=cursor)
    return Page(items=items, next_cursor=next_cursor)


@router.get("/sessions/{session_id}", response_model=SessionOut, summary="Get a session")
async def get_session(session_id: uuid.UUID, user: User, db: Db) -> SessionOut:
    return await svc.get_session(db, user.id, session_id)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a session")
async def delete_session(session_id: uuid.UUID, user: User, db: Db) -> Response:
    await svc.delete_session(db, user.id, session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sessions/{session_id}/messages", response_model=Page[MessageOut],
            summary="Message history (newest page, in chronological order)")
async def list_messages(
    session_id: uuid.UUID, user: User, db: Db,
    limit: Annotated[int, Query(ge=1, le=100)] = 20, cursor: str | None = None,
) -> Page[MessageOut]:
    """Returns the newest `limit` messages ordered by `seq` (oldest first, ready to render). Pass `next_cursor`
    back to load the page of older messages."""
    items, next_cursor = await svc.list_messages(db, user.id, session_id, limit=limit, cursor=cursor)
    return Page(items=items, next_cursor=next_cursor)


async def _start_turn(session_id: uuid.UUID, body: MessageCreate, user: User) -> svc.Turn:
    """Pre-stream checks + the user/placeholder insert. Runs before the response starts, so its errors are real
    HTTP errors (404, 409) instead of an `error` event inside a 200 stream."""
    return await svc.start_turn(user.id, session_id, body)


Turn = Annotated[svc.Turn, Depends(_start_turn)]


@router.post("/sessions/{session_id}/messages", response_model=AskResponse,
             summary="Ask a question (JSON response)", responses={**ASK_ERRORS, 502: {}, 504: {}},
             dependencies=CHAT_GUARDS)  # route dependencies run before _start_turn writes any row
async def ask(turn: Turn) -> AskResponse:
    """Same pipeline as the streaming endpoint, returned as one JSON body once the answer is complete (overall
    deadline JSON_ANSWER_TIMEOUT_S -> 504 UPSTREAM_TIMEOUT). Retrying with the same `client_message_id` returns
    the saved answer instead of paying for a new one."""
    return await svc.answer_json(turn)


@router.post("/sessions/{session_id}/messages/stream", response_class=EventSourceResponse,
             summary="Ask a question (Server-Sent Events)", responses=ASK_ERRORS,
             dependencies=CHAT_GUARDS)
async def ask_stream(turn: Turn) -> AsyncIterator[ServerSentEvent]:
    """Events, in order: `meta` (sources, standalone query, warnings), `token` (repeated), `citations` (checked
    citations, answer type, confidence, signals), `followups` (optional), `done` (usage, timings). A failure after
    the stream started arrives as a terminal `error` event. Read it with fetch() so X-API-Key can be sent; clients
    must ignore unknown event types and fields."""
    async with aclosing(svc.answer(turn)) as events:
        async for event in events:
            yield ServerSentEvent(event=EVENT_NAMES[type(event)], data=event)
