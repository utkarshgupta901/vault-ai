"""Celery tasks. Names and kwargs are the frozen contract the API publishes to (app/worker/dispatch.py):

    vault.ingest_document(document_id, run_id, request_id=None, lock_waits=0)    queue `ingest`
    vault.generate_insights(document_id, run_id, request_id=None, lock_waits=0)  queue `ai`

Both tasks are thin: worker/attempt.py owns locking, claiming, crash counting, retries and failure codes, and the
bodies (ingestion/pipeline.py, ai/insights.py) own the actual work. No autoretry_for: retries are decided in one place.
"""

import structlog

from app.config import settings
from app.core.db import sync_session
from app.ingestion.pipeline import run_ingest
from app.services import state
from app.worker.attempt import Attempt, RunSuperseded, run_attempt
from app.worker.celery_app import celery_app

log = structlog.get_logger(__name__)


@celery_app.task(name="vault.ingest_document", bind=True, max_retries=settings.MAX_TASK_RETRIES)
def ingest_document(self, document_id: str, run_id: str, request_id: str | None = None, lock_waits: int = 0) -> None:
    run_attempt(self, kind="ingest", document_id=document_id, run_id=run_id, request_id=request_id,
                lock_waits=lock_waits, body=run_ingest)


@celery_app.task(name="vault.generate_insights", bind=True, max_retries=settings.MAX_TASK_RETRIES)
def generate_insights(self, document_id: str, run_id: str, request_id: str | None = None,
                      lock_waits: int = 0) -> None:
    run_attempt(self, kind="insights", document_id=document_id, run_id=run_id, request_id=request_id,
                lock_waits=lock_waits, body=insights_body)


def insights_body(attempt: Attempt) -> None:
    """Run the insights generator (app/ai/insights.py) and record its stage timings on the attempt.

    run_insights loads the pages, calls the model, verifies the result and writes it in one fenced transaction; it
    raises ProviderError/PermanentError and leaves claiming, retries and processing_runs to run_attempt."""
    try:
        from app.ai.insights import run_insights
    except ModuleNotFoundError as exc:
        if exc.name != "app.ai.insights":  # a real import bug inside the module must not be hidden
            raise
        _insights_ready_without_content(attempt)
        return
    timings = run_insights(attempt.document_id, attempt.run_id, task_id=attempt.task_id,
                           request_id=attempt.request_id)
    for stage, ms in (timings or {}).items():
        attempt.record_stage(stage, ms)


def _insights_ready_without_content(attempt: Attempt) -> None:
    """Stand-in until the insights module exists, so ingestion works on its own: finish the run with no content."""
    log.warning("insights_module_missing")
    with sync_session() as s:
        if s.execute(state.insights_ready_stmt(attempt.document_id, attempt.run_id)).first() is None:
            raise RunSuperseded()
