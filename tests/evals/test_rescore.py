"""evals/rescore.py: re-score a stored report's answers with the current scorer and labels, no API calls, and say
which verdicts changed and why."""

import json
from pathlib import Path
from typing import Any

import pytest

from evals import rescore

SIGNALS = {"valid_citations": 1, "invalid_citations": 0, "ungrounded_facts": 0, "gate": False}


def _run(content: str, passed: bool, failed: list[str], checks: dict[str, Any]) -> dict[str, Any]:
    return {"passed": passed, "failed": failed, "checks": checks, "error": None, "answer": {
        "content": content, "answer_type": "answered", "signals": SIGNALS,
        "citations": [{"ref": "S1", "filename": "Larkspur_Employee_Handbook_2026.pdf"}]}}


@pytest.fixture
def report() -> dict[str, Any]:
    """Two cases as a 2026-10-21 report stored them: the contractors answer failed any_of under the old matcher, the
    PTO answer passed. Labels as stored then (no `grounded` gating yet)."""
    contractors = {"any_of": ["not eligible", "are excluded"], "must_cite": "Larkspur_Employee_Handbook_2026.pdf",
                   "gold_chunk": ["are not eligible for parental leave"]}
    pto = {"any_of": ["18 days"], "must_cite": "Larkspur_Employee_Handbook_2026.pdf", "gold_chunk": ["18 days"]}
    return {"meta": {"git_sha": "71be0fa"}, "cases": [
        {"id": "f-contractors-parental", "expect": contractors, "runs": [_run(
            "No. Contractors are explicitly excluded from parental leave under clause 4.1. [S1]", False, ["any_of"],
            {"any_of": False, "not_found": True})]},
        {"id": "f-pto-typo", "expect": pto, "runs": [_run("New hires get **18 days of PTO**. [S1]", True, [],
                                                          {"any_of": True, "not_found": True})]},
        {"id": "o-gone", "expect": {"not_found": True}, "runs": [{"passed": False, "failed": ["error"], "checks": {},
                                                                 "error": "timeout", "answer": None}]},
        {"id": "h-retired", "tags": ["factoid", "hard"], "expect": pto, "runs": [_run(
            "Full-timers get twenty days. [S1]", False, ["any_of"], {"any_of": False, "not_found": True})]},
    ]}


def test_only_changed_verdicts_are_listed_with_the_reason(report: dict[str, Any]) -> None:
    rows = rescore.rescore(report)
    changed = [r for r in rows if r["changed"]]
    assert [(r["id"], r["old"], r["new"]) for r in changed] == [("f-contractors-parental", "fail", "pass")]
    why = changed[0]["why"]
    assert "any_of: fail -> pass ('are excluded' matched 'are explicitly excluded')" in why
    assert "labels differ from the stored ones" in why                  # the current golden adds `grounded: true`
    gone = next(r for r in rows if r["id"] == "o-gone")
    assert (gone["old"], gone["new"], gone["changed"], gone["error"]) == ("fail", "fail", False, "timeout")


def test_a_run_whose_call_failed_counts_as_a_failure(report: dict[str, Any]) -> None:
    """Regression: runs without an answer were left out of the totals, so a report with one failed call read 42/42
    instead of 42/43. There is nothing to re-score, so the run stays a failure."""
    assert rescore.first_runs(rescore.rescore(report))["core"] == {"stored": {"ok": 1, "n": 3},
                                                                   "now": {"ok": 2, "n": 3}}


def test_first_runs_keep_the_hard_set_out_of_the_headline(report: dict[str, Any]) -> None:
    """Regression: the totals mixed the 43 original cases with the hard set ("55/56"), unlike the report itself.
    A case's set comes from the current golden set, or from its stored tags when the case is gone."""
    rows = rescore.rescore(report)
    assert {r["id"]: r["set"] for r in rows} == {"f-contractors-parental": "core", "f-pto-typo": "core",
                                                 "o-gone": "core", "h-retired": "hard"}
    assert rescore.first_runs(rows) == {"core": {"stored": {"ok": 1, "n": 3}, "now": {"ok": 2, "n": 3}},
                                        "hard": {"stored": {"ok": 0, "n": 1}, "now": {"ok": 0, "n": 1}}}
    without_hard = {**report, "cases": report["cases"][:3]}
    assert set(rescore.first_runs(rescore.rescore(without_hard))) == {"core"}


def test_a_failed_call_in_the_hard_set_counts_in_the_hard_set(report: dict[str, Any]) -> None:
    """A run without an answer keeps its case's set: a hard case's failed call is a hard-set failure, never one of
    the original cases (the headline would read 1/4 instead of 1/3)."""
    unanswered = {"id": "h-timed-out", "tags": ["factoid", "hard"], "expect": report["cases"][1]["expect"],
                  "runs": [{"passed": False, "failed": ["error"], "checks": {}, "error": "timeout", "answer": None}]}
    rows = rescore.rescore({**report, "cases": report["cases"][:3] + [unanswered]})
    assert rescore.first_runs(rows) == {"core": {"stored": {"ok": 1, "n": 3}, "now": {"ok": 2, "n": 3}},
                                        "hard": {"stored": {"ok": 0, "n": 1}, "now": {"ok": 0, "n": 1}}}


def test_main_prints_the_summary_and_writes_json(report: dict[str, Any], tmp_path: Path,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "20261021T163824Z.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    assert rescore.main([str(path), "--json", str(tmp_path / "rescored.json")]) == 0
    out = capsys.readouterr().out
    assert "- First runs, original cases: 1/3 passed as stored, 2/3 now" in out
    assert "- First runs, hard set: 0/1 passed as stored, 0/1 now" in out
    assert "| f-contractors-parental #1 | fail | pass | any_of: fail -> pass" in out
    saved = json.loads((tmp_path / "rescored.json").read_text())
    assert saved["first_runs"]["core"] == {"stored": {"ok": 1, "n": 3}, "now": {"ok": 2, "n": 3}}
