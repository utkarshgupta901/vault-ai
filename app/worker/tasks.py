"""Celery tasks — WALKING-SKELETON version (WP0). WP2 replaces this with the real pipeline
(app/ingestion/pipeline.py + app/worker/attempt.py: PDF/DOCX extraction, page-aware chunking, batched embeddings,
retries/backoff, crash counter, processing_runs). The task names and kwargs here are the frozen contract."""

import hashlib
import uuid

import structlog

from app.ai import get_provider
from app.config import settings
from app.core.db import sync_session
from app.models import Chunk, Document, DocumentPage
from app.services import state, storage
from app.worker.celery_app import celery_app
from app.worker.dispatch import enqueue_insights

log = structlog.get_logger(__name__)


@celery_app.task(name="vault.ingest_document", bind=True, max_retries=settings.MAX_TASK_RETRIES)
def ingest_document(self, document_id: str, run_id: str, request_id: str | None = None, lock_waits: int = 0) -> None:
    doc_id, run = uuid.UUID(document_id), uuid.UUID(run_id)
    with sync_session() as s:
        if s.execute(state.claim_ingest_stmt(doc_id, run)).first() is None:
            log.info("ingest_superseded")
            return
        doc = s.get(Document, doc_id)
        key, owner_id, title = doc.storage_key, doc.owner_id, doc.title or doc.filename
    path = storage.download_to_temp(key)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    finally:
        path.unlink(missing_ok=True)
    words = text.split()
    pieces = [" ".join(words[i:i + 300]) for i in range(0, max(len(words), 1), 300)] or [""]
    emb = get_provider(for_worker=True).embed([f"{title}\n{p}" for p in pieces])
    with sync_session() as s:
        s.add(DocumentPage(document_id=doc_id, page_no=1, label="p. 1", heading_path=None, text=text,
                           char_count=len(text)))
        for i, (p, v) in enumerate(zip(pieces, emb.vectors, strict=True)):
            s.add(Chunk(document_id=doc_id, owner_id=owner_id, chunk_index=i, page_start=1, page_end=1,
                        header=f"{title} (p. 1)", content=p, token_count=len(p.split()),
                        embed_hash=hashlib.sha256(p.encode()).hexdigest(), embedding_model=settings.EMBED_MODEL,
                        embedding=v))
        s.execute(state.ready_stmt(doc_id, run, page_count=1, chunk_count=len(pieces), token_count=len(words)))
    enqueue_insights(doc_id, run, request_id)


@celery_app.task(name="vault.generate_insights", bind=True, max_retries=settings.MAX_TASK_RETRIES)
def generate_insights(self, document_id: str, run_id: str, request_id: str | None = None, lock_waits: int = 0) -> None:
    doc_id, run = uuid.UUID(document_id), uuid.UUID(run_id)
    with sync_session() as s:
        if s.execute(state.claim_insights_stmt(doc_id, run)).first() is None:
            return
        s.execute(state.insights_ready_stmt(doc_id, run))
