"""The eval runner end to end against an in-memory fake of the HTTP API (tests/evals/fake_api.py): corpus upload,
public_doc skipping, retrieval metrics, multi-turn order, demo repeats (pass^N), refusal paths and report files.
No compose stack and no model are involved."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from evals import run_eval
from tests.evals.fake_api import FakeVaultApi, transport

DOCS = {"A.txt": "Employees get 18 days of PTO per year.", "B.txt": "Incident LRK-4471 cost $120k in total."}

GOLDEN = """
corpus: [A.txt, B.txt]
cases:
  - id: f-pto
    question: "How many PTO days?"
    tags: [factoid, demo]
    scope: all
    expect: {any_of: ["18 days"], must_cite: A.txt, gold_chunk: ["18 days of PTO"]}
  - id: o-fifa
    question: "Who won the World Cup?"
    tags: [unanswerable_offtopic]
    scope: all
    expect: {not_found: true}
  - id: t-cost
    turns: ["What happened with LRK-4471?"]
    question: "How much did it cost?"
    retrieval_query: "LRK-4471 cost"
    tags: [multiturn]
    scope: [B.txt]
    expect: {any_of: ["120k"], must_cite: B.txt, gold_chunk: ["cost $120k"]}
  - id: p-public
    question: "What does the public document say?"
    tags: [public_doc]
    scope: [Public.pdf]
    expect: {any_of: ["something"], must_cite: Public.pdf, gold_chunk: ["something"]}
