"""Citation parsing and sentence splitting edge cases found by review: abbreviations whose period does not end a
sentence, and citation markers the model writes as ranges or in lower case."""

import pytest

from app.config import settings
from app.rag.citations import (
    Source,
    check_answer,
    extract_facts,
    parse_refs,
    split_sentences,
    strip_citations,
)
from app.rag.confidence import confidence

PTO = {"S1": Source("Handbook (p. 4)", "Employees get up to 18 days of PTO per year; contractors get 10 days.")}


def _badge(answer: str, sources: dict) -> str:
    """The badge chat would show for `answer`, with retrieval signals that never cap it below high."""
    check = check_answer(answer, sources)
    return confidence(answer_type=check.answer_type, valid_citations=check.valid_citations,
                      invalid_citations=check.invalid_citations, uncited_claims=check.uncited_claims,
                      any_weak_support=any(c.weak_support for c in check.citations),
                      top_similarity=settings.TAU_HIGH + 0.1, lexical_match=True)


# --- sentence splitting ----------------------------------------------------------------------------------------
def test_vs_abbreviation_does_not_force_a_low_badge() -> None:
    answer = "Employees get up to 18 days vs. 10 days for contractors [S1]."
    assert _badge(answer, PTO) == "high"        # was low: "up to 18 days vs." read as an uncited claim
    check = check_answer(answer, PTO)
    assert check.uncited_claims == 0 and check.ungrounded_facts == 0
    assert split_sentences(answer) == [answer]


@pytest.mark.parametrize("sentence", [
    "Some leave types, e.g. sick leave over 18 days, need a note [S1].",
    "E.g. 18 days of PTO per year [S1].",
    "The cap, i.e. 18 days, applies per year [S1].",
    "Leave is approx. 18 days per year [S1].",
    "PTO, sick leave, etc. add up to 18 days [S1].",
    "Clause No. 18 grants the leave [S1].",
    "Larkspur Inc. grants 18 days [S1].",
    "Larkspur Ltd. grants 18 days [S1].",
    "Ask Dr. Rao, Mr. Iyer or Ms. Lee about the 18 days [S1].",
    "Contractors get 10 days (cf. 18 days for employees) [S1].",
    "Employees get 18 days Vs. 10 days for contractors [S1].",
])
def test_common_abbreviations_are_not_sentence_ends(sentence: str) -> None:
    assert split_sentences(sentence) == [sentence]
    assert check_answer(sentence, PTO).uncited_claims == 0


@pytest.mark.parametrize(("text", "sentences"), [
    # A citation after the period belongs to the sentence before it.
    ("Employees get 18 days. [S1] Contractors get 10 days [S2].",
     ["Employees get 18 days. [S1]", "Contractors get 10 days [S2]."]),
    # A citation group followed by a capitalised word ends a sentence, whatever the spacing.
    ("Employees get 18 days [S1]  Contractors get 10 days [S2].",
     ["Employees get 18 days [S1]", "Contractors get 10 days [S2]."]),
    ("Is it 18 days? Yes [S1]! Contractors get 10 days [S2].",
     ["Is it 18 days?", "Yes [S1]!", "Contractors get 10 days [S2]."]),
    # A word that merely ends in an abbreviation's letters is a real sentence end.
    ("We stock 18 items. Contractors get 10 days [S1].", ["We stock 18 items.", "Contractors get 10 days [S1]."]),
    ("They met at the casino. Contractors get 10 days [S1].",
     ["They met at the casino.", "Contractors get 10 days [S1]."]),
    ("- Employees: 18 days [S1]\n- Contractors: 10 days [S1]",
     ["- Employees: 18 days [S1]", "- Contractors: 10 days [S1]"]),
])
def test_real_sentence_boundaries_still_split(text: str, sentences: list[str]) -> None:
    assert split_sentences(text) == sentences


# --- citation markers ------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("text", "refs"), [
    ("A [S1-3].", ["S1", "S2", "S3"]),
    ("A [S1-S3].", ["S1", "S2", "S3"]),
    ("A [S1–S3].", ["S1", "S2", "S3"]),            # en dash
    ("A [S2 – 4].", ["S2", "S3", "S4"]),
    ("A [s1]. B [s2, s4].", ["S1", "S2", "S4"]),
    ("A [S1, S3-4; 6].", ["S1", "S3", "S4", "S6"]),
    ("A [S2-2].", ["S2"]),
])
def test_ranges_and_lowercase_markers_are_parsed(text: str, refs: list[str]) -> None:
    assert parse_refs(text) == refs


@pytest.mark.parametrize("dash", ["\u2010", "\u2011", "\u2212"], ids=["hyphen", "non-breaking-hyphen", "minus"])
def test_ranges_written_with_any_hyphen_are_parsed_and_stripped(dash: str) -> None:
    """GPT-5-family models often write U+2011 (the date check already accepts it). Such a range was neither parsed
    (the answer lost its citations: ungrounded, a low badge) nor stripped from history."""
    assert parse_refs(f"A [S1{dash}3].") == ["S1", "S2", "S3"]
    assert parse_refs(f"A [S1 {dash} S2; 4].") == ["S1", "S2", "S4"]
    assert strip_citations(f"Employees get 18 days [S1{dash}2].") == "Employees get 18 days."
    sources = {"S1": PTO["S1"], "S2": Source("Handbook (p. 5)", "Employees get up to 18 days of PTO per year.")}
    answer = f"Employees get up to 18 days of PTO per year [S1{dash}2]."
    check = check_answer(answer, sources)
    assert check.answer_type == "answered" and check.uncited_claims == 0
    assert [c.ref for c in check.citations] == ["S1", "S2"]
    assert _badge(answer, sources) == "high"                  # was low: "ungrounded", no citation found


def test_absurd_or_reversed_ranges_keep_only_their_ends() -> None:
    assert parse_refs("A [S1-999].") == ["S1", "S999"]        # never expands into hundreds of refs
    assert parse_refs("A [S3-S1].") == ["S3", "S1"]


def test_range_markers_are_stripped_and_never_read_as_facts() -> None:
    assert strip_citations("Employees get 18 days [S1–S3]. Contractors none [s4].") == \
        "Employees get 18 days. Contractors none."
    assert extract_facts("See the policy [S12-14].") == set()


def test_a_range_citation_grounds_its_sentence() -> None:
    sources = {"S1": Source("Handbook (p. 1)", "Leave policy overview."), "S2": PTO["S1"]}
    for answer in ("Employees get 18 days [S1-2].", "Employees get 18 days [S1–S2].", "They get 18 days [s2]."):
        check = check_answer(answer, sources)
        assert check.answer_type == "answered" and check.uncited_claims == 0 and check.ungrounded_facts == 0
        assert "S2" in [c.ref for c in check.citations]


def test_out_of_range_ids_are_still_invalid() -> None:
    sources = {f"S{i}": Source(f"Doc (p. {i})", "Employees get 18 days.") for i in range(1, 9)}
    check = check_answer("Employees get 18 days [S0]. Employees get 18 days [S7-9]. Again 18 days [s9].", sources)
    assert [c.ref for c in check.citations] == ["S7", "S8"]
    assert check.invalid_refs == ["S0", "S9"]
