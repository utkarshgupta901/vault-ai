"""Document management API: upload & ingestion, retrieval, live status (SSE), pages, original file, reprocess, delete."""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Literal

import anyio
from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.db import async_session, get_db
from app.models import Document
from app.schemas.common import ERROR_RESPONSES, Page
from app.schemas.documents import (
    DocumentOut,
    DocumentStatusEvent,
    PageOut,
    ReprocessRequest,
    ReprocessResponse,
    UploadLinks,
    UploadResponse,
)
from app.services import documents as svc
from app.services import storage

router = APIRouter(prefix="/documents", tags=["documents"], responses=ERROR_RESPONSES)

User = Annotated[CurrentUser, Depends(get_current_user)]
Db = Annotated[AsyncSession, Depends(get_db)]

EVENTS_POLL_S = 0.5
EVENTS_MAX_S = 600


def _links(document_id: uuid.UUID) -> UploadLinks:
    base = f"/api/v1/documents/{document_id}"
    return UploadLinks(self=base, events=f"{base}/events", insights=f"{base}/insights")


@router.post(
    "",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a document (PDF, DOCX, TXT, MD)",
    responses={200: {"model": UploadResponse, "description": "Duplicate of an existing document (no new work)"},
               411: {}, 413: {}, 415: {}, 503: {}},
)
async def upload_document(user: User, db: Db, response: Response, file: UploadFile = File(...)) -> UploadResponse:
    """Validates and stores the file, then queues it for async processing and returns **202** immediately.
    Follow progress with `GET /documents/{id}/events` (SSE) or poll `GET /documents/{id}`.
    Re-uploading identical bytes returns **200** with `duplicate: true` and costs nothing."""
    result = await svc.upload(db, user.id, file)
    if result.duplicate and not result.requeued:
        response.status_code = status.HTTP_200_OK
    response.headers["Location"] = f"/api/v1/documents/{result.document.id}"
    return UploadResponse(document=svc.to_out(result.document), duplicate=result.duplicate,
                          requeued=result.requeued, links=_links(result.document.id))


@router.get("", response_model=Page[DocumentOut], summary="List documents (newest first, cursor-paginated)")
async def list_documents(
    user: User,
    db: Db,
    status_: Annotated[Literal["queued", "processing", "ready", "failed"] | None, Query(alias="status")] = None,
    category: str | None = None,
    tag: str | None = None,
    q: Annotated[str | None, Query(max_length=200, description="Search filename/title")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> Page[DocumentOut]:
    rows, next_cursor = await svc.list_documents(db, user.id, status=status_, category=category, tag=tag, q=q,
                                                 limit=limit, cursor=cursor)
    return Page(items=[svc.to_out(d) for d in rows], next_cursor=next_cursor)


@router.get("/{document_id}", response_model=DocumentOut, summary="Get document metadata and processing status")
async def get_document(document_id: uuid.UUID, user: User, db: Db) -> DocumentOut:
    return svc.to_out(await svc.get_owned(db, user.id, document_id))


async def _owned_document_id(document_id: uuid.UUID, user: User) -> uuid.UUID:
    """Pre-stream check (runs before the 200 + headers are sent, so a bad id still gets a proper 404)."""
    async with async_session() as db:
        await svc.get_owned(db, user.id, document_id)
    return document_id


def _is_terminal(ev: DocumentStatusEvent) -> bool:
    return ev.status == "failed" or (ev.status == "ready" and ev.insights_status in ("ready", "failed"))


@router.get("/{document_id}/events", response_class=EventSourceResponse,
            summary="Live processing status (Server-Sent Events)")
async def document_events(
    document_id: Annotated[uuid.UUID, Depends(_owned_document_id)], user: User
) -> AsyncIterator[ServerSentEvent]:
    """Emits `event: status` whenever status/stage/progress changes; ends when the document is fully processed
    (ready + insights done), failed, or after 10 minutes. Read it with fetch() so the X-API-Key header can be sent."""
    last: DocumentStatusEvent | None = None
    started = time.monotonic()
    while time.monotonic() - started < EVENTS_MAX_S:
        async with async_session() as db:  # short session per poll; never held across the sleep
            doc = await db.scalar(select(Document).where(Document.id == document_id, Document.owner_id == user.id))
        if doc is None:
            yield ServerSentEvent(event="deleted", data={"document_id": str(document_id)})
            return
        out = svc.to_out(doc)
        ev = DocumentStatusEvent(status=out.status, stage=out.stage, progress_pct=out.progress_pct,
                                 insights_status=out.insights_status, error=out.error, stale=out.stale)
        if ev != last:
            yield ServerSentEvent(event="status", data=ev)
            last = ev
        if _is_terminal(ev):
            return
        await asyncio.sleep(EVENTS_POLL_S)


@router.get("/{document_id}/pages/{page_no}", response_model=PageOut, summary="Extracted text of one page (citation click-through)")
async def get_page(document_id: uuid.UUID, page_no: int, user: User, db: Db) -> PageOut:
    page = await svc.get_page(db, user.id, document_id, page_no)
    return PageOut(document_id=document_id, page_no=page.page_no, label=page.label,
                   heading_path=page.heading_path, text=page.text)


@router.get("/{document_id}/file", summary="Download the original file", response_class=StreamingResponse)
async def get_file(document_id: uuid.UUID, user: User, db: Db) -> StreamingResponse:
    doc = await svc.get_owned(db, user.id, document_id)
    body = await anyio.to_thread.run_sync(storage.open_stream, doc.storage_key)
    disposition = "inline" if doc.mime_type == "application/pdf" else "attachment"
    safe_name = doc.filename.replace('"', "")
    return StreamingResponse(
        body.iter_chunks(1024 * 1024),  # sync iterator -> Starlette runs it in a threadpool
        media_type=doc.mime_type,
        headers={"Content-Disposition": f'{disposition}; filename="{safe_name}"',
                 "X-Content-Type-Options": "nosniff", "Content-Length": str(doc.size_bytes)},
    )


@router.post("/{document_id}/reprocess", response_model=ReprocessResponse, status_code=status.HTTP_202_ACCEPTED,
             summary="Re-run processing (all stages, or insights only)")
async def reprocess_document(document_id: uuid.UUID, body: ReprocessRequest, user: User, db: Db) -> ReprocessResponse:
    doc, run_id = await svc.reprocess(db, user.id, document_id, body.scope)
    return ReprocessResponse(document=svc.to_out(doc), run_id=run_id)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a document and its data")
async def delete_document(document_id: uuid.UUID, user: User, db: Db) -> Response:
    await svc.delete_document(db, user.id, document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
