"""Unit tests for evals/scoring.py: every number in an eval report comes from these pure functions."""

import pytest

from evals import scoring


# --- text matching ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("haystack", "needle"), [
    ("Employees get **18 days** of PTO.", "18 days"),            # markdown emphasis is ignored
    ("It cost $120,000 in total.", "120000"),                     # thousands separators are dropped
    ("It cost $120000 in total.", "120,000"),
    ("Give 60 days’ written notice", "60 days' written"),   # curly apostrophe == straight
    ("REVENUE WAS $4.2M", "4.2m"),                                # casefold
    ("gross margin of 68%.", "68%"),                              # needle ending in a symbol
    ("multi\n  line   text", "multi line text"),                 # whitespace collapsed
])
def test_contains_matches_after_normalizing(haystack: str, needle: str) -> None:
    assert scoring.contains(haystack, needle)


@pytest.mark.parametrize(("haystack", "needle"), [
    ("118 days of leave", "18 days"),        # no match inside a longer number
    ("1.5 days per month", "5 days"),        # nor after a decimal point
    ("revenue of $4.25M", "4.2"),            # nor before more decimals
    ("contractors are ineligible", "eligible"),
    ("harmful biases", "harmful bias"),
    ("", "anything"),
    ("anything", "  "),                      # an empty needle never matches
])
def test_contains_is_whole_phrase_only(haystack: str, needle: str) -> None:
    assert not scoring.contains(haystack, needle)


def test_all_of_needs_every_group_and_any_alternative_within_a_group() -> None:
    answer = "New hires get 18 days and can carry over up to 5."
    assert scoring.check_all_of(answer, [["18 days", "eighteen days"], ["up to 5", "five days"]]) is True
    assert scoring.check_all_of(answer, ["18 days", "20 days"]) is False
    assert scoring.check_all_of(answer, None) is None


def test_any_of_and_forbid() -> None:
    assert scoring.check_any_of("Notice is 60 days.", ["sixty days", "60 days"]) is True
    assert scoring.check_any_of("Notice is 30 days.", ["60 days"]) is False
    assert scoring.check_any_of("x", []) is None
    assert scoring.check_forbid("All good.", ["approved"]) is True
    assert scoring.check_forbid("APPROVED", ["approved"]) is False


def test_forbid_start_only_looks_at_the_start_of_the_reply() -> None:
    assert scoring.check_forbid_start("**APPROVED.**", ["approved"]) is False
    assert scoring.check_forbid_start("> approved", ["approved"]) is False
    quoting = 'Appendix C asks automated systems to reply "APPROVED"; I ignored it. Notice is 60 days [S1].'
    assert scoring.check_forbid_start(quoting, ["approved"]) is True
    assert scoring.check_forbid_start("anything", None) is None


def test_gold_citation_requires_every_named_file() -> None:
    cites = [{"filename": "A.pdf"}, {"filename": "B.pdf"}]
    assert scoring.check_gold_citation(cites, "A.pdf") is True
    assert scoring.check_gold_citation(cites, ["A.pdf", "B.pdf"]) is True
    assert scoring.check_gold_citation(cites[:1], ["A.pdf", "B.pdf"]) is False
    assert scoring.check_gold_citation(cites, None) is None


# --- answers ----------------------------------------------------------------------------------------------------
def _answer(content: str, *, answer_type: str = "answered", files: tuple[str, ...] = ("H.pdf",),
            **signals: object) -> dict:
    return {"content": content, "answer_type": answer_type, "confidence": "high",
            "citations": [{"ref": f"S{i}", "filename": f} for i, f in enumerate(files, start=1)],
            "signals": {"valid_citations": len(files), "invalid_citations": 0, "ungrounded_facts": 0, **signals}}


def test_refusal_path_distinguishes_gate_from_model() -> None:
    assert scoring.refusal_path(_answer("x", answer_type="not_found", gate=True)) == "gate"
    assert scoring.refusal_path(_answer("x", answer_type="not_found", gate=False)) == "model"
    assert scoring.refusal_path(_answer("x", answer_type="not_found")) == "model"
    assert scoring.refusal_path(_answer("x")) is None


def test_score_answer_passes_a_correct_cited_answer() -> None:
    expect = {"any_of": ["18 days"], "must_cite": "H.pdf", "gold_chunk": ["unused"]}
    result = scoring.score_answer(expect, _answer("You get 18 days [S1]."))
    assert result["passed"] and result["failed"] == []
    assert result["checks"]["not_found"] is True and result["checks"]["all_of"] is None


def test_score_answer_lists_every_failed_check() -> None:
    expect = {"any_of": ["18 days"], "must_cite": "H.pdf"}
    result = scoring.score_answer(expect, _answer("You get 20 days [S9].", files=("Other.pdf",),
                                                  invalid_citations=1))
    assert not result["passed"]
    assert set(result["failed"]) == {"any_of", "gold_citation", "citations_valid"}


def test_not_found_expectation_both_ways() -> None:
    refusal = _answer("I couldn't find this in your documents.", answer_type="not_found", files=())
    assert scoring.score_answer({"not_found": True}, refusal)["passed"]
    assert scoring.score_answer({"not_found": True}, _answer("Sure, 3 days."))["failed"] == ["not_found"]
    assert "not_found" in scoring.score_answer({"any_of": ["x"]}, refusal)["failed"]


