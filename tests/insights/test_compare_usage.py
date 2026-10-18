"""POST /documents/compare: the paid model call reaches the cost ledger even when what follows it does not finish
(post-processing raises, or the request is cancelled because the client went away)."""

import uuid
from typing import Any

import anyio
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.ai import compare
from app.ai.compare import CompareRequest
from app.ai.fake_provider import FakeProvider
from app.core.db import async_session
from app.models import AIUsage
from tests.insights.helpers import MSA_TEXT, upload_ready

OTHER_MSA = "Northwind Services Agreement.\n8.1 Either party may terminate on thirty (30) days' written notice.\n"


async def _request(client: AsyncClient) -> CompareRequest:
    ids = [await upload_ready(client, "brightline.txt", MSA_TEXT), await upload_ready(client, "northwind.txt", OTHER_MSA)]
    return CompareRequest(document_ids=ids)


async def _compare_rows() -> int:
    async with async_session() as s:
        return await s.scalar(select(func.count()).select_from(AIUsage).where(AIUsage.operation == "compare")) or 0


async def test_usage_is_recorded_when_postprocessing_fails(client: AsyncClient, fake: FakeProvider,
                                                           demo_user_id: uuid.UUID,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    req = await _request(client)

    def broken(*_: Any) -> Any:
        raise RuntimeError("postprocess bug")

    monkeypatch.setattr(compare, "postprocess", broken)
    with pytest.raises(RuntimeError, match="postprocess bug"):
        await compare.compare_documents(demo_user_id, req)
    assert await _compare_rows() == 1


async def test_usage_is_recorded_when_the_request_is_cancelled_after_the_call(client: AsyncClient,
                                                                              fake: FakeProvider,
                                                                              demo_user_id: uuid.UUID) -> None:
    """Cancelled the moment the model answers (the client disconnected): the next await used to be the ledger
    write itself, so the paid call vanished from the daily quota."""
    req = await _request(client)
    original = fake.complete_json
    with anyio.CancelScope() as scope:
        def answer_then_disconnect(**kwargs: Any) -> Any:
            result = original(**kwargs)
            scope.cancel()
            return result

        fake.complete_json = answer_then_disconnect  # type: ignore[method-assign]
        await compare.compare_documents(demo_user_id, req)
    assert await _compare_rows() == 1
