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
    ]}


def test_only_changed_verdicts_are_listed_with_the_reason(report: dict[str, Any]) -> None:
    rows = rescore.rescore(report)
    changed = [r for r in rows if r["changed"]]
    assert [(r["id"], r["old"], r["new"]) for r in changed] == [("f-contractors-parental", "fail", "pass")]
    why = changed[0]["why"]
    assert "any_of: fail -> pass ('are excluded' matched 'are explicitly excluded')" in why
    assert "labels differ from the stored ones" in why                  # the current golden adds `grounded: true`
    assert [r["id"] for r in rows] == ["f-contractors-parental", "f-pto-typo"]   # a run with no answer is skipped


def test_main_prints_the_summary_and_writes_json(report: dict[str, Any], tmp_path: Path,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "20261021T163824Z.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    assert rescore.main([str(path), "--json", str(tmp_path / "rescored.json")]) == 0
    out = capsys.readouterr().out
    assert "First runs: 1/2 passed as stored, 2/2 now" in out
    assert "| f-contractors-parental #1 | fail | pass | any_of: fail -> pass" in out
    saved = json.loads((tmp_path / "rescored.json").read_text())
    assert saved["first_runs"] == {"stored": {"ok": 1, "n": 2}, "now": {"ok": 2, "n": 2}}
