"""The eval runner end to end against an in-memory fake of the HTTP API (tests/evals/fake_api.py): corpus upload,
public_doc skipping, retrieval metrics, multi-turn order, demo repeats (pass^N), refusal paths and report files.
No compose stack and no model are involved."""

import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from evals import run_eval
from evals.report import render_markdown
from tests.evals.fake_api import ALL_ROUTES, SIGNALS, FakeVaultApi, transport

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
  - id: h-vacation
    question: "How much yearly vacation do I get?"
    tags: [factoid, hard]
    scope: all
    expect: {any_of: ["18 days"], must_cite: A.txt, gold_chunk: ["18 days of PTO"]}
"""


def _cited(content: str, filename: str, **extra: Any) -> dict[str, Any]:
    return {"content": content, "answer_type": "answered", "confidence": "high",
            "citations": [{"ref": "S1", "filename": filename, "label": "p. 1", "page_start": 1}],
            "signals": {**SIGNALS, "valid_citations": 1}, **extra}


def answer(question: str, prior: list[str], ids: list[str]) -> dict[str, Any]:
    if "PTO" in question:
        return _cited("You get 18 days [S1].", "A.txt")
    if "vacation" in question:                          # right, in words the any_of labels don't list
        return _cited("Full-timers get eighteen paid days off [S1].", "A.txt")
    if "cost" in question and prior:                    # only answerable once the earlier turn gave the context
        return _cited("It cost $120k [S1].", "B.txt")
    if "LRK-4471" in question:
        return _cited("An outage [S1].", "B.txt")
    return {"content": "I couldn't find this in your documents.", "answer_type": "not_found", "confidence": "low",
            "citations": [], "signals": {**SIGNALS, "gate": True}, "cost_usd": "0", "model": None, "ttft_ms": 8}


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


def run(setup: dict[str, Path], api: FakeVaultApi, *extra: str, judge_fn: Any = None) -> tuple[int, dict[str, Any]]:
    argv = ["--api", "http://vault.test", "--key", "eval-key", "--golden", str(setup["golden"]), "--no-starters",
            "--docs-dir", str(setup["docs"]), "--out", str(setup["out"]), *extra]
    code = run_eval.main(argv, transport=transport(api), judge_fn=judge_fn)
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
    assert answers["passed"] == {"ok": 3, "n": 3, "rate": 1.0}                 # first run of each case
    assert answers["passed_all_runs"] == {"ok": 5, "n": 5, "rate": 1.0}        # + the demo case's 2 repeats
    assert answers["demo"] == [{"id": "f-pto", "passes": 3, "runs": 3, "stable": True, "flaky": False}]
    assert answers["refusals"]["by_path"]["gate"]["correct"] == 1
    assert answers["refusals"]["expected_path"] == {"matched": 1, "n": 1}
    assert answers["server"]["prompt_versions"] == {"v2": 5}            # 3 demo runs + 2 single runs
    assert answers["cost_usd"]["answers"] == 5
    # TTFT twice: the gate refusal (8 ms, no model call) is left out of the model-only numbers
    latency = answers["latency_ms"]
    assert latency["ttft_model"] == {"n": 4, "p50": 100.0, "p95": 100.0, "avg": 100.0, "max": 100.0}
    assert latency["server_total"]["max"] == 900.0
    assert latency["ttft_all"]["n"] == 5 and latency["ttft_all"]["avg"] == (4 * 100 + 8) / 5
    assert latency["ttft"] == latency["ttft_all"]                       # the old key keeps its old meaning
    assert "git_dirty" in meta                                          # next to git_sha; see test_git_dirty_flag

    sessions = [q for q in api.questions.values() if q[-1] == "How much did it cost?"]
    assert sessions == [["What happened with LRK-4471?", "How much did it cost?"]]   # prior turn asked first
    printed = capsys.readouterr().out
    assert "| hybrid | 2 | 2/2 (100%)" in printed and "Skipped (document not present): p-public" in printed
    assert "| TTFT (server, model-generated answers only) | 4 | 100 | 100 | 100 |" in printed
    assert "| TTFT (server, all answers incl. gate refusals) | 5 | 100 | 100 | 100 |" in printed
    assert "| total (server) | 5 | 900 | 900 | 900 |" in printed
    assert "- First-run pass rate: **3/3 (100%)** (all runs incl. demo repeats: 5/5 (100%))" in printed
    assert "query embeddings mostly already cached" in printed         # the TTFT caveat travels with the numbers
    assert "the retrieval phase above embeds" in printed
    # latency n (5) is runs, not cases (3): the report has to say so next to the first-run headline
    assert "Latency and cost count every run, demo repeats included" in printed
    assert len(list(setup["out"].glob("*.md"))) == 1
    assert "judge" not in report and "LLM judge" not in printed          # --judge is opt-in


def test_the_hard_set_is_reported_apart_from_the_headline(setup: dict[str, Path]) -> None:
    """A hard case that fails (the answer is right, the labels miss its wording) must not move the headline, which
    stays comparable with reports that had no hard set; it gets its own retrieval and answer lines."""
    code, report = run(setup, FakeVaultApi(answer_fn=answer, search_fn=search), "--repeat", "3")
    assert code == 0                                                     # hard cases never decide the exit code
    answers = report["answers"]
    assert answers["passed"] == {"ok": 3, "n": 3, "rate": 1.0}
    assert answers["cost_usd"]["answers"] == 5 and "hard" not in answers["by_tag"]
    hard = answers["hard"]
    assert hard["passed"] == {"ok": 0, "n": 1, "rate": 0.0} and hard["cost_usd"]["answers"] == 1
    assert report["retrieval"]["summary"]["hybrid"]["overall"]["n"] == 2
    assert report["retrieval"]["hard"]["hybrid"]["overall"]["hit@1"] == {"hits": 0, "n": 1, "rate": 0.0}
    md = render_markdown(report)
    assert "- First-run pass rate: **3/3 (100%)**" in md
    assert "## Hard set (1 case, tag `hard`, not in the headline above)" in md
    assert "- First-run pass rate (hard set): **0/1 (0%)**" in md
    assert "| hybrid | 1 | 0/1 (0%)" in md                                # the hard set's retrieval line
    assert "| h-vacation #1 | hard | any_of |" in md                      # failures say which set they are in
    # the spend line is the stack's whole bill for the run, unlike the cost lines (answers of one set each)
    assert "AI spend during the whole run (eval user, /metrics/ai-usage; every call, hard set included)" in md


def fake_judge(case: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    """Grades like a careful reader: the vacation answer is right in other words; the LRK-4471 answer is called
    wrong (as if it had confused two incidents) although the labels pass it; the PTO answer is right."""
    if case["id"] == "t-cost":
        return {"correct": False, "rationale": "names the wrong incident", "model": "fake-judge"}
    if case["id"] == "f-pto":
        assert case["expect"]["any_of"] == ["18 days"] and run["answer"]["content"] == "You get 18 days [S1]."
    return {"correct": True, "rationale": "gives the key fact", "model": "fake-judge"}


def test_judge_agreement_is_reported_and_never_gates(setup: dict[str, Path]) -> None:
    code, report = run(setup, FakeVaultApi(answer_fn=answer, search_fn=search), "--judge", judge_fn=fake_judge)
    assert code == 0
    by_id = {c["id"]: c for c in report["cases"]}
    assert by_id["t-cost"]["runs"][0]["passed"] and not by_id["h-vacation"]["runs"][0]["passed"]   # unchanged
    judge = report["judge"]["summary"]
    assert (judge["judged"], judge["agree"], judge["disagree"]) == (3, 1, 2)       # o-fifa refused: not judged
    assert judge["code_pass_judge_fail"] == ["t-cost"] and judge["code_fail_judge_pass"] == ["h-vacation"]
    assert judge["by_set"] == {"core": {"judged": 2, "agree": 1}, "hard": {"judged": 1, "agree": 0}}
    assert judge["models"] == {"fake-judge": 3} and judge["errors"] == []
    row = next(r for r in report["judge"]["rows"] if r["id"] == "h-vacation")
    assert row == {"id": "h-vacation", "set": "hard", "code": False, "judge": True, "failed": ["any_of"],
                   "rationale": "gives the key fact", "model": "fake-judge", "error": None}
    md = render_markdown(report)
    assert "## LLM judge agreement (--judge, never gates)" in md
    assert "- Agree: 1/3 (33%) | original cases 1/2 | hard set 0/1" in md
    assert "| t-cost | core | pass | fail | - | names the wrong incident |" in md
    assert "| h-vacation | hard | fail | pass | any_of | gives the key fact |" in md


def test_a_judge_error_is_recorded_not_fatal(setup: dict[str, Path]) -> None:
    def flaky_judge(case: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        if case["id"] == "t-cost":
            raise TimeoutError("judge timed out")
        return fake_judge(case, run)

    code, report = run(setup, FakeVaultApi(answer_fn=answer, search_fn=search), "--judge", judge_fn=flaky_judge)
    assert code == 0
    judge = report["judge"]["summary"]
    assert judge["errors"] == ["t-cost"] and judge["judged"] == 2
    assert next(r for r in report["judge"]["rows"] if r["id"] == "t-cost")["error"] == "TimeoutError: judge timed out"
    assert "- Judge errors (not counted): t-cost" in render_markdown(report)


def test_judge_against_another_stack_needs_that_stacks_ledger(setup: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
                                                              capsys: pytest.CaptureFixture[str]) -> None:
    """DATABASE_URL defaults to the default stack's database. Judging an API on another port with that default would
    write the judge's ai_usage rows into a stack it never graded, so the run stops before any request or spend."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    api = FakeVaultApi(answer_fn=answer, search_fn=search)
    code, report = run(setup, api, "--api", "http://localhost:8001", "--judge")
    assert code == 1 and report == {} and api.calls == []
    assert "DATABASE_URL is not set" in capsys.readouterr().err


