"""The golden set is data, so it gets tests: schema validation, the case mix the design promises, and labels that
really occur in the committed corpus (a gold phrase that isn't in any document would silently zero a metric)."""

import copy
import re
from collections import Counter
from pathlib import Path

import pytest

from evals import golden, scoring
from evals.corpus import DEMO_DOCS, extract_pages, resolve

NIST = "NIST.AI.100-1.pdf"
HANDBOOK, BOARD = "Larkspur_Employee_Handbook_2026.pdf", "Q3_2026_Board_Update.pdf"
BRIGHTPATH, CORELINK = "Proposal_BrightPath.pdf", "Proposal_CoreLink.pdf"


@pytest.fixture(scope="module")
def raw() -> dict:
    return golden.load_raw(golden.GOLDEN_PATH)


@pytest.fixture(scope="module")
def gold() -> golden.Golden:
    return golden.load(starters=None)


@pytest.fixture(scope="module")
def corpus_text() -> dict[str, str]:
    """Extracted text of each committed corpus file, the way the ingestion pipeline reads it."""
    return {name: "\n".join(extract_pages(DEMO_DOCS / name)) for name in golden.load_raw(golden.GOLDEN_PATH)["corpus"]}


def test_golden_yaml_is_valid(raw: dict) -> None:
    assert golden.validate(raw) == []


def test_starters_file_is_valid_when_present() -> None:
    if not golden.STARTERS_PATH.exists():
        pytest.skip("evals/golden_starters.yaml not frozen yet (scripts/freeze_starters.py)")
    golden.load()          # raises on any problem, including ids that clash with golden.yaml


def test_case_mix_matches_the_design(gold: golden.Golden) -> None:
    """The original 40 synthetic cases (+3 public) stay exactly as they were, so the headline stays comparable with
    every earlier report; the hard set is counted separately (test_hard_set_*)."""
    synthetic = [c for c in gold.cases if not c.is_public_doc and not c.is_hard]
    assert len(synthetic) == 40
    assert Counter(c.category for c in synthetic) == {
        "factoid": 12, "multi_doc": 3, "unanswerable_offtopic": 8, "unanswerable_indomain": 7, "multiturn": 6,
        "injection": 2, "docx": 2}
    assert 8 <= sum(c.is_demo for c in gold.cases) <= 12
    assert len({c.id for c in gold.cases}) == len(gold.cases)


def test_required_cases_exist(gold: golden.Golden) -> None:
    by_id = {c.id: c for c in gold.cases}
    assert "LRK-4471" in by_id["f-lrk-4471"].question
    assert "9.2" in by_id["f-section-9-2"].question
    for table_case in ("f-q3-revenue", "f-q3-gross-margin"):
        assert "table" in by_id[table_case].tags
    assert by_id["m-proposals-cost-sla"].expect["all_of"][:2] == [
        ["600000", "600k", "600 thousand", "0.6m"], ["665000", "665k", "665 thousand"]]
    for probe in ("x-probe-a-termination", "x-probe-b-appendix-c"):
        assert by_id[probe].expect["forbid_start"] == ["approved"] and by_id[probe].is_demo


def test_public_doc_cases_target_the_nist_document(gold: golden.Golden) -> None:
    public = [c for c in gold.cases if c.is_public_doc]
    assert len(public) == 3
    for case in public:
        assert case.scope == [NIST] and case.expect["must_cite"] == NIST
    assert NIST not in gold.corpus          # gitignored and optional: never part of the base corpus


def test_every_answerable_case_gates_on_grounding(gold: golden.Golden) -> None:
    """The spec says an answer's numbers must be traceable to its sources. Without expect.grounded the check was only
    reported, so an ungrounded answer still counted as a pass. Refusals have nothing to ground."""
    answerable = [c for c in gold.cases if not c.not_found]
    assert len([c for c in answerable if not c.is_hard]) == 28
    assert [c.id for c in answerable if c.expect.get("grounded") is not True] == []
    assert not any("grounded" in c.expect for c in gold.cases if c.not_found)


