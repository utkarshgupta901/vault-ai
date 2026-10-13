"""Publishing tasks. The API never imports task code; it publishes by task *name* (send_task).

Ordering rule: the API commits the document row FIRST, then publishes. If publishing fails, the row is marked
failed/QUEUE_UNAVAILABLE (retryable) — never a task pointing at a row that doesn't exist. (The fully robust version
is a transactional outbox; documented as a deliberate trade-off.)

Publishing is a blocking network call, so async code must use the `a*` variants (run in a worker thread)."""

import uuid
from functools import partial

import anyio

from app.worker.celery_app import celery_app

_RETRY_POLICY = {"max_retries": 1, "interval_start": 0.2, "interval_step": 0.5, "interval_max": 1.0}


class QueueUnavailable(Exception):
    pass


def _send(name: str, queue: str, kwargs: dict, countdown: float | None = None) -> None:
    if celery_app.conf.task_always_eager:  # tests: run inline (send_task itself ignores eager mode)
        import app.worker.tasks  # noqa: F401  (registers tasks)

        celery_app.tasks[name].apply(kwargs=kwargs)
        return
    try:
        celery_app.send_task(name, kwargs=kwargs, queue=queue, countdown=countdown, retry=True,
                             retry_policy=_RETRY_POLICY)
    except Exception as exc:  # noqa: BLE001 - kombu raises several connection error types
        raise QueueUnavailable(str(exc)) from exc


def enqueue_ingest(document_id: uuid.UUID, run_id: uuid.UUID, request_id: str | None = None) -> None:
    _send("vault.ingest_document", "ingest",
          {"document_id": str(document_id), "run_id": str(run_id), "request_id": request_id})


def enqueue_insights(document_id: uuid.UUID, run_id: uuid.UUID, request_id: str | None = None) -> None:
    _send("vault.generate_insights", "ai",
          {"document_id": str(document_id), "run_id": str(run_id), "request_id": request_id})


async def aenqueue_ingest(document_id: uuid.UUID, run_id: uuid.UUID, request_id: str | None = None) -> None:
    await anyio.to_thread.run_sync(partial(enqueue_ingest, document_id, run_id, request_id))


async def aenqueue_insights(document_id: uuid.UUID, run_id: uuid.UUID, request_id: str | None = None) -> None:
    await anyio.to_thread.run_sync(partial(enqueue_insights, document_id, run_id, request_id))