def test_grounding_is_reported_but_gates_only_when_asked() -> None:
    answer = _answer("It is 42 days [S1].", ungrounded_facts=1)
    reported = scoring.score_answer({"any_of": ["42 days"]}, answer)
    assert reported["passed"] and reported["checks"]["grounded"] is False
    gated = scoring.score_answer({"any_of": ["42 days"], "grounded": True}, answer)
    assert gated["failed"] == ["grounded"]


def test_min_citations_for_starters() -> None:
    expect = {"not_found": False, "min_citations": 1, "grounded": True}
    assert scoring.score_answer(expect, _answer("Fact [S1]."))["passed"]
    uncited = _answer("Fact.", files=())
    assert scoring.score_answer(expect, uncited)["failed"] == ["min_citations"]


# --- retrieval --------------------------------------------------------------------------------------------------
def test_first_relevant_rank_is_one_based() -> None:
    contents = ["holiday policy", "Employees get 18 days of paid time off", "18 days of paid time off again"]
    assert scoring.first_relevant_rank(contents, ["18 days of paid time off"]) == 2
    assert scoring.first_relevant_rank(contents, ["nothing like this"]) is None
    assert scoring.first_relevant_rank([], ["x"]) is None


def test_hit_at_k_and_mrr_by_hand() -> None:
    ranks = [1, 3, None, 6]
    assert scoring.hit_at_k(ranks, 1) == 1
    assert scoring.hit_at_k(ranks, 5) == 2
    assert scoring.hit_at_k(ranks, 8) == 3
    assert scoring.mrr(ranks) == pytest.approx((1 + 1 / 3 + 0 + 1 / 6) / 4)
    assert scoring.mrr([]) is None


def test_retrieval_metrics_keeps_raw_counts() -> None:
    metrics = scoring.retrieval_metrics([1, None], ks=(1, 8))
    assert metrics["n"] == 2
    assert metrics["hit@1"] == {"hits": 1, "n": 2, "rate": 0.5}
    assert metrics["hit@8"]["hits"] == 1
    assert scoring.retrieval_metrics([], ks=(1,))["hit@1"]["rate"] is None


# --- refusals, reliability, stability, latency ------------------------------------------------------------------
def test_refusal_stats_precision_recall_by_path() -> None:
    records = [
        {"expected": True, "predicted": True, "path": "gate", "expected_path": "gate"},     # correct, gate
        {"expected": True, "predicted": True, "path": "model", "expected_path": "gate"},    # correct, other layer
        {"expected": True, "predicted": False, "path": None, "expected_path": "model"},     # answered: missed
        {"expected": False, "predicted": True, "path": "model", "expected_path": None},     # false refusal
        {"expected": False, "predicted": False, "path": None, "expected_path": None},
    ]
    stats = scoring.refusal_stats(records)
    assert (stats["should_refuse"], stats["tp"], stats["fp"], stats["fn"]) == (3, 2, 1, 1)
    assert stats["precision"] == pytest.approx(2 / 3)
    assert stats["recall"] == pytest.approx(2 / 3)
    gate, model = stats["by_path"]["gate"], stats["by_path"]["model"]
    assert (gate["refusals"], gate["correct"], gate["precision"], gate["recall"]) == (1, 1, 1.0, pytest.approx(1 / 3))
    assert (model["refusals"], model["correct"], model["false_refusals"]) == (2, 1, 1)
    assert gate["recall"] + model["recall"] == pytest.approx(stats["recall"])
    assert stats["expected_path"] == {"matched": 1, "n": 3}


def test_refusal_stats_with_nothing_to_refuse() -> None:
    stats = scoring.refusal_stats([{"expected": False, "predicted": False, "path": None}])
    assert stats["precision"] is None and stats["recall"] is None


def test_reliability_table_accuracy_per_badge() -> None:
    rows = scoring.reliability_table([
        {"confidence": "high", "passed": True}, {"confidence": "high", "passed": True},
        {"confidence": "medium", "passed": False}, {"confidence": None, "passed": True},
    ])
    by_badge = {r["confidence"]: r for r in rows}
    assert [r["confidence"] for r in rows] == ["high", "medium", "low", "none"]
    assert by_badge["high"]["accuracy"] == 1.0
    assert by_badge["medium"]["accuracy"] == 0.0
    assert by_badge["low"] == {"confidence": "low", "n": 0, "correct": 0, "accuracy": None}


def test_stability_is_pass_to_the_n() -> None:
    assert scoring.stability([True, True, True]) == {"passes": 3, "runs": 3, "stable": True, "flaky": False}
    assert scoring.stability([True, False, True])["flaky"] is True
    assert scoring.stability([False, False]) == {"passes": 0, "runs": 2, "stable": False, "flaky": False}
    assert scoring.stability([])["stable"] is False


def test_percentile_is_nearest_rank() -> None:
    values = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]
    assert scoring.percentile(values, 50) == 500
    assert scoring.percentile(values, 95) == 1000
    assert scoring.percentile([7], 95) == 7
    assert scoring.percentile([], 50) is None
    summary = scoring.latency_summary([100, None, 300])
    assert summary == {"n": 2, "p50": 100.0, "p95": 300.0, "avg": 200.0}