def test_judge_ledger_problem() -> None:
    assert run_eval.judge_ledger_problem("http://localhost:8000", {}) is None             # the default stack's DB
    assert run_eval.judge_ledger_problem("http://localhost:8000/", {}) is None
    assert "DATABASE_URL is not set" in run_eval.judge_ledger_problem("http://localhost:8001", {})
    url = "postgresql+psycopg://vault:vault@localhost:5434/vault"
    assert run_eval.judge_ledger_problem("http://localhost:8001", {"DATABASE_URL": url}) is None


def test_the_report_names_the_server_code_only_when_told(setup: dict[str, Path]) -> None:
    """The API does not report its build, and git_sha is the eval client's checkout: the server's code is recorded
    only when the person running the eval states it (--server-ref), and the report says so either way."""
    code, report = run(setup, FakeVaultApi(answer_fn=answer, search_fn=search), "--server-ref", "review-fixes@1e534b3")
    assert code == 0 and report["meta"]["server_ref"] == "review-fixes@1e534b3"
    assert "- Server code: `review-fixes@1e534b3` (stated with --server-ref" in render_markdown(report)
    code, report = run(setup, FakeVaultApi(answer_fn=answer, search_fn=search))
    assert report["meta"]["server_ref"] is None
    assert "- Server code: not recorded (" in render_markdown(report)