"""


def _cited(content: str, filename: str, **extra: Any) -> dict[str, Any]:
    return {"content": content, "answer_type": "answered", "confidence": "high",
            "citations": [{"ref": "S1", "filename": filename, "label": "p. 1", "page_start": 1}],
            "signals": {"valid_citations": 1, "invalid_citations": 0, "ungrounded_facts": 0}, **extra}


def answer(question: str, prior: list[str], ids: list[str]) -> dict[str, Any]:
    if "PTO" in question:
        return _cited("You get 18 days [S1].", "A.txt")
    if "cost" in question and prior:                    # only answerable once the earlier turn gave the context
        return _cited("It cost $120k [S1].", "B.txt")
    if "LRK-4471" in question:
        return _cited("An outage [S1].", "B.txt")
    return {"content": "I couldn't find this in your documents.", "answer_type": "not_found", "confidence": "low",
            "citations": [], "signals": {"gate": True}, "cost_usd": "0"}


def search(query: str, mode: str, ids: list[str]) -> list[dict[str, Any]]:
    order = ["A.txt", "B.txt"] if "PTO" in query else ["B.txt", "A.txt"]
    return [{"chunk_id": i, "filename": name, "label": "p. 1", "content": DOCS[name], "lex_rank": i, "sem_rank": i,
             "id_hit": "LRK" in query, "snippet": DOCS[name][:20]} for i, name in enumerate(order, start=1)]


@pytest.fixture
def setup(tmp_path: Path) -> dict[str, Path]:
    docs = tmp_path / "docs"
    (docs / "user").mkdir(parents=True)
    for name, text in DOCS.items():
        (docs / name).write_text(text, encoding="utf-8")
    (docs / "user" / "Mine.txt").write_text("The user's own document.", encoding="utf-8")
    golden_path = tmp_path / "golden.yaml"
    golden_path.write_text(GOLDEN, encoding="utf-8")
    return {"docs": docs, "golden": golden_path, "out": tmp_path / "reports"}


def run(setup: dict[str, Path], api: FakeVaultApi, *extra: str) -> tuple[int, dict[str, Any]]:
    argv = ["--api", "http://vault.test", "--key", "eval-key", "--golden", str(setup["golden"]), "--no-starters",
            "--docs-dir", str(setup["docs"]), "--out", str(setup["out"]), *extra]
    code = run_eval.main(argv, transport=transport(api))
    reports = sorted(setup["out"].glob("*.json")) if setup["out"].exists() else []
    return code, (json.loads(reports[-1].read_text()) if reports else {})


def test_full_run_scores_everything(setup: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    api = FakeVaultApi(answer_fn=answer, search_fn=search)
    code, report = run(setup, api, "--repeat", "3")
    assert code == 0
    meta = report["meta"]
    assert meta["skipped_cases"] == ["p-public"]                       # Public.pdf is absent: skipped, not failed
    assert set(meta["corpus"]) == {"A.txt", "B.txt", "Mine.txt"}       # demo_docs/user/* is uploaded too
    assert meta["health"]["models"]["answer"] == "fake-answer"

    hybrid = report["retrieval"]["summary"]["hybrid"]
    assert hybrid["overall"]["hit@1"] == {"hits": 2, "n": 2, "rate": 1.0}
    assert hybrid["overall"]["mrr"] == 1.0
    assert set(report["retrieval"]["summary"]) == {"hybrid", "vector", "keyword"}
    lrk = next(r for r in report["retrieval"]["records"] if r["case_id"] == "t-cost")
    assert lrk["query"] == "LRK-4471 cost" and lrk["gold_id_hit"] is True

    answers = report["answers"]
    assert answers["passed"] == {"ok": 3, "n": 3, "rate": 1.0}
    assert answers["demo"] == [{"id": "f-pto", "passes": 3, "runs": 3, "stable": True, "flaky": False}]
    assert answers["refusals"]["by_path"]["gate"]["correct"] == 1
    assert answers["refusals"]["expected_path"] == {"matched": 1, "n": 1}
    assert answers["server"]["prompt_versions"] == {"v2": 5}            # 3 demo runs + 2 single runs
    assert answers["cost_usd"]["answers"] == 5

    sessions = [q for q in api.questions.values() if q[-1] == "How much did it cost?"]
    assert sessions == [["What happened with LRK-4471?", "How much did it cost?"]]   # prior turn asked first
    printed = capsys.readouterr().out
    assert "| hybrid | 2 | 2/2 (100%)" in printed and "Skipped (document not present): p-public" in printed
    assert len(list(setup["out"].glob("*.md"))) == 1


def test_flaky_demo_case_fails_the_run(setup: dict[str, Path]) -> None:
    calls = {"n": 0}

    def flaky(question: str, prior: list[str], ids: list[str]) -> dict[str, Any]:
        if "PTO" in question:
            calls["n"] += 1
            if calls["n"] == 2:
                return _cited("You get 20 days [S1].", "A.txt")
        return answer(question, prior, ids)

    code, report = run(setup, FakeVaultApi(answer_fn=flaky, search_fn=search), "--repeat", "3", "--tag", "demo")
    assert code == 1
    assert report["answers"]["demo"][0] == {"id": "f-pto", "passes": 2, "runs": 3, "stable": False, "flaky": True}
    failure = next(c for c in report["cases"] if c["id"] == "f-pto")["runs"][1]
    assert failure["failed"] == ["any_of"]


def test_retrieval_only_spends_nothing_and_falls_back_on_k(setup: dict[str, Path]) -> None:
    api = FakeVaultApi(answer_fn=answer, search_fn=search)
    code, report = run(setup, api, "--retrieval-only", "--k", "50", "--modes", "hybrid")
    assert code == 0
    assert report["answers"] == {"skipped": "--retrieval-only"}
    assert report["retrieval"]["k"] == 8 and report["retrieval"]["ks"] == [1, 5, 8]
    assert not api.sessions


def test_missing_chat_api_is_a_setup_error(setup: dict[str, Path]) -> None:
    api = FakeVaultApi(routes={"/api/v1/documents", "/api/v1/search"})
    code, report = run(setup, api)
    assert code == 1 and report == {}


def test_missing_corpus_file_is_a_setup_error(setup: dict[str, Path]) -> None:
    (setup["docs"] / "B.txt").unlink()
    code, _ = run(setup, FakeVaultApi(answer_fn=answer, search_fn=search))
    assert code == 1


def test_a_failed_answer_is_recorded_not_fatal(setup: dict[str, Path]) -> None:
    def slow(question: str, prior: list[str], ids: list[str]) -> dict[str, Any]:
        if "World Cup" in question:
            raise httpx.ReadTimeout("answer took too long")
        return answer(question, prior, ids)

    code, report = run(setup, FakeVaultApi(answer_fn=slow, search_fn=search), "--case", "o-fifa,f-pto")
    assert code == 0                                   # only demo cases decide the exit code
    assert report["answers"]["passed"] == {"ok": 1, "n": 2, "rate": 0.5}
    assert report["answers"]["errors"] == ["o-fifa"]
