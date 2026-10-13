"""POST /documents/compare (P1): validation, ownership, readiness, size limit, and page re-checking."""

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.ai import compare
from app.ai.fake_provider import FakeProvider
from app.core.db import async_session
from app.models import AIUsage
from tests.conftest import DEMO, OTHER
from tests.insights.helpers import MSA_TEXT, sql, upload_ready

URL = "/api/v1/documents/compare"
OTHER_MSA = ("Northwind Services Agreement. Effective 1 June 2025.\n"
             "8.1 Either party may terminate on thirty (30) days’ written notice.\n"
             "Liability is capped at the fees paid in the previous six (6) months.\n")


def _comparison() -> dict[str, Any]:
    return {
        "dimensions": [
            {"name": "Termination notice", "values": [
                {"document": "D1", "value": "60 days' written notice for convenience", "page": 4},
                {"document": "d2", "value": "30 days' written notice", "page": 1},
                {"document": "D9", "value": "hallucinated document", "page": 1},
            ]},
            {"name": "Unknown docs only", "values": [{"document": "D7", "value": "x", "page": None}]},
        ],
        "similarities": ["Both allow termination with written notice.", " "],
        "differences": ["D1 requires 60 days; D2 requires 30 days."],
        "summary": "Two services agreements with different notice periods.",
    }


async def _two_docs(client: AsyncClient) -> list[str]:
    return [str(await upload_ready(client, "brightline.txt", MSA_TEXT)),
            str(await upload_ready(client, "northwind.txt", OTHER_MSA))]


async def test_compare_two_documents(client: AsyncClient, fake: FakeProvider) -> None:
    ids = await _two_docs(client)
    fake.json_responses["Comparison"] = _comparison()

    r = await client.post(URL, json={"document_ids": ids, "focus": "termination"}, headers=DEMO)

    assert r.status_code == 200, r.text
    body = r.json()
    assert [(d["ref"], d["document_id"]) for d in body["documents"]] == [("D1", ids[0]), ("D2", ids[1])]
    [dim] = body["dimensions"]                                  # the dimension with only unknown refs is dropped
    assert dim["name"] == "Termination notice"
    assert [(v["ref"], v["document_id"], v["page"]) for v in dim["values"]] == [("D1", ids[0], 1), ("D2", ids[1], 1)]
    assert body["similarities"] == ["Both allow termination with written notice."]
    assert body["prompt_version"] == "compare.v1"
    async with async_session() as s:
        [usage] = list(await s.scalars(select(AIUsage).where(AIUsage.operation == "compare")))
    assert usage.prompt_version == "compare.v1" and usage.success is True


async def test_compare_prompt_contains_both_documents_with_ids(client: AsyncClient, fake: FakeProvider) -> None:
    ids = await _two_docs(client)
    seen: list[str] = []
    original = fake.complete_json

    def spy(**kwargs: Any) -> Any:
        seen.append(kwargs["input"])
        return original(**kwargs)

    fake.complete_json = spy  # type: ignore[method-assign]
    await client.post(URL, json={"document_ids": ids, "focus": "<fees>"}, headers=DEMO)
    assert '<document id="D1" name="brightline.txt"' in seen[0] and '<document id="D2" name="northwind.txt"' in seen[0]
    assert "<focus>&lt;fees&gt;</focus>" in seen[0]


@pytest.mark.parametrize("payload", [
    {"document_ids": []},
    {"document_ids": ["00000000-0000-0000-0000-000000000001"]},
    {"document_ids": ["00000000-0000-0000-0000-000000000001"] * 2},
    {"document_ids": [f"00000000-0000-0000-0000-00000000000{i}" for i in range(1, 6)]},
])
async def test_compare_validates_document_count(client: AsyncClient, payload: dict) -> None:
    r = await client.post(URL, json=payload, headers=DEMO)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_compare_with_another_owners_document_is_404(client: AsyncClient) -> None:
    mine = str(await upload_ready(client))
    theirs = str(await upload_ready(client, "northwind.txt", OTHER_MSA, headers=OTHER))
    r = await client.post(URL, json={"document_ids": [mine, theirs]}, headers=DEMO)
    assert r.status_code == 404 and r.json()["error"]["details"] == {"document_ids": [theirs]}


async def test_compare_requires_ready_documents(client: AsyncClient) -> None:
    ids = await _two_docs(client)
    await sql("UPDATE documents SET status = 'processing' WHERE id = :id", id=ids[1])
    r = await client.post(URL, json={"document_ids": ids}, headers=DEMO)
    assert r.status_code == 409 and r.json()["error"]["code"] == "DOCUMENT_NOT_READY"
    assert r.json()["error"]["details"] == {"document_ids": [ids[1]]}


async def test_compare_too_large_points_to_chat(client: AsyncClient, fake: FakeProvider,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    ids = await _two_docs(client)
    monkeypatch.setattr(compare, "COMPARE_MAX_INPUT_TOKENS", 10)
    r = await client.post(URL, json={"document_ids": ids}, headers=DEMO)
    assert r.status_code == 422
    error = r.json()["error"]
    assert error["code"] == "COMPARE_TOO_LARGE" and error["details"]["hint"] == "use multi-document chat"
    assert not [c for c in fake.calls if c.get("schema") == "Comparison"]
