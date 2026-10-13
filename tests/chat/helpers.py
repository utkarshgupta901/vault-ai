"""Shared helpers for chat/retrieval tests: SSE parsing, direct corpus inserts, session setup."""

import hashlib
import json
import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fake_provider import fake_vector
from app.config import settings
from app.models import Chunk, Document, DocumentPage
from tests.conftest import DEMO, upload

API = "/api/v1"


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """[(event, data)] from a text/event-stream body (keep-alive comments and blank lines ignored)."""
    events = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        name, data = None, []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
        if name:
            events.append((name, json.loads("\n".join(data)) if data else {}))
    return events


async def insert_document(
    db: AsyncSession,
    owner_id: uuid.UUID,
    filename: str,
    chunks: list[str | tuple[int, str]],
    *,
    status: str = "ready",
    labels: dict[int, str] | None = None,
    embed: bool = True,
) -> uuid.UUID:
    """A ready document with exactly these chunks (page 1 unless given as (page, text)), embedded with the same
    fake vectors FakeProvider gives queries. Bypasses ingestion so retrieval tests control chunk boundaries."""
    doc_id = uuid.uuid4()
    title = filename.rsplit(".", 1)[0]
    db.add(Document(id=doc_id, owner_id=owner_id, filename=filename, title=title, mime_type="text/plain",
                    size_bytes=100, sha256=hashlib.sha256(doc_id.bytes).hexdigest(),
                    storage_key=f"test/{doc_id}.txt", status=status))
    await db.flush()
    pages: dict[int, list[str]] = {}
    for i, item in enumerate(chunks):
        page, content = item if isinstance(item, tuple) else (1, item)
        pages.setdefault(page, []).append(content)
        label = (labels or {}).get(page, f"p. {page}")
        header = f"{title} ({label})"
        db.add(Chunk(document_id=doc_id, owner_id=owner_id, chunk_index=i, page_start=page, page_end=page,
                     header=header, content=content, token_count=len(content.split()),
                     embed_hash=hashlib.sha256(f"{doc_id}{i}".encode()).hexdigest(),
                     embedding_model=settings.EMBED_MODEL,
                     embedding=fake_vector(f"{header}\n{content}") if embed else None))
    for page, texts in pages.items():
        body = "\n".join(texts)
        db.add(DocumentPage(document_id=doc_id, page_no=page, label=(labels or {}).get(page, f"p. {page}"),
                            text=body, char_count=len(body)))
    await db.commit()
    return doc_id


async def set_suggested_questions(db: AsyncSession, document_id: uuid.UUID, questions: list[str]) -> None:
    await db.execute(text("""
        INSERT INTO document_insights (document_id, suggested_questions) VALUES (:id, CAST(:q AS jsonb))
        ON CONFLICT (document_id) DO UPDATE SET suggested_questions = EXCLUDED.suggested_questions"""),
        {"id": document_id, "q": json.dumps(questions)})
    await db.commit()


async def upload_ready(client: AsyncClient, name: str, content: str, headers: dict | None = None) -> str:
    r = await upload(client, name, content.encode(), headers or DEMO)
    assert r.status_code in (200, 202), r.text
    doc = (await client.get(f"{API}/documents/{r.json()['document']['id']}", headers=headers or DEMO)).json()
    assert doc["status"] == "ready", doc
    return doc["id"]


async def new_session(client: AsyncClient, document_ids: list[str] | None = None, headers: dict | None = None,
                      **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"document_ids": document_ids} if document_ids else {"all_documents": True}
    r = await client.post(f"{API}/chat/sessions", json=body | extra, headers=headers or DEMO)
    assert r.status_code == 201, r.text
    return r.json()


async def ask_stream(client: AsyncClient, session_id: str, content: str, headers: dict | None = None,
                     **extra: Any):
    return await client.post(f"{API}/chat/sessions/{session_id}/messages/stream",
                             json={"content": content} | extra, headers=headers or DEMO)


async def ask_json(client: AsyncClient, session_id: str, content: str, headers: dict | None = None, **extra: Any):
    return await client.post(f"{API}/chat/sessions/{session_id}/messages",
                             json={"content": content} | extra, headers=headers or DEMO)
