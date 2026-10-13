"""GET /search: response shape, modes, document filtering, tenant isolation and guards."""

import asyncio
import time
import uuid

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fake_provider import FakeProvider
from tests.chat.helpers import API, upload_ready
from tests.conftest import DEMO, OTHER

OPS = "Incident LRK-4471 was a payroll outage on 3 March. The fix shipped the same day."
HANDBOOK = "Larkspur employees receive 18 PTO days per year. Every incident needs a postmortem."


async def test_search_shape_and_identifier_hit(client: AsyncClient, db: AsyncSession, fake: FakeProvider) -> None:
    ops = await upload_ready(client, "ops.txt", OPS)
    await upload_ready(client, "handbook.txt", HANDBOOK)
    fake.calls.clear()                      # ingestion (insights) calls the model; the search itself must not
    r = await client.get(f"{API}/search", params={"q": "What happened in incident LRK-4471?"}, headers=DEMO)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "hybrid" and body["id_terms"] == ["lrk-4471"]
    assert body["terms"] == ["happened", "incident", "lrk-4471"] and body["top_similarity"] is not None
    top = body["hits"][0]
    assert set(top) == {"chunk_id", "document_id", "filename", "label", "page_start", "page_end", "rrf", "sem_rank",
                        "lex_rank", "similarity", "id_hit", "snippet", "content"}
    assert top["document_id"] == ops and top["id_hit"] is True and top["lex_rank"] == 1
    assert "LRK-4471" in top["content"] and "LRK-4471" in top["snippet"]
    assert not [c for c in fake.calls if c["op"] in ("stream", "json")]     # retrieval only, no LLM call
    usage = (await db.execute(text("SELECT operation FROM ai_usage WHERE operation = 'embed_query'"))).all()
    assert len(usage) == 1


async def test_search_modes_and_k(client: AsyncClient) -> None:
    await upload_ready(client, "ops.txt", OPS)
    await upload_ready(client, "handbook.txt", HANDBOOK)
    keyword = (await client.get(f"{API}/search", params={"q": "incident", "mode": "keyword"}, headers=DEMO)).json()
    assert keyword["mode"] == "keyword" and keyword["top_similarity"] is None
    assert all(h["sem_rank"] is None and h["similarity"] is None for h in keyword["hits"])
    vector = (await client.get(f"{API}/search", params={"q": "incident", "mode": "vector", "k": 1},
                               headers=DEMO)).json()
    assert vector["mode"] == "vector" and len(vector["hits"]) == 1 and vector["hits"][0]["lex_rank"] is None


async def test_search_document_filter_and_tenant_isolation(client: AsyncClient) -> None:
    mine = await upload_ready(client, "ops.txt", OPS)
    theirs = await upload_ready(client, "theirs.txt", "Incident LRK-4471 secret notes for the other tenant.", OTHER)
    q = {"q": "incident LRK-4471"}
    only_theirs = (await client.get(f"{API}/search", params=q | {"document_ids": [theirs]}, headers=DEMO)).json()
    assert only_theirs["hits"] == []                                        # foreign ids are intersected away
    both = (await client.get(f"{API}/search", params=q | {"document_ids": [mine, theirs]}, headers=DEMO)).json()
    assert {h["document_id"] for h in both["hits"]} == {mine}
    default = (await client.get(f"{API}/search", params=q, headers=OTHER)).json()
    assert {h["document_id"] for h in default["hits"]} == {theirs}


async def test_search_validation(client: AsyncClient) -> None:
    assert (await client.get(f"{API}/search", params={"q": ""}, headers=DEMO)).status_code == 422
    assert (await client.get(f"{API}/search", params={"q": "x", "mode": "bm25"}, headers=DEMO)).status_code == 422
    assert (await client.get(f"{API}/search", params={"q": "x"})).status_code == 401
    empty = (await client.get(f"{API}/search", params={"q": "anything"}, headers=DEMO)).json()
    assert empty["hits"] == []


async def test_search_is_rate_limited_and_quota_guarded(client: AsyncClient, db: AsyncSession,
                                                        demo_user_id: uuid.UUID) -> None:
    """/search pays for a query embedding, so it sits behind a per-minute limit and the daily cost quota."""
    original = await db.scalar(text("SELECT rate_limit_per_min FROM users WHERE name = 'demo'"))
    await db.execute(text("UPDATE users SET rate_limit_per_min = 1 WHERE name = 'demo'"))
    await db.commit()
    try:
        if time.time() % 60 > 58:           # don't let the two calls straddle a window boundary
            await asyncio.sleep(60 - time.time() % 60 + 0.05)
        assert (await client.get(f"{API}/search", params={"q": "incident"}, headers=DEMO)).status_code == 200
        r = await client.get(f"{API}/search", params={"q": "incident"}, headers=DEMO)
        assert r.status_code == 429 and r.json()["error"]["code"] == "RATE_LIMITED"
    finally:                                # users are shared across the test session: always restore
        await db.execute(text("UPDATE users SET rate_limit_per_min = :n WHERE name = 'demo'"), {"n": original})
        await db.commit()

    await db.execute(text("INSERT INTO ai_usage (owner_id, operation, model, cost_usd) "
                          "VALUES (:o, 'answer', 'gpt-6-luna', 1000)"), {"o": demo_user_id})
    await db.commit()
    r = await client.get(f"{API}/search", params={"q": "incident"}, headers=DEMO)
    assert r.status_code == 429 and r.json()["error"]["code"] == "QUOTA_EXCEEDED"
    assert (await client.get(f"{API}/search", params={"q": "incident"}, headers=OTHER)).status_code == 200