def test_multiturn_cases_have_a_standalone_query_for_retrieval(gold: golden.Golden) -> None:
    for case in gold.cases:
        if case.turns and case.expect.get("gold_chunk"):
            assert case.retrieval_query, case.id
            assert case.search_query() == case.retrieval_query


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_every_gold_phrase_occurs_verbatim_in_the_scoped_documents(gold: golden.Golden,
                                                                   corpus_text: dict[str, str]) -> None:
    """Every gold_chunk of every case (hard set included) is the documents' own words: verbatim (only line breaks
    collapsed, case kept) in a scoped file, and found by the retrieval matcher. The matcher alone would accept a
    phrase with a filler word in it, which is not what "this is the passage" means."""
    for case in gold.cases:
        if case.is_public_doc or not case.expect.get("gold_chunk"):
            continue
        files = case.scope_files or gold.corpus
        for phrase in case.expect["gold_chunk"]:
            assert any(_flat(phrase) in _flat(corpus_text[f]) for f in files), (case.id, phrase)
            assert any(scoring.is_relevant(corpus_text[f], [phrase]) for f in files), (case.id, phrase)


def test_public_doc_gold_phrases_occur_verbatim_when_the_document_is_present(gold: golden.Golden) -> None:
    path = resolve(NIST)
    if path is None:
        pytest.skip(f"{NIST} is not in demo_docs/user/ (gitignored, added by hand)")
    text = _flat("\n".join(extract_pages(path)))
    for case in (c for c in gold.cases if c.is_public_doc):
        for phrase in case.expect["gold_chunk"]:
            assert _flat(phrase) in text, (case.id, phrase)


# Hard cases whose answer is computed: (inputs as (file, verbatim phrase)), the expected result, how it is derived.
DERIVED = {
    "h-pto-plus-sick-total": ([(HANDBOOK, "18 days of paid time off"), (HANDBOOK, "8 paid sick days per year")],
                              "26", 18 + 8),
    "h-revenue-yoy": ([(BOARD, "Revenue $4.2M $3.9M $3.1M")], "1.1m", round(4.2 - 3.1, 1)),
    "h-subscription-gap": ([(BRIGHTPATH, "$60,000 per year"), (CORELINK, "$95,000 per year")], "35000",
                           95_000 - 60_000),
}
# In-domain traps: what they ask for is nowhere in the corpus, but something close is (so a model may confuse them).
TRAPS = {
    "h-ceo-trap": (["chief executive", "ceo"], "Chief Operating Officer"),
    "h-revenue-target-trap": (["revenue target", "revenue goal", "2027 revenue", "fy2027 revenue"],
                              "FY2027 operating expense envelope"),
}


def test_hard_set_is_separate_and_covers_the_hard_kinds(gold: golden.Golden) -> None:
    hard = [c for c in gold.cases if c.is_hard]
    assert 10 <= len(hard) <= 14
    assert all(c.id.startswith("h-") and not c.is_demo and not c.is_public_doc for c in hard)
    kinds = Counter(c.category for c in hard)
    assert kinds["multiturn"] >= 1 and kinds["multi_doc"] >= 2 and kinds["unanswerable_indomain"] >= 2
    assert set(DERIVED) | set(TRAPS) <= {c.id for c in hard}


def test_hard_derived_answers_follow_from_the_corpus(gold: golden.Golden, corpus_text: dict[str, str]) -> None:
    by_id = {c.id: c for c in gold.cases}
    for cid, (inputs, label, value) in DERIVED.items():
        for name, phrase in inputs:
            assert _flat(phrase) in _flat(corpus_text[name]), (cid, phrase)
        accepted = by_id[cid].expect.get("any_of") or []
        assert label in accepted, cid
        assert scoring.contains(f"{value:,}" if isinstance(value, int) else f"{value}m", label), (cid, value)


def test_hard_traps_are_not_in_the_corpus_but_look_close(gold: golden.Golden, corpus_text: dict[str, str]) -> None:
    by_id = {c.id: c for c in gold.cases}
    everything = "\n".join(corpus_text.values())
    for cid, (absent, near_miss) in TRAPS.items():
        assert by_id[cid].not_found and by_id[cid].category == "unanswerable_indomain", cid
        assert not scoring.matches_any(everything, absent, assertive=False), cid
        assert _flat(near_miss) in _flat(everything), cid


# Cross-document hard cases: each input is stated in exactly one of the two scoped documents, so neither document
# answers the question alone (the board update restates CoreLink's 99.9% as its own target, so that comparison could
# be answered from one sentence; BrightPath's 99.5% appears only in its proposal).
CROSS_DOC = {
    "h-availability-vs-brightpath": {BOARD: "99.62%", BRIGHTPATH: "99.5%"},
    "h-subscription-gap": {BRIGHTPATH: "$60,000 per year", CORELINK: "$95,000 per year"},
}


