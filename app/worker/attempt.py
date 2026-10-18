"""The single place that holds the worker's reliability rules (design §5).

Celery delivers a message at least once, so the same task can arrive twice, late, or after its worker died.
Every task body runs inside `run_attempt`, which answers four questions in order:

  1. Is another worker on this document right now?   session-level advisory lock; if busy, re-publish in 15 s
                                                       (contention is not a failure and does not use up retries);
                                                       after 10 minutes, fail the run as retryable
  2. Is this task still the current run?              claim = UPDATE ... WHERE current_run_id = :run_id;
                                                       0 rows = superseded (reprocess/delete) or finished -> exit
                                                       (a finished ingest whose insights were never published
                                                       publishes them: its worker died in between)
  3. Did earlier attempts of this run kill a worker?  crash counter: 3 dead attempts -> PROCESSING_CRASHED, so one
                                                       poison file cannot loop forever
  4. What does a failure mean?                        transient -> Celery retry with backoff; permanent -> fail with
                                                       a stable error code that the API shows as error{code,...}

A message that cannot be re-published (broker down) is never dropped silently: the run fails as retryable instead.

Bodies (ingestion/pipeline.py, the insights task) never retry, sleep or write processing_runs themselves.
"""

import random
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Literal

import structlog
from celery import Task
from celery.exceptions import Reject, SoftTimeLimitExceeded
from sqlalchemy import Update, func, insert, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.ai.provider import ProviderError
from app.core.db import get_sync_engine, sync_session
from app.core.errors import ErrorCode, PermanentError
from app.models import Document, ProcessingRun
from app.services import state
from app.services.storage import StorageUnavailable
from app.worker.dispatch import QueueUnavailable, enqueue_insights

log = structlog.get_logger(__name__)

Kind = Literal["ingest", "insights"]

MAX_LOCK_WAITS = 40             # 40 re-publishes x 15 s = 10 minutes of waiting for the lock holder
LOCK_WAIT_COUNTDOWN_S = 15
MAX_CRASHED_ATTEMPTS = 3
MAX_BACKOFF_S = 120

# Codes outside app.core.errors.ErrorCode (documents.error_code / processing_runs.error_code are free text).
PROCESSING_ERROR = "PROCESSING_ERROR"   # an unexpected exception (a bug or an unclassified outage); retryable
SUPERSEDED = "SUPERSEDED"               # processing_runs only: a reprocess or delete took over mid-attempt
TRANSIENT = "TRANSIENT"                 # processing_runs only: this attempt failed and Celery will retry it

_PROVIDER_CODES: dict[str, str] = {
    "refusal": ErrorCode.INSIGHTS_REFUSED,
    "incomplete": ErrorCode.INSIGHTS_TRUNCATED,
    "bad_request": ErrorCode.PROVIDER_BAD_REQUEST,
}

# What the user sees in error.message (technical detail goes to processing_runs.error_message and the logs; a provider
# error stores only its kind and status there, see ProviderError.summary).
_USER_MESSAGES: dict[str, str] = {
    ErrorCode.PROVIDER_UNAVAILABLE: "The AI provider was unavailable after several retries. Reprocess to try again.",
    ErrorCode.PROVIDER_BAD_REQUEST: "The AI provider rejected the request for this document.",
    ErrorCode.INSIGHTS_REFUSED: "The AI model declined to analyse this document.",
    ErrorCode.INSIGHTS_TRUNCATED: "The AI model's answer was cut off before it was complete.",
    ErrorCode.PROCESSING_TIMEOUT: "Processing took too long and was stopped. Reprocess to try again.",
    ErrorCode.PROCESSING_CRASHED: "Processing crashed the worker 3 times, so this file was not retried again.",
    PROCESSING_ERROR: "Unexpected error while processing. Reprocess to try again.",
    ErrorCode.QUEUE_UNAVAILABLE: "The processing queue was unavailable, so processing stopped. Reprocess to try again.",
    ErrorCode.STORAGE_ERROR: "File storage was unavailable after several retries. Reprocess to try again.",
}


