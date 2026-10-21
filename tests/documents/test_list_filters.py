"""GET /documents filters: status, category, tag and q each narrow the list, they combine with AND, they never reach
another owner's documents, and q is matched literally (a "%" or "_" typed in the dashboard's search box is text, not
a LIKE wildcard that lists everything). Rows are inserted directly, so no pipeline runs."""

import hashlib
import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document
from tests.conftest import DEMO


async def add_document(db: AsyncSession, owner_id: uuid.UUID, filename: str, **cols: Any) -> str:
    doc_id = uuid.uuid4()
    db.add(Document(id=doc_id, owner_id=owner_id, filename=filename, mime_type="text/plain", size_bytes=10,
                    sha256=hashlib.sha256(doc_id.bytes).hexdigest(), storage_key=f"uploads/{owner_id}/{doc_id}.txt",
                    current_run_id=uuid.uuid4(), **cols))
    await db.commit()
    return str(doc_id)


async def listed(client: AsyncClient, **params: str) -> set[str]:
    resp = await client.get("/api/v1/documents", headers=DEMO, params=params)
    assert resp.status_code == 200, resp.text
    return {item["id"] for item in resp.json()["items"]}


async def test_each_filter_narrows_and_filters_combine(client: AsyncClient, db: AsyncSession,
                                                       demo_user_id: uuid.UUID) -> None:
    handbook = await add_document(db, demo_user_id, "Handbook_2026.pdf", status="ready", category="hr_policy",
                                  tags=["benefits", "pto"], title="Employee Handbook")
    msa = await add_document(db, demo_user_id, "Brightline_MSA.docx", status="ready", category="legal_contract",
                             tags=["msa"], title="Master Services Agreement")
    notes = await add_document(db, demo_user_id, "notes.txt", status="failed", category="hr_policy", tags=["pto"])

    assert await listed(client) == {handbook, msa, notes}
    assert await listed(client, status="ready") == {handbook, msa}
    assert await listed(client, category="hr_policy") == {handbook, notes}
    assert await listed(client, tag="PTO") == {handbook, notes}              # tags are stored lower-case
    assert await listed(client, q="brightline") == {msa}                      # filename, any case
    assert await listed(client, q="services agreement") == {msa}              # or title
    assert await listed(client, status="ready", category="hr_policy", tag="pto") == {handbook}
    assert await listed(client, status="failed", q="handbook") == set()


async def test_filters_never_reach_another_owners_documents(client: AsyncClient, db: AsyncSession,
                                                            demo_user_id: uuid.UUID, other_user_id: uuid.UUID) -> None:
    mine = await add_document(db, demo_user_id, "Handbook_2026.pdf", status="ready", category="hr_policy",
                              tags=["pto"])
    await add_document(db, other_user_id, "Handbook_2026_copy.pdf", status="ready", category="hr_policy",
                       tags=["pto"])

    assert await listed(client, status="ready", category="hr_policy", tag="pto", q="handbook") == {mine}


async def test_q_is_text_not_a_like_pattern(client: AsyncClient, db: AsyncSession, demo_user_id: uuid.UUID) -> None:
    discount = await add_document(db, demo_user_id, "discount-100%.txt")
    snake = await add_document(db, demo_user_id, "q3_report.txt")
    await add_document(db, demo_user_id, "Handbook.pdf")

    assert await listed(client, q="%") == {discount}
    assert await listed(client, q="_") == {snake}
    assert await listed(client, q="q3_r") == {snake}
    assert await listed(client, q="\\") == set()


async def test_a_filtered_list_pages_through_every_match_once(client: AsyncClient, db: AsyncSession,
                                                              demo_user_id: uuid.UUID) -> None:
    matching = {await add_document(db, demo_user_id, f"policy_{i}.txt", status="ready") for i in range(5)}
    await add_document(db, demo_user_id, "draft.txt", status="failed")

    seen: list[str] = []
    cursor = None
    while True:
        params = {"status": "ready", "limit": "2", **({"cursor": cursor} if cursor else {})}
        resp = await client.get("/api/v1/documents", headers=DEMO, params=params)
        assert resp.status_code == 200, resp.text
        seen += [item["id"] for item in resp.json()["items"]]
        cursor = resp.json()["next_cursor"]
        if not cursor:
            break
    assert sorted(seen) == sorted(matching)