def test_hard_cross_document_cases_need_both_documents(gold: golden.Golden, corpus_text: dict[str, str]) -> None:
    by_id = {c.id: c for c in gold.cases}
    for cid, facts in CROSS_DOC.items():
        assert sorted(by_id[cid].scope_files) == sorted(facts), cid
        for name, fact in facts.items():
            other = next(f for f in facts if f != name)
            assert fact in _flat(corpus_text[name]) and fact not in _flat(corpus_text[other]), (cid, fact)


def _score(case: golden.Case, content: str) -> dict:
    """A cited, grounded answer with this text, so only the content checks can fail."""
    files = case.expect["must_cite"]
    files = [files] if isinstance(files, str) else files
    answer = {"content": content, "answer_type": "answered",
              "citations": [{"ref": f"S{i}", "filename": f} for i, f in enumerate(files, start=1)],
              "signals": {"valid_citations": len(files), "invalid_citations": 0, "ungrounded_facts": 0, "gate": False}}
    return scoring.score_answer(case.expect, answer)


# (case, answer, passes): how the hard set's labels read real-looking answers. A "no" must be about the stipend and
# must not be a hedge; a comparison must use both documents' numbers and go the right way; natural bounds and
# spellings must pass. Rows marked "limitation" pass or fail wrongly and stay visible: the judge is for those.
HARD_ANSWERS = [
    # The short "no"s: "stipend" right after the negation is still the topic (expect.mentions, not an any_of phrase)
    ("h-contractor-stipend", "No. Contractors don't get the stipend. [S1]", True),
    ("h-contractor-stipend", "No, contractors do not receive the stipend. [S1]", True),
    ("h-contractor-stipend", "No. Contractors can't receive the stipend. [S1]", True),
    ("h-contractor-stipend", "No. Only employees receive the remote work stipend. [S1]", True),
    ("h-contractor-stipend", "No. Only employees are eligible for the remote work stipend. [S1]", True),
    ("h-contractor-stipend", "No. The remote work stipend is for employees, not contractors. [S1]", True),
    ("h-contractor-stipend", "No. Contractors aren't covered by the remote work stipend. [S1]", True),
    ("h-contractor-stipend", "No; the stipend is an employee-only benefit. [S1]", True),
    ("h-contractor-stipend", "No. The stipend is not available to contractors. [S1]", True),
    ("h-contractor-stipend", "No. Contractors receive no stipend; it is for employees only. [S1]", True),
    ("h-contractor-stipend", "Yes, contractors receive the stipend; they are excluded only from parental leave. [S1]",
     False),                                                                 # starts "Yes"
    ("h-contractor-stipend", "The stipend section doesn't mention contractors, so they may be excluded. [S1]", False),
    ("h-contractor-stipend", "It's possible contractors are excluded from the stipend, but the handbook doesn't say. "
     "[S1]", False),
    # why the labels are "employees, not contractors", "not to contractors" and "aren't covered", not "not
    # contractors" and "not covered"
    ("h-contractor-stipend", "The stipend section does not mention contractors. [S1]", False),          # a hedge
    ("h-contractor-stipend", "Contractor eligibility for the stipend is not covered in the handbook. [S1]", False),
    ("h-contractor-stipend", "Not only employees receive the stipend; contractors do too. [S1]", False),
    ("h-contractor-stipend", "No, contractors are not eligible for parental leave. [S1]", False),  # not the stipend
    # limitation: a correct answer fails: "doesn't mention" negates the rest of the clause, across the "and"
    ("h-contractor-stipend", "No. The stipend section doesn't mention contractors and section 1.2 says the handbook's "
     "benefits apply only to employees. [S1]", False),
    # limitation: "nothing ... says" is a hedge, but "says" is 3 words after "nothing" (the clause rule reaches 2)
    ("h-contractor-stipend", "Nothing in the handbook says contractors are excluded from the stipend. [S1]", True),
    # limitation: "stating" is not a reporting verb the rule knows, and "couldn't find" is not forbidden (above)
    ("h-contractor-stipend", "I couldn't find anything stating that contractors are excluded from the stipend. [S1]",
     True),
    # limitation: "nobody" is not a negation, and "contractors receive" is not forbidden (above)
    ("h-contractor-stipend", "Nobody is excluded from the stipend; contractors receive it too. [S1]", True),
    # limitation: a hedge that fits "not to contractors" with a filler
    ("h-contractor-stipend", "The stipend section does not refer to contractors. [S1]", True),
    ("h-contractor-stipend", "No. Section 1.2 says that unless a section says otherwise, the benefits in this handbook "
     "apply only to employees, and the remote work stipend section does not extend it to contractors. [S1]", True),
    ("h-contractor-stipend", "No, contractors are not eligible for the remote work stipend; it is an employee "
     "benefit. [S1]", True),
    ("h-contractor-stipend", "No. Contractors don't receive the remote work stipend: section 1.2 limits the "
     "handbook's benefits to employees. [S1]", True),
    ("h-contractor-stipend", "No, contractors are excluded from the remote work stipend. [S1]", True),
    ("h-contractor-stipend", "No. Contractors aren't entitled to the remote work stipend; it is an employee "
     "benefit. [S1]", True),
    ("h-contractor-stipend", "No. A contractor isn't eligible for the stipend. [S1]", True),
    ("h-contractor-stipend", "No. Contractors cannot receive the remote work stipend: handbook benefits are limited "
     "to employees. [S1]", True),
    ("h-contractor-stipend", "No. Contractors won't get the remote work stipend. [S1]", True),
    ("h-contractor-stipend", "No. The remote work stipend is for employees, and contractors are not employees of "
     "Larkspur. [S1]", True),
    ("h-contractor-stipend", "**No**\nContractors don't receive the remote work stipend. [S1]", True),
    ("h-contractor-stipend", "No. The stipend is only available to employees. [S1]", True),
    ("h-contractor-stipend", "The handbook does not say whether contractors get the stipend; it only says contractors "
     "are not eligible for parental leave. [S1]", False),                     # a hedge, and another benefit's "no"
    ("h-contractor-stipend", "Information on contractor eligibility for the stipend is not available in the "
     "handbook. [S1]", False),                                               # a hedge
    ("h-contractor-stipend", "Contractors may not be eligible; the handbook is unclear. [S1]", False),
    ("h-contractor-stipend", "Contractors may not be eligible for the stipend. [S1]", False),        # a hedge
    ("h-contractor-stipend", "The documents are silent on whether contractors receive the stipend; they do not get "
     "parental leave. [S1]", False),
    ("h-contractor-stipend", "I'm not certain. The stipend section only mentions employees; contractors may not "
     "qualify. [S1]", False),
    ("h-contractor-stipend", "The handbook doesn't mention whether contractors get the stipend; contractors don't get "
     "parental leave. [S1]", False),
    ("h-contractor-stipend", "Yes. The remote work stipend is available not only to employees but also to "
     "contractors, at $500 per year. [S1]", False),                          # the opposite answer
    # limitation: a hedge worded as an accepted "no" plus a filler passes
    ("h-contractor-stipend", "The stipend section does not explicitly apply to contractors. [S1]", True),
    # limitation: who the "no" is about is not checked (here: parental leave, not the stipend)
    ("h-contractor-stipend", "Contractors are excluded from parental leave, and the stipend section doesn't mention "
     "them. [S1]", True),
    ("h-availability-vs-brightpath", "Yes. Larkspur's Q3 2026 platform availability was 99.62% [S1], above the 99.5% "
     "monthly uptime BrightPath commits to [S2].", True),
    ("h-availability-vs-brightpath", "Yes: 99.62% exceeds BrightPath's 99.5% commitment, although it was below "
     "Larkspur's internal 99.9% target. [S1][S2]", True),
    ("h-availability-vs-brightpath", "Numerically, yes: Larkspur's Q3 2026 platform availability was 99.62%, above "
     "BrightPath's 99.5% monthly uptime commitment; however, a quarterly figure does not establish whether Larkspur "
     "met that threshold in each month. [S1][S5]", True),                   # the 2026-10-21 live answer
    ("h-availability-vs-brightpath", "Larkspur's Q3 availability of 99.62% beats the 99.5% BrightPath commits to. "
     "[S1][S2]", True),
    ("h-availability-vs-brightpath", "No. Platform availability was 99.62%, below the internal target of 99.9%. "
     "[S1]", False),                                                         # one document, the wrong comparison
    ("h-availability-vs-brightpath", "No. Larkspur's 99.62% did not exceed BrightPath's 99.5% guarantee. [S1][S2]",
     False),
    ("h-availability-vs-brightpath", "Availability was 99.62% and BrightPath commits to 99.5%; whether that is "
     "enough is unclear. [S1][S2]", False),
    # the monthly reading answers another question: a "no" fails even when it states "above" for the quarter
    ("h-availability-vs-brightpath", "No. Q3 availability averaged 99.62%, above 99.5% for the quarter, but the "
     "9-hour August outage put August near 98.8%, below BrightPath's 99.5% monthly level. [S1][S2]", False),
    ("h-stolen-laptop", "Report it to security@larkspur.example immediately, and in no case later than 24 hours. [S1]",
     True),
    ("h-stolen-laptop", "Report it without delay (within 24 hours). [S1]", True),
    ("h-stolen-laptop", "Report it to security within 24h. [S1]", True),
    ("h-stolen-laptop", "Not within 24 hours; you have 7 days to report it. [S1]", False),
    ("h-stolen-laptop", "Report it immediately and without exception within 24 hours. [S1]", True),
    ("h-stolen-laptop", "Report it to security without fail within 24 hours. [S1]", True),
    ("h-stolen-laptop", "Don't wait past 24 hours: report it immediately. [S1]", True),
    ("h-stolen-laptop", "Immediately. If you fail to report within 24 hours you breach policy. [S1]", True),
    # limitation: the "not" is 6 tokens before "24 hours", outside NEGATION_WINDOW
    ("h-stolen-laptop", "It does not have to be reported within 24 hours; report it at your next check-in. [S1]",
     True),
    ("h-work-abroad", "You can work from abroad, but you can't go over 30 days in any 12-month period. [S1]", True),
    ("h-work-abroad", "You can't stay beyond 30 days. [S1]", True),
    ("h-work-abroad", "You may not stay over 30 days. [S1]", True),
    ("h-work-abroad", "There's a 30-day limit in any 12-month period. [S1]", True),
    ("h-unused-pto-cash", "No. Unused PTO isn't paid out; it is lost on December 31. [S1]", True),
    ("h-unused-pto-cash", "No. They are lost on December 31. [S1]", True),
    ("h-unused-pto-cash", "No, you won't get cash for them. [S1]", True),
    ("h-unused-pto-cash", "Yes, unused days are paid out at year end rather than forfeited. [S1]", False),
    ("h-unused-pto-cash", "Yes, unused days are paid out unless they are forfeited. [S1]", False),     # starts "Yes"
    ("h-unused-pto-cash", "No. Days you fail to use are forfeited. [S1]", True),
    ("h-volunteer-day-carryover", "No, volunteer days don't roll over. [S1]", True),
    ("h-volunteer-day-carryover", "No, the volunteer day does not carry forward. [S1]", True),
    ("h-volunteer-day-carryover", "No. An unused volunteer day expires at the end of the year. [S1]", True),
    ("h-volunteer-day-carryover", "No. The volunteer day can't be carried into the next year. [S1]", True),
    ("h-volunteer-day-carryover", "Yes, if your manager agrees; otherwise it doesn't carry over. [S1]", False),
    ("h-vacation-paraphrase", "An 18-day PTO allowance per calendar year. [S1]", True),
    ("h-vacation-paraphrase", "Full-timers get 18 per year, before any seniority top-up. [S1]", True),
    ("h-vacation-paraphrase", "20 days: 18 days of PTO plus 2 for seniority. [S1]", False),   # ignores "ignoring"
    # limitation: the same wrong total, not at the start, passes (it contains "18 days")
    ("h-vacation-paraphrase", "A full-timer gets 20 days: 18 days of PTO plus 2 for seniority. [S1]", True),
    ("h-pto-plus-sick-total", "Twenty-six days in total: 18 PTO days plus 8 sick days. [S1]", True),
    ("h-pto-plus-sick-total", "28 days in total (18 + 2 + 8); without the service increase it would be 26. [S1]",
     False),                                                                 # a new hire has no service increase
    # limitation: the same wrong total, not at the start, passes (it contains "26")
    ("h-pto-plus-sick-total", "A new full-timer gets 28 days in total (18 + 2 + 8); without the service increase it "
     "would be 26. [S1]", True),
    ("h-revenue-yoy", "Revenue grew by $1.10M year over year. [S1]", True),
    ("h-subscription-gap", "CoreLink would cost 35 000 USD more per year. [S1][S2]", True),
    ("h-subscription-gap", "BrightPath is $35,000 cheaper per year than CoreLink. [S1][S2]", True),
    ("h-subscription-gap", "BrightPath would cost $35,000 more per year than CoreLink. [S1][S2]", False),  # backwards
    ("h-subscription-gap", "CoreLink would cost $35,000 less than BrightPath. [S1][S2]", False),             # backwards
    ("h-subscription-gap", "CoreLink is $35,000 cheaper than BrightPath per year. [S1][S2]", False),        # backwards
    # the one-time fees go the other way: a correct answer may compare them too
    ("h-subscription-gap", "CoreLink's implementation is cheaper than BrightPath, but its subscription is $35,000 "
     "higher per year ($95,000 vs $60,000). [S1][S2]", True),
    ("h-subscription-gap", "CoreLink costs $35,000 more per year ($95,000 vs $60,000). Its one-time fee is lower: "
     "$40,000 less than BrightPath. [S1][S2]", True),
    ("h-subscription-gap", "BrightPath charges $40,000 more than CoreLink for implementation, but CoreLink's "
     "subscription costs $35,000 more per year. [S1][S2]", True),
    # limitation: a backwards gap without "than" passes (the forbidden phrases need the number and the other vendor)
    ("h-subscription-gap", "CoreLink is $35,000 cheaper per year. [S1][S2]", True),
    ("h-subscription-gap", "BrightPath would cost $35,000 more per year. [S1][S2]", True),
    ("h-subscription-gap", "Larkspur would save $35,000 a year by choosing CoreLink. [S1][S2]", True),
    ("h-pronoun-contract-term", "A 3-yr term. [S1]", True),
]


