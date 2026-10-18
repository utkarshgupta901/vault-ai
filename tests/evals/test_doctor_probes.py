"""make doctor makes one real model call and believes readiness about the AI key:
  - a vector-mode /search for a query unique to the run (no embedding-cache hit), which FAILs when the call is
    rejected or the search quietly fell back to keyword;
  - a FAIL when /health/ready says the AI provider rejected our key or model (a soft check, so it used to WARN).
Against the in-memory fake API, like the other script tests."""

import re
from typing import Any

import httpx
import pytest

from evals.corpus import DEMO_DOCS
from tests.evals.fake_api import FakeVaultApi, envelope, transport
from tests.evals.test_scripts import load_script

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
AUTH_FAILED = {"ok": False, "provider": "openai", "auth_failed": True,
               "error": "AI provider rejected our credentials (HTTP 401, embeddings: text-embedding-3-small)"}


class ProbedApi(FakeVaultApi):
    """The fake API, plus what a broken embeddings path looks like from outside."""

    def __init__(self, *, vector_mode: str = "vector", vector_status: int = 200,
                 ai_check: dict[str, Any] | None = None):
        super().__init__()
        self.vector_mode, self.vector_status, self.ai_check = vector_mode, vector_status, ai_check
        self.vector_queries: list[str] = []

    def search(self, request: httpx.Request) -> httpx.Response:
        if request.url.params.get("mode") != "vector":
            return super().search(request)
        self.vector_queries.append(request.url.params["q"])
        if self.vector_status != 200:
            return envelope(self.vector_status, "UPSTREAM_AI_ERROR")
        body = super().search(request).json()
        return httpx.Response(200, json={**body, "mode": self.vector_mode})     # the legs that actually ran

    def ready(self, request: httpx.Request) -> httpx.Response:
        body = super().ready(request).json()
        if self.ai_check is not None:
            body["checks"]["ai"] = self.ai_check
            body["status"] = "ok" if self.ai_check["ok"] else "degraded"
        return httpx.Response(200, json=body)


def seeded(api: ProbedApi) -> ProbedApi:
    assert load_script("seed_demo").main(["--docs-dir", str(DEMO_DOCS)], transport=transport(api)) == 0
    return api


def doctor_line(out: str, name: str) -> str:
    return next(ln for ln in out.splitlines() if name in ln)


def test_doctor_runs_a_real_embedding_call(capsys: pytest.CaptureFixture[str]) -> None:
    api = seeded(ProbedApi())
    doctor = load_script("doctor")
    assert doctor.main(["--api", "http://vault.test"], transport=transport(api)) == 0
    assert doctor.main(["--api", "http://vault.test"], transport=transport(api)) == 0
    first, second = api.vector_queries
    assert UUID.search(first) and first != second              # unique per run: the embedding cache can't answer
    assert doctor_line(capsys.readouterr().out, "embedding probe").startswith("PASS")


@pytest.mark.parametrize("api", [ProbedApi(vector_mode="keyword"), ProbedApi(vector_status=502),
                                 ProbedApi(vector_status=429)], ids=["fell-back-to-keyword", "502", "429"])
def test_doctor_fails_when_the_embedding_call_does_not_work(api: ProbedApi,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    assert load_script("doctor").main(["--api", "http://vault.test"], transport=transport(seeded(api))) == 1
    assert doctor_line(capsys.readouterr().out, "embedding probe").startswith("FAIL")


def test_embedding_probe_is_skipped_without_a_ready_document(capsys: pytest.CaptureFixture[str]) -> None:
    """The API embeds the query only when there is a document to search; an empty vault fails elsewhere."""
    api = ProbedApi()
    load_script("doctor").main(["--api", "http://vault.test"], transport=transport(api))
    assert api.vector_queries == []
    assert doctor_line(capsys.readouterr().out, "embedding probe").startswith("WARN")


def test_doctor_fails_when_readiness_reports_a_rejected_ai_key(capsys: pytest.CaptureFixture[str]) -> None:
    api = seeded(ProbedApi(ai_check=AUTH_FAILED))
    assert load_script("doctor").main(["--api", "http://vault.test"], transport=transport(api)) == 1
    assert doctor_line(capsys.readouterr().out, "  ai").startswith("FAIL")


def test_assess_ready_levels_for_the_ai_check() -> None:
    doctor = load_script("doctor")
    database = {"ok": True, "ms": 2}

    def ai_level(ai: dict[str, Any]) -> str:
        checks = doctor.assess_ready(200, {"status": "degraded", "checks": {"database": database, "ai": ai}})
        return {c.name.strip(): c.level for c in checks}["ai"]

    assert ai_level(AUTH_FAILED) == "FAIL"
    assert ai_level({"ok": False, "auth_failed": False, "error": "last 3 generation calls failed"}) == "WARN"
    assert ai_level({"ok": True, "auth_failed": False}) == "PASS"
