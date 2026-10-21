"""The golden set is data, so it gets tests: schema validation, the case mix the design promises, and labels that
really occur in the committed corpus (a gold phrase that isn't in any document would silently zero a metric)."""

import copy
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
    ({"expect": {"any_of": ["x"], "gold_chunk": ["x"], "grounded": "yes"}}, "expect.grounded must be true or false"),
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