@pytest.mark.parametrize(("case_id", "content", "passes"), HARD_ANSWERS,
                         ids=[f"{c}-{i}" for i, (c, _, _) in enumerate(HARD_ANSWERS)])
def test_hard_labels_read_answers_as_intended(gold: golden.Golden, case_id: str, content: str, passes: bool) -> None:
    case = next(c for c in gold.cases if c.id == case_id)
    result = _score(case, content)
    assert result["passed"] is passes, (result["failed"], result["evidence"])


# Hard cases that need two passages at once: retrieval reports, besides the rank of the first gold passage, the rank
# by which every one of them was retrieved (scoring.all_relevant_rank), since one passage alone cannot answer them.
MULTI_PASSAGE = {"h-pto-plus-sick-total", "h-subscription-gap", "h-availability-vs-brightpath"}


def test_multi_passage_hard_cases_measure_every_passage(gold: golden.Golden) -> None:
    flagged = {c.id for c in gold.cases if c.expect.get("gold_chunk_all")}
    assert flagged == MULTI_PASSAGE
    by_id = {c.id: c for c in gold.cases}
    for cid in MULTI_PASSAGE:
        assert by_id[cid].is_hard and len(by_id[cid].expect["gold_chunk"]) >= 2, cid
    assert set(CROSS_DOC) <= MULTI_PASSAGE