class RunSuperseded(Exception):
    """Raised by a body when a fenced write matched no row: the document was reprocessed (new run_id) or deleted.
    Not an error: the attempt stops and writes nothing else to the document."""


@dataclass
class Attempt:
    """What a body receives: the ids its fenced writes and ai_usage rows need, plus a place for stage timings."""

    kind: Kind
    document_id: uuid.UUID
    run_id: uuid.UUID
    owner_id: uuid.UUID
    task_id: str | None
    request_id: str | None
    number: int                 # 1-based attempt number within this run
    row_id: int                 # processing_runs.id of this attempt
    stage_ms: dict[str, int] = field(default_factory=dict)
    started: float = field(default_factory=time.perf_counter)

    def record_stage(self, stage: str, ms: float) -> None:
        self.stage_ms[stage] = self.stage_ms.get(stage, 0) + int(ms)

    @contextmanager
    def timed(self, stage: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self.record_stage(stage, (time.perf_counter() - started) * 1000)

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)


Body = Callable[[Attempt], None]


def backoff(retries: int, retry_after: float | None = None) -> float:
    """Seconds to wait before retry number `retries + 1`.

    The server knows best: honour Retry-After (capped). Otherwise exponential 4, 8, 16, ... capped at 120 s, with
    "equal jitter" (half fixed, half random) so workers that failed together don't all retry in the same second,
    while the wait never collapses to ~0 the way full jitter can."""
    if retry_after and retry_after > 0:
        return min(float(retry_after), MAX_BACKOFF_S)
    base = min(MAX_BACKOFF_S, 4 * 2 ** retries)
    return base / 2 + random.uniform(0, base / 2)


def run_attempt(task: Task, *, kind: Kind, document_id: str, run_id: str, request_id: str | None,
                lock_waits: int, body: Body) -> None:
    doc_id, run = uuid.UUID(str(document_id)), uuid.UUID(str(run_id))
    try:
        got_lock = _run_if_unlocked(task, kind, doc_id, run, request_id, body)
    except OperationalError as exc:
        # Postgres itself is unreachable, so not even the attempt row can be written: let Celery try again later.
        if task.request.retries >= task.max_retries:
            raise
        log.warning("attempt_db_unavailable", kind=kind, error=str(exc))
        raise task.retry(countdown=backoff(task.request.retries)) from exc
    if not got_lock:
        _republish_later(task, kind=kind, document_id=document_id, run_id=run_id, request_id=request_id,
                         lock_waits=lock_waits)


def _run_if_unlocked(task: Task, kind: Kind, document_id: uuid.UUID, run_id: uuid.UUID, request_id: str | None,
                     body: Body) -> bool:
    """Returns False if another worker holds this document's lock. Either way the lock is released on return, so a
    re-publish never happens while we hold it (in eager tests the re-published task runs inline)."""
    with advisory_lock(f"{kind}:{document_id}") as acquired:
        if not acquired:
            return False
        attempt = _claim_and_start(task, kind, document_id, run_id, request_id)
        if attempt is not None:
            _execute(task, attempt, body)
        elif kind == "ingest":
            _republish_lost_insights(document_id, run_id, request_id)
        return True


