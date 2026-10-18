"""load_history against real rows: which past turns the next question gets as conversation context."""

import uuid
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.db import get_async_engine
from app.rag.history import load_history


async def _session(db: AsyncSession, owner_id: uuid.UUID) -> uuid.UUID:
    session_id = uuid.uuid4()
    await db.execute(text("INSERT INTO chat_sessions (id, owner_id) VALUES (:id, :o)"), {"id": session_id, "o": owner_id})
    return session_id


async def _message(db: AsyncSession, session_id: uuid.UUID, role: str, content: str, *, status: str = "complete",
                   reply_to: uuid.UUID | None = None) -> tuple[uuid.UUID, int]:
    row = (await db.execute(text(
        "INSERT INTO chat_messages (session_id, role, content, status, reply_to_id) "
        "VALUES (:s, :role, :content, :status, :reply_to) RETURNING id, seq"),
        {"s": session_id, "role": role, "content": content, "status": status, "reply_to": reply_to})).one()
    return row.id, row.seq


async def _turn(db: AsyncSession, session_id: uuid.UUID, n: int, reply_status: str | None = "complete",
                answer: str | None = None) -> None:
    user_id, _ = await _message(db, session_id, "user", f"question {n}")
    if reply_status is not None:
        await _message(db, session_id, "assistant", f"answer {n}" if answer is None else answer, status=reply_status,
                       reply_to=user_id)


async def test_failed_and_unanswered_turns_do_not_crowd_out_complete_ones(
    db: AsyncSession, demo_user_id: uuid.UUID, other_user_id: uuid.UUID
) -> None:
    """Regression: the window was the last HISTORY_TURNS*2 rows counting every user row, so questions whose answer
    failed, was interrupted or never came took the slots of real turns (and arrived with no answer)."""
    assert settings.HISTORY_TURNS == 3
    session_id = await _session(db, demo_user_id)
    for n in (1, 2, 3, 4):
        await _turn(db, session_id, n)
    await _turn(db, session_id, 5, reply_status="failed")
    await _turn(db, session_id, 6, reply_status="interrupted")
    await _turn(db, session_id, 7, reply_status=None)               # e.g. the server died before the placeholder
    _, current_seq = await _message(db, session_id, "user", "the question being answered now")
    await db.commit()

    history = await load_history(db, owner_id=demo_user_id, session_id=session_id, before_seq=current_seq)
    assert [(m["role"], m["content"]) for m in history.messages] == [
        ("user", "question 2"), ("assistant", "answer 2"),
        ("user", "question 3"), ("assistant", "answer 3"),
        ("user", "question 4"), ("assistant", "answer 4"),
    ]
    assert history.previous_query == "question 4"                  # the last turn that was actually answered

    foreign = await load_history(db, owner_id=other_user_id, session_id=session_id, before_seq=current_seq)
    assert foreign.messages == [] and foreign.previous_query is None


async def test_a_regenerated_turn_counts_once_with_its_complete_answer(
    db: AsyncSession, demo_user_id: uuid.UUID
) -> None:
    """A retried question keeps ONE user row; its failed first reply must not appear, only the complete one."""
    session_id = await _session(db, demo_user_id)
    user_id, _ = await _message(db, session_id, "user", "question 1")
    await _message(db, session_id, "assistant", "half an ans", status="failed", reply_to=user_id)
    await _message(db, session_id, "assistant", "answer 1", reply_to=user_id)
    _, current_seq = await _message(db, session_id, "user", "question 2")
    await db.commit()

    history = await load_history(db, owner_id=demo_user_id, session_id=session_id, before_seq=current_seq)
    assert [(m["role"], m["content"]) for m in history.messages] == [("user", "question 1"), ("assistant", "answer 1")]


async def test_a_blank_complete_answer_is_not_a_turn(db: AsyncSession, demo_user_id: uuid.UUID) -> None:
    """Before the empty-answer fix an answer stream with no text was saved as an empty 'complete' message, and such
    rows are still in existing databases. They took history slots and left their question there without an answer."""
    session_id = await _session(db, demo_user_id)
    for n in (1, 2, 3):
        await _turn(db, session_id, n)
    await _turn(db, session_id, 4, answer="")
    await _turn(db, session_id, 5, answer=" \n")
    _, current_seq = await _message(db, session_id, "user", "the question being answered now")
    await db.commit()

    history = await load_history(db, owner_id=demo_user_id, session_id=session_id, before_seq=current_seq)
    assert [(m["role"], m["content"]) for m in history.messages] == [
        ("user", "question 1"), ("assistant", "answer 1"),
        ("user", "question 2"), ("assistant", "answer 2"),
        ("user", "question 3"), ("assistant", "answer 3"),
    ]
    assert history.previous_query == "question 3"


async def test_the_history_query_reads_only_this_sessions_messages(db: AsyncSession, demo_user_id: uuid.UUID) -> None:
    """Regression: the reply side of the join was found by reply_to_id alone, which no index covers, so every chat
    turn with prior turns scanned every owner's chat_messages. Checked on the plan of the query load_history really
    runs, with sequential scans priced out (the test table is small enough that a scan would win anyway)."""
    session_id = await _session(db, demo_user_id)
    await _turn(db, session_id, 1)
    _, current_seq = await _message(db, session_id, "user", "question 2")
    await db.commit()
    seen: list[tuple[str, Any]] = []

    def capture(conn: Any, cursor: Any, statement: str, parameters: Any, *args: Any) -> None:
        if "reply_to_id" in statement:
            seen.append((statement, parameters))

    engine = get_async_engine().sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        await load_history(db, owner_id=demo_user_id, session_id=session_id, before_seq=current_seq)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    (statement, parameters), = seen
    await db.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join((await (await db.connection()).exec_driver_sql(f"EXPLAIN {statement}", parameters)).scalars())
    await db.rollback()
    assert "Seq Scan on chat_messages" not in plan, plan