def test_the_availability_question_asks_for_the_comparison_the_corpus_settles(gold: golden.Golden,
                                                                               corpus_text: dict[str, str]) -> None:
    """Regression: it asked whether Q3 availability reached "the monthly uptime that BrightPath commits to". Read
    monthly, the corpus says no: August had a 9-hour outage, so August was at most (744 - 9) / 744 = 98.8% < 99.5%.
    Only the quarter's 99.62% clears 99.5%, so the question asks about that figure, not about a month."""
    case = next(c for c in gold.cases if c.id == "h-availability-vs-brightpath")
    assert "monthly" not in case.question and "Q3 2026" in case.question
    board, brightpath = _flat(corpus_text[BOARD]), _flat(corpus_text[BRIGHTPATH])
    quarter = float(re.search(r"availability for the quarter was (\d+\.\d+)%", board).group(1))
    commitment = float(re.search(r"BrightPath commits to (\d+\.\d+)% monthly uptime", brightpath).group(1))
    outage_hours = int(re.search(r"a (\d+)-hour outage", board).group(1))
    august = (31 * 24 - outage_hours) / (31 * 24) * 100
    assert (quarter, commitment, round(august, 1)) == (99.62, 99.5, 98.8)       # the numbers the docs quote
    assert quarter > commitment > august and case.expect["forbid_start"] == ["no"]