def test_ttft_caveat_only_cites_a_retrieval_phase_that_ran(setup: dict[str, Path]) -> None:
    """Regression: the caveat said "the retrieval phase above embeds the same questions" even when /search was not
    deployed and the run skipped retrieval, so nothing above had warmed the query-embedding cache."""
    api = FakeVaultApi(answer_fn=answer, search_fn=search, routes=ALL_ROUTES - {"/api/v1/search"})
    code, report = run(setup, api)
    assert code == 0 and "skipped" in report["retrieval"]
    md = render_markdown(report)
    assert "retrieval phase above" not in md
    assert "this run had no retrieval phase" in md and "about 0.5 s at the median" in md


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


@pytest.mark.parametrize(("signals", "missing"), [
    (None, "['valid_citations', 'invalid_citations', 'ungrounded_facts', 'gate']"),
    ({k: v for k, v in SIGNALS.items() if k != "ungrounded_facts"}, "['ungrounded_facts']"),
])
def test_an_answer_without_signals_fails_and_says_why(setup: dict[str, Path], signals: dict | None,
                                                      missing: str) -> None:
    """Regression: an answer without `signals` (dropped or renamed on the server) used to score as 0 invalid
    citations and 0 ungrounded facts. The runner now fails that run and names the cause."""
    def unsignalled(question: str, prior: list[str], ids: list[str]) -> dict[str, Any]:
        reply = answer(question, prior, ids)
        return {**reply, "signals": signals} if "PTO" in question else reply

    code, report = run(setup, FakeVaultApi(answer_fn=unsignalled, search_fn=search), "--case", "f-pto,o-fifa")
    pto = next(c for c in report["cases"] if c["id"] == "f-pto")["runs"][0]
    assert not pto["passed"] and pto["failed"][0] == "signals"
    assert f"answer lacks signals {missing}" in pto["error"]
    assert report["answers"]["errors"] == ["f-pto"] and report["answers"]["passed"]["ok"] == 1
    assert code == 1                                   # f-pto is a demo case


