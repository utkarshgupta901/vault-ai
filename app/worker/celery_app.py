"""Celery application.

Mental model: the API *publishes* a small JSON message ({document_id, run_id}) to a Redis list (the queue).
Worker processes BRPOP messages, run the task, and only then *acknowledge* it (acks_late). If a worker dies
mid-task the message is not acked, so the broker redelivers it — at-least-once delivery. That is why every task is
idempotent and fenced by run_id (see services/state.py and worker/attempt.py).

Queues: `ingest` (extract/chunk/embed — CPU + embeddings API) and `ai` (LLM insights). Separate queues mean a slow
LLM call can never block ingestion, and each pool scales independently.

Broker connections in the API process: every publish borrows one connection from Celery's pool for the length of a
single LPUSH, and /health/ready's worker inspect uses its own connection (see api/health.py). No code path holds one
pool connection while waiting for another, so a bounded pool cannot deadlock, and we keep Celery's default bound of 10
(caps Redis connections per process). `broker_pool_limit=None` would also avoid the 2026-10-13 deadlock, but it opens
and closes a connection (TCP + TLS in the cloud) for every publish and has no upper bound under bursts.
The socket timeouts make a broker that is down OR accepts connections but never answers fail a call within seconds;
without `socket_timeout` a publish could block its thread forever. dispatch.py adds an overall bound on top.
"""

import structlog
from celery import Celery
from celery.signals import setup_logging, task_postrun, task_prerun, worker_process_init

from app.config import settings

celery_app = Celery("vault", broker=settings.BROKER_URL, include=["app.worker.tasks"])

celery_app.conf.update(
    task_acks_late=True,               # ack after the task finishes, not when it is received
    task_reject_on_worker_lost=True,   # child killed (OOM, kill -9) -> requeue now; crash counter stops poison loops
    worker_prefetch_multiplier=1,      # don't hoard messages: one task in hand per process
    task_ignore_result=True,           # state lives in Postgres, not in a result backend
    task_serializer="json",
    accept_content=["json"],
    broker_connection_retry_on_startup=True,
    broker_pool_limit=10,              # Celery's default, stated on purpose (see the module docstring)
    broker_transport_options={
        "visibility_timeout": 900,     # unacked msg redelivered after 15 min; > hard time limit, so no double-runs
        "socket_connect_timeout": 2,
        "socket_timeout": 5,           # > kombu's 1 s BRPOP timeout, so idle workers are unaffected
    },
    task_soft_time_limit=540,          # raises SoftTimeLimitExceeded inside the task -> clean failure
    task_time_limit=600,               # hard kill
    task_routes={
        "vault.ingest_document": {"queue": "ingest"},
        "vault.generate_insights": {"queue": "ai"},
    },
    task_default_queue="ingest",
    worker_hijack_root_logger=False,
    worker_send_task_events=True,
    task_send_sent_event=True,
)


@setup_logging.connect
def _setup_logging(**_: object) -> None:
    from app.core.logging import configure_logging

    configure_logging()


@worker_process_init.connect
def _init_child(**_: object) -> None:
    # Each forked child gets fresh DB engines and a fresh provider (no sockets shared across fork()).
    from app.ai import set_provider
    from app.core.db import reset_engines_after_fork

    reset_engines_after_fork()
    set_provider(None)


@task_prerun.connect
def _bind_task_context(task_id: str | None = None, task: object = None, kwargs: dict | None = None, **_: object) -> None:
    kwargs = kwargs or {}
    structlog.contextvars.bind_contextvars(
        task_id=task_id,
        task=getattr(task, "name", None),
        document_id=kwargs.get("document_id"),
        request_id=kwargs.get("request_id"),
    )


@task_postrun.connect
def _unbind_task_context(**_: object) -> None:
    structlog.contextvars.clear_contextvars()
