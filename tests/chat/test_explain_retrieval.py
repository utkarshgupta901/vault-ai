"""scripts/explain_retrieval.py recomputes each hit's fused score from its ranks. It must use retrieve()'s formula,
identifier boost included: without it every identifier hit printed MISMATCH and the script's ranking disagreed with
the real one exactly where the boost matters."""

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fake_provider import fake_vector
from app.config import settings
from app.rag.retrieval import retrieve
from tests.chat.helpers import API, insert_document
from tests.conftest import DEMO

ROOT = Path(__file__).resolve().parents[2]
QUESTION = "What is the notice period for termination under clause 9.2?"
MSA = [
    "9.1 Either party may end the agreement for material breach.",
    "9.2 Either party may end this agreement for convenience on sixty days' warning.",   # few shared words
    "The notice period for termination is described in the termination section of this agreement.",
    "Termination notice must be sent in writing to the address on the cover page.",
    "Any notice period under this agreement starts on the day after delivery.",
]


def _load_script() -> ModuleType:
    """scripts/ is not a package (each file is run directly), so load the script by path."""
    spec = importlib.util.spec_from_file_location("explain_retrieval", ROOT / "scripts" / "explain_retrieval.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_script_ranking_equals_retrieve(client: AsyncClient, db: AsyncSession, demo_user_id: uuid.UUID) -> None:
    doc = await insert_document(db, demo_user_id, "msa.txt", MSA)
    truth = await retrieve(db, owner_id=demo_user_id, document_ids=[doc], query_text=QUESTION,
                           query_vec=fake_vector(QUESTION))
    r = await client.get(f"{API}/search", params={"q": QUESTION}, headers=DEMO)
    assert r.status_code == 200, r.text
    hits = r.json()["hits"]                                     # what the script reads
    assert [h["chunk_id"] for h in hits] == [c.chunk_id for c in truth.chunks]
    assert any(h["id_hit"] for h in hits)                       # the case the script used to get wrong

    script = _load_script()
    for i, hit in enumerate(hits, start=1):
        assert "MISMATCH" not in script.explain_hit(i, hit, settings.RRF_K, settings.LEXICAL_WEIGHT)

    by_id = {c.chunk_id: c for c in truth.chunks}
    score = {h["chunk_id"]: script.fused_score(h, settings.RRF_K, settings.LEXICAL_WEIGHT, settings.ID_HIT_BOOST)
             for h in hits}
    assert score == {cid: pytest.approx(c.rrf) for cid, c in by_id.items()}
    assert sorted(score, key=lambda cid: (-score[cid], cid)) == [c.chunk_id for c in truth.chunks]

    # The corpus really exercises the boost: ranked without it, the 9.2 clause would not come first.
    unboosted = {h["chunk_id"]: script.fused_score(h, settings.RRF_K, settings.LEXICAL_WEIGHT, False) for h in hits}
    assert truth.chunks[0].content.startswith("9.2 ")
    assert max(unboosted, key=lambda cid: (unboosted[cid], -cid)) != truth.chunks[0].chunk_id