@contextmanager
def advisory_lock(key: str) -> Iterator[bool]:
    """Try a session-level Postgres advisory lock on a private connection; yields whether we got it.

    Session-level (not transaction-level) because the body commits many short transactions. The connection is
    AUTOCOMMIT, so it is never left "idle in transaction" while the body works. The worker engine uses NullPool, so
    close() really ends the session, and ending the session releases the lock — also when the worker is killed,
    because the socket dies with the process."""
    conn = get_sync_engine().connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        acquired = conn.execute(text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"), {"key": key}).scalar()
        yield bool(acquired)
    finally:
        conn.close()


def _republish_later(task: Task, *, kind: Kind, document_id: str, run_id: str, request_id: str | None,
                     lock_waits: int) -> None:
    if lock_waits >= MAX_LOCK_WAITS:
        # 40 x 15 s (plus queueing) is at least the 10-minute hard time limit: the holder is not a healthy attempt of
        # this run, and a run with no task left must not sit in queued/processing until a human notices.
        log.warning("lock_wait_gave_up", kind=kind, document_id=document_id, lock_waits=lock_waits)
        _fail_waiting_run(kind, document_id, run_id, ErrorCode.PROCESSING_TIMEOUT,
                          f"Gave up after waiting {lock_waits} times for another worker to release this document.")
        return
    log.info("lock_busy_republishing", kind=kind, document_id=document_id, lock_waits=lock_waits + 1)
    try:
        task.apply_async(
            kwargs={"document_id": document_id, "run_id": run_id, "request_id": request_id,
                    "lock_waits": lock_waits + 1},
            countdown=LOCK_WAIT_COUNTDOWN_S,
        )
    except Exception as exc:  # noqa: BLE001 - kombu raises several connection error types
        log.error("lock_wait_republish_failed", kind=kind, document_id=document_id, error=str(exc))
        _fail_waiting_run(kind, document_id, run_id, ErrorCode.QUEUE_UNAVAILABLE, f"Re-publish failed: {exc}")


def _fail_waiting_run(kind: Kind, document_id: str, run_id: str, code: str, detail: str) -> None:
    """Fail (as retryable) a run whose message is about to disappear without ever getting the lock, so the user sees
    a failed document they can reprocess instead of one that says queued/processing with no task left to run it.

    Fenced like every worker write (a superseded run writes nothing). And never while an attempt of this same run is
    'running': that attempt holds the lock we could not get and finishes or fails the run itself (if its worker
    died, Celery redelivers its message), so failing it here would kill a healthy run because a duplicate gave up."""
    doc_id, run = uuid.UUID(str(document_id)), uuid.UUID(str(run_id))
    with sync_session() as s:
        running = s.scalar(select(func.count()).select_from(ProcessingRun).where(
            ProcessingRun.run_id == run, ProcessingRun.task == kind, ProcessingRun.status == "running"))
        if running:
            log.warning("waiting_duplicate_dropped", kind=kind, run_id=str(run), code=code)
            return
        log.warning("waiting_run_failed", kind=kind, run_id=str(run), code=code, detail=detail)
        s.execute(_fail_document_stmt(kind, doc_id, run, code, _USER_MESSAGES[code], retryable=True))


def _republish_lost_insights(document_id: uuid.UUID, run_id: uuid.UUID, request_id: str | None) -> None:
    """An ingest that could not claim its run may be the redelivery of one that committed 'ready' (insights pending)
    and then lost its worker before publishing the insights task (acks_late redelivers the ingest message, but
    nobody would ever publish insights). Publish them now.

    A duplicate insights task is usually harmless: its claim is fenced by run_id and only matches insights
    pending/processing, and the insights lock runs copies one at a time, so a second copy finds them ready (or
    failed) and stops. A duplicate consumed while the original waits for a retry can claim and run once more (one
    extra model call at most); the run fence stops any stale write."""
    with sync_session() as s:
        lost = s.scalar(select(Document.id).where(
            Document.id == document_id, Document.current_run_id == run_id,
            Document.status == "ready", Document.insights_status == "pending"))
    if lost is None:
        return
    log.warning("insights_handoff_republished", run_id=str(run_id))
    try:
        enqueue_insights(document_id, run_id, request_id)
    except QueueUnavailable as exc:
        log.error("insights_enqueue_failed", error=str(exc))
        with sync_session() as s:
            # Only while still pending: an insights task that started meanwhile must not be failed under its feet.
            s.execute(state.insights_fail_stmt(document_id, run_id, ErrorCode.QUEUE_UNAVAILABLE)
                      .where(Document.insights_status == "pending"))


# --- Claim + crash counter ------------------------------------------------------------------------------------------


def _claim_and_start(task: Task, kind: Kind, document_id: uuid.UUID, run_id: uuid.UUID,
                     request_id: str | None) -> Attempt | None:
    """One transaction: claim the run, close out crashed attempts, then either fail the run or open a new attempt."""
    claim = state.claim_ingest_stmt if kind == "ingest" else state.claim_insights_stmt
    with sync_session() as s:
        if s.execute(claim(document_id, run_id)).first() is None:
            log.info("attempt_not_current", kind=kind, run_id=str(run_id))
            return None
        owner_id = s.scalar(select(Document.owner_id).where(Document.id == document_id))
        crashed = _close_out_crashed_attempts(s, kind, document_id, run_id)
        if crashed >= MAX_CRASHED_ATTEMPTS:
            log.error("attempt_crash_limit", kind=kind, run_id=str(run_id), crashed=crashed)
            s.execute(_fail_document_stmt(kind, document_id, run_id, ErrorCode.PROCESSING_CRASHED,
                                          _USER_MESSAGES[ErrorCode.PROCESSING_CRASHED], retryable=False))
            return None
        number = 1 + (s.scalar(select(func.count()).select_from(ProcessingRun)
                               .where(ProcessingRun.run_id == run_id, ProcessingRun.task == kind)) or 0)
        row_id = s.scalar(insert(ProcessingRun).values(
            run_id=run_id, document_id=document_id, owner_id=owner_id, task=kind, attempt=number,
            celery_task_id=task.request.id, request_id=request_id, status="running",
        ).returning(ProcessingRun.id))
    log.info("attempt_started", kind=kind, run_id=str(run_id), attempt=number)
    return Attempt(kind=kind, document_id=document_id, run_id=run_id, owner_id=owner_id, task_id=task.request.id,
                   request_id=request_id, number=number, row_id=row_id)


def _close_out_crashed_attempts(s: Session, kind: Kind, document_id: uuid.UUID, run_id: uuid.UUID) -> int:
    """Mark leftover 'running' attempts as failed/WORKER_LOST; return how many attempts of THIS run were lost.

    Why 'running' means 'dead': every attempt holds the advisory lock for its whole life and finishes its row
    before releasing it. We hold the lock now, so any row still 'running' belongs to a worker that was killed
    (OOM, kill -9) and whose message Celery redelivered."""
    s.execute(
        update(ProcessingRun)
        .where(ProcessingRun.document_id == document_id, ProcessingRun.task == kind, ProcessingRun.status == "running")
        .values(status="failed", error_code=ErrorCode.WORKER_LOST, finished_at=func.now(),
                error_message="The worker process died during this attempt (detected by the next attempt).")
    )
    return s.scalar(
        select(func.count()).select_from(ProcessingRun)
        .where(ProcessingRun.run_id == run_id, ProcessingRun.task == kind,
               ProcessingRun.error_code == ErrorCode.WORKER_LOST)
    ) or 0


# --- Running the body and classifying failures ------------------------------------------------------------------------


def _execute(task: Task, attempt: Attempt, body: Body) -> None:
    try:
        body(attempt)
    except RunSuperseded:
        log.info("attempt_superseded", kind=attempt.kind, run_id=str(attempt.run_id))
        _finish(attempt, "failed", SUPERSEDED, "A reprocess or delete took over; stopped without writing.")
    except ProviderError as exc:
        if exc.kind == "transient":
            _retry_or_fail(task, attempt, exc, exhausted_code=ErrorCode.PROVIDER_UNAVAILABLE,
                           retry_after=exc.retry_after)
        else:
            code = _PROVIDER_CODES.get(exc.kind, ErrorCode.PROVIDER_BAD_REQUEST)
            _fail(attempt, code, _USER_MESSAGES[code], detail=exc.summary(), log_detail=exc.message, retryable=False)
    except PermanentError as exc:
        _fail(attempt, exc.code, exc.message, retryable=False)
    except SoftTimeLimitExceeded:
        _fail(attempt, ErrorCode.PROCESSING_TIMEOUT, _USER_MESSAGES[ErrorCode.PROCESSING_TIMEOUT], retryable=True)
    except OperationalError as exc:
        _retry_or_fail(task, attempt, exc, exhausted_code=PROCESSING_ERROR, retry_after=None)
    except StorageUnavailable as exc:
        _retry_or_fail(task, attempt, exc, exhausted_code=ErrorCode.STORAGE_ERROR, retry_after=None)
    except Exception as exc:
        log.exception("attempt_unexpected_error", kind=attempt.kind, run_id=str(attempt.run_id))
        _fail(attempt, PROCESSING_ERROR, _USER_MESSAGES[PROCESSING_ERROR], detail=repr(exc), retryable=True)
    else:
        _finish(attempt, "succeeded")
        log.info("attempt_succeeded", kind=attempt.kind, duration_ms=attempt.elapsed_ms(), stage_ms=attempt.stage_ms)


def _retry_or_fail(task: Task, attempt: Attempt, exc: Exception, *, exhausted_code: str,
                   retry_after: float | None) -> None:
    retries = task.request.retries
    if retries >= task.max_retries:
        _fail(attempt, exhausted_code, _USER_MESSAGES[exhausted_code], detail=str(exc), retryable=True)
        return
    countdown = backoff(retries, retry_after)
    try:
        _finish(attempt, "retrying", TRANSIENT, str(exc))
    except Exception:  # noqa: BLE001 - bookkeeping must never block the retry (e.g. the DB is what failed)
        log.warning("attempt_bookkeeping_failed", run_id=str(attempt.run_id))
    log.warning("attempt_retrying", kind=attempt.kind, retries=retries + 1, countdown_s=round(countdown, 1),
                error=str(exc))
    try:
        # The Retry exception tells the worker to ack this message; Celery has already published the delayed copy.
        raise task.retry(countdown=countdown)
    except Reject as rejected:
        # Celery could not publish the delayed copy (broker down) and would drop this message: fail the run instead.
        _fail(attempt, ErrorCode.QUEUE_UNAVAILABLE, _USER_MESSAGES[ErrorCode.QUEUE_UNAVAILABLE], retryable=True,
              detail=f"Retry could not be published: {rejected.reason}")


def _finish_stmt(attempt: Attempt, status: str, code: str | None, detail: str | None) -> Update:
    return (
        update(ProcessingRun)
        .where(ProcessingRun.id == attempt.row_id)
        .values(status=status, error_code=code, error_message=detail[:2000] if detail else None,
                stage_ms=attempt.stage_ms, finished_at=func.now(), duration_ms=attempt.elapsed_ms())
    )


def _finish(attempt: Attempt, status: str, code: str | None = None, detail: str | None = None) -> None:
    with sync_session() as s:
        s.execute(_finish_stmt(attempt, status, code, detail))


def _fail(attempt: Attempt, code: str, message: str, *, retryable: bool, detail: str | None = None,
          log_detail: str | None = None) -> None:
    """Close the attempt and fail the run in ONE transaction (fenced: a superseded run's failure writes nothing).
    `detail` is stored in processing_runs, which /metrics/processing serves to the owner's API keys; `log_detail`
    (vendor text) is logged instead of it and never stored."""
    log.warning("attempt_failed", kind=attempt.kind, code=code, retryable=retryable,
                detail=log_detail or detail or message)
    with sync_session() as s:
        s.execute(_finish_stmt(attempt, "failed", code, detail or message))
        s.execute(_fail_document_stmt(attempt.kind, attempt.document_id, attempt.run_id, code, message, retryable))


def _fail_document_stmt(kind: Kind, document_id: uuid.UUID, run_id: uuid.UUID, code: str, message: str,
                        retryable: bool) -> Update:
    # Ingest failures fail the document. Insights failures only fail insights: the document stays ready, chat works.
    if kind == "ingest":
        return state.fail_stmt(document_id, run_id, code, message, retryable)
    return state.insights_fail_stmt(document_id, run_id, code)