def test_reports_written_before_the_ttft_split_still_render() -> None:
    """Older reports have only latency_ms.ttft (all answers) and no git_dirty; the renderer must still read them."""
    old = json.loads((Path(run_eval.ROOT) / "evals" / "reports" / "20261013T170823Z.json").read_text())
    assert "ttft_model" not in old["answers"]["latency_ms"] and "git_dirty" not in old["meta"]
    md = render_markdown(old)
    assert "| TTFT (server, all answers incl. gate refusals) | 30 |" in md and "model-generated" not in md
    assert "| total (server) | 30 | 4,035 | 9,068 | n/a |" in md                # no max recorded before 2026-10-18
    assert "- First-run pass rate: **10/10 (100%)**\n" in md                  # no all-runs count either
    assert "git `b8985a3` |" in md
    old["meta"]["git_dirty"] = True
    assert "git `b8985a3` (dirty: uncommitted changes) |" in render_markdown(old)


def test_git_dirty_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    for porcelain, dirty in ((" M app/rag/citations.py\n?? notes.txt", True), ("", False), (None, None)):
        monkeypatch.setattr(run_eval, "_git", lambda *args, out=porcelain: out)
        assert run_eval.git_dirty() is dirty


def test_git_dirty_ignores_the_reports_the_eval_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: reports are committed next to the code, so after any eval the previous run's report files were
    untracked and every following run on unchanged code was stamped dirty. Runs real git in a scratch repository."""
    def git(*args: str) -> None:
        subprocess.run(["git", "-c", "user.name=eval", "-c", "user.email=eval@example.com", "-c",
                        "commit.gpgsign=false", *args], cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / "evals" / "reports").mkdir(parents=True)
    (tmp_path / "evals" / "reports" / "20261013T170823Z.json").write_text("{}")
    (tmp_path / "app.py").write_text("x = 1\n")
    git("init", "-q")
    git("add", ".")
    git("commit", "-qm", "code and a first report")
    monkeypatch.setattr(run_eval, "ROOT", tmp_path)
    assert run_eval.git_dirty() is False

    for name in ("20261014T090000Z.json", "20261014T090000Z.md"):        # the last eval's output
        (tmp_path / "evals" / "reports" / name).write_text("{}")
    assert run_eval.git_dirty() is False
    (tmp_path / "notes.txt").write_text("untracked")                    # anything else still counts
    assert run_eval.git_dirty() is True
    (tmp_path / "notes.txt").unlink()
    (tmp_path / "app.py").write_text("x = 2\n")
    assert run_eval.git_dirty() is True