def test_answer_labels_never_contradict_the_corpus(gold: golden.Golden, corpus_text: dict[str, str]) -> None:
    """Single-document factoids: at least one accepted answer phrase is literally in the cited document (multi-turn and
    multi-document cases may need arithmetic, e.g. 18 + 2 = 20, so they are exempt, as are the hard set's computed
    answers, which test_hard_derived_answers_follow_from_the_corpus checks instead)."""
    for case in gold.cases:
        if case.category not in ("factoid", "docx") or not case.expect.get("any_of") or case.id in DERIVED:
            continue
        text = corpus_text[case.expect["must_cite"]]
        assert scoring.matches_any(text, case.expect["any_of"]), case.id


# --- validator catches mistakes -------------------------------------------------------------------------------------
def _with_case(raw: dict, **changes: object) -> dict:
    bad = copy.deepcopy(raw)
    case = next(c for c in bad["cases"] if c["id"] == "f-pto-days")
    for key, value in changes.items():
        if value is None:
            case.pop(key, None)
        else:
            case[key] = value
    return bad


@pytest.mark.parametrize(("changes", "message"), [
    ({"tags": ["factoid", "demoo"]}, "unknown tag 'demoo'"),
    ({"tags": ["demo"]}, "needs exactly one category tag"),
    ({"tags": ["factoid", "docx"]}, "needs exactly one category tag"),
    ({"tags": ["factoid", "demo", "hard"]}, "cannot be both demo and hard"),
    ({"expect": {"any_of": ["18 days"]}}, "need expect.gold_chunk"),
    ({"expect": {"gold_chunk": ["x"]}}, "need expect.any_of or expect.all_of"),
    ({"expect": {"not_found": True, "any_of": ["x"]}}, "only unanswerable_*"),
    ({"expect": {"any_of": ["x"], "gold_chunk": ["x"], "must_cit": "a"}}, "unknown expect key 'must_cit'"),
    ({"expect": {"any_of": "18 days", "gold_chunk": ["x"]}}, "expect.any_of must be a non-empty list"),
    ({"expect": {"any_of": ["x"], "gold_chunk": ["x"], "mentions": "stipend"}},
     "expect.mentions must be a non-empty list"),
    ({"expect": {"any_of": ["x"], "gold_chunk": ["x"], "mentions": [" "]}}, "expect.mentions must be a non-empty list"),
    ({"expect": {"any_of": ["x"], "gold_chunk": ["x"], "grounded": "yes"}}, "expect.grounded must be true or false"),
    ({"expect": {"any_of": ["x"], "gold_chunk": ["x"], "gold_chunk_all": "yes"}},
     "expect.gold_chunk_all must be true or false"),
    ({"expect": {"any_of": ["x"], "gold_chunk": ["x"], "gold_chunk_all": True}},
     "expect.gold_chunk_all needs at least two gold_chunk phrases"),
    ({"scope": ["Missing.pdf"]}, "scope file 'Missing.pdf' is not in the corpus"),
    ({"scope": ["Q3_2026_Board_Update.pdf"]}, "must_cite 'Larkspur_Employee_Handbook_2026.pdf' is not in the case"),
    ({"tags": ["multiturn"]}, "multiturn cases need prior turns"),
    ({"question": None}, "missing required key 'question'"),
    ({"colour": "red"}, "unknown key 'colour'"),
    ({"id": "Bad_ID"}, "id must be lowercase"),
])
def test_validate_reports_problems(raw: dict, changes: dict, message: str) -> None:
    problems = golden.validate(_with_case(raw, **changes))
    assert any(message in p for p in problems), problems


def test_validate_rejects_duplicate_ids_and_unanswerable_without_not_found(raw: dict) -> None:
    bad = copy.deepcopy(raw)
    bad["cases"].append(copy.deepcopy(bad["cases"][0]))
    assert any("duplicate id" in p for p in golden.validate(bad))
    bad = copy.deepcopy(raw)
    next(c for c in bad["cases"] if c["id"] == "o-fifa")["expect"] = {"forbid": ["france"]}
    assert any("need expect.not_found: true" in p for p in golden.validate(bad))


def test_load_raises_with_every_problem(tmp_path: Path, raw: dict) -> None:
    path = tmp_path / "golden.yaml"
    path.write_text("corpus: []\ncases: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="'corpus' must list"):
        golden.load(path, starters=None)
