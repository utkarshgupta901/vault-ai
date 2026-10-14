"""Dates the model was SHOWN count as grounded where the answer presents them as such: each cited source's document
date (the <source date="..."> attribute) after "dated", and today's date (the "Today's date" line) after "today" or
"as of". Regression for the demo case m-stipend-conflict, whose correct answers quoted both (as answer.v2 rule 5
asks) and came back "low" in 3/3 runs of evals/reports/20261013T170823Z.json because the checker built each source's
facts from header + chunk text only."""

from datetime import date

import pytest

from app.ai.prompts import answer as answer_prompt
from app.config import Settings, settings
from app.rag.citations import Source, check_answer, date_mentions, without_shown_dates
from app.rag.confidence import confidence
from app.rag.prompting import build_answer_prompt

TODAY = date(2026, 10, 13)              # the day the eval ran (the container's date.today(), UTC)
HANDBOOK_DATE = date(2026, 1, 15)
BOARD_DATE = date(2026, 10, 2)

# The two chunks those answers cited, verbatim from the eval user's vault (chunk ids 137 and 147; synthetic corpus).
HANDBOOK_P7 = """6. Remote and Hybrid Work
6.1 Eligibility
Most roles at Larkspur are hybrid. With manager approval, employees may work remotely up to 3 days
per week. Fully remote arrangements require approval from the department's vice president.
6.2 Remote work stipend
Larkspur provides a remote work stipend of $500 per year to help employees cover internet, furniture
and other home-office costs. The stipend is paid in the January payroll and is prorated for employees
who join during the year.
6.3 Equipment
Larkspur provides a laptop, a monitor and a headset. Laptops are refreshed every 36 months.
Equipment remains Larkspur property and must be returned within 10 days of an employee's last day.
6.4 Working from another country
Employees may work from another country for up to 30 days in any 12-month period with approval from
People Operations, so that tax and immigration rules can be checked in advance.
6.5 Coworking allowance
Employees who live more than 50 miles from a Larkspur office may expense a coworking membership
of up to $150 per month. The coworking allowance is separate from the remote work stipend.
Larkspur Analytics | Employee Handbook 2026 | Page 7 of 12"""
BOARD_P5 = """5. People and Policies
Headcount at quarter end was 84, up from 77 at the end of June. Voluntary attrition was 4% for the
quarter, and there are 12 open roles, mostly in engineering.
Remote work stipend
Following the spring employee survey, the board approved an increase in the remote work stipend from
$500 to $750 per year, effective October 1, 2026. People Operations will update the employee
handbook in its next edition.
Engagement
The employee engagement score rose to 78, up 4 points from the spring survey. Managers will share
team-level results and action plans by November 15.
Larkspur Analytics | Q3 2026 Board Update | Confidential | Page 5 of 6"""

STIPEND_SOURCES = {
    "S1": Source("Larkspur Employee Handbook 2026 (p. 7)", HANDBOOK_P7, HANDBOOK_DATE),
    "S2": Source("Q3 2026 Board Update (p. 5)", BOARD_P5, BOARD_DATE),
}
UNDATED_SOURCES = {ref: Source(s.header, s.content) for ref, s in STIPEND_SOURCES.items()}   # the old view

# The three answers of m-stipend-conflict in evals/reports/20261013T170823Z.json, verbatim, with the
# ungrounded_facts the old checker gave them (3, 2, 1) and the shown dates that caused it.
STIPEND_ANSWERS = [
    pytest.param(
        "The remote work stipend is **$750 per year**, effective October 1, 2026; yes, it increased from "
        "**$500 per year**. [S2]\n\n- The handbook dated 2026-01-15 lists $500 per year, paid in the January payroll "
        "and prorated for employees who join during the year, to cover internet, furniture and other home-office "
        "costs. [S1]\n- The more recent board update dated 2026-10-02 records the approved increase to $750 per year "
        "and says People Operations will update the handbook in its next edition; the increase is effective as of "
        "today, 2026-10-13. [S2]\n- The coworking allowance is separate from the remote work stipend. [S1]",
        3, id="run1"),
    pytest.param(
        "The current remote work stipend is **$750 per year**, effective **October 1, 2026**; the board approved an "
        "increase from **$500 per year**. [S2]\n\n- The handbook dated **2026-01-15** still lists **$500 per year**, "
        "paid in the January payroll and prorated for employees who join during the year. [S1]\n- The more recent "
        "board update, dated **2026-10-02**, states that People Operations will update the handbook in its next "
        "edition to reflect the increase. [S2]",
        2, id="run2"),
    pytest.param(
        "The remote work stipend is **$750 per year**, effective October 1, 2026; the board approved an increase from "
        "**$500 per year**. [S2]\n\n- The handbook dated 2026-01-15 still lists $500 per year, but the more recent "
        "board update dated 2026-10-02 reports the increase, which is effective as of today, 2026-10-13. [S1][S2]\n"
        "- The handbook says the stipend helps cover internet, furniture and other home-office costs, is paid in the "
        "January payroll, and is prorated for employees who join during the year. [S1]\n- People Operations will "
        "update the handbook in its next edition. [S2]",
        1, id="run3"),
]


def _badge(check) -> str:
    """The badge the API would give: the eval's retrieval signals for this question (top similarity 0.4931, keyword
    match on the cited chunks), so 'medium' is the ceiling (0.4931 < TAU_HIGH)."""
    return confidence(answer_type=check.answer_type, valid_citations=check.valid_citations,
                      invalid_citations=check.invalid_citations, uncited_claims=check.uncited_claims,
                      any_weak_support=any(c.weak_support for c in check.citations), top_similarity=0.4931,
                      lexical_match=True)


# --- the real answers --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("answer", "old_ungrounded"), STIPEND_ANSWERS)
def test_real_stipend_conflict_answers_are_grounded(answer: str, old_ungrounded: int) -> None:
    check = check_answer(answer, STIPEND_SOURCES, today=TODAY)
    assert check.ungrounded_facts == 0 and check.uncited_claims == 0 and check.invalid_citations == 0
    assert [c.ref for c in check.citations] == ["S2", "S1"]
    assert not any(c.weak_support for c in check.citations)
    assert _badge(check) == "medium"
    # Sources without dates (the old checker's view, and any SourceText without document_date) behave as before.
    old = check_answer(answer, UNDATED_SOURCES)
    assert old.ungrounded_facts == old_ungrounded and _badge(old) == "low"


# --- each shown date, in the forms the model writes ------------------------------------------------------------
HANDBOOK = Source("Handbook (p. 7)", "Remote work stipend: $500 per year, paid in the January payroll.", HANDBOOK_DATE)


@pytest.mark.parametrize("written", ["2026-01-15", "January 15, 2026", "15 January 2026", "Jan 15, 2026",
                                     "January 15th, 2026", "**2026-01-15**", "2026‑01‑15", "January 2026"])
def test_cited_document_date_is_grounded_in_every_form(written: str) -> None:
    answer = f"The handbook dated {written} lists $500 per year [S1]."
    assert check_answer(answer, {"S1": HANDBOOK}, today=TODAY).ungrounded_facts == 0
    undated = Source(HANDBOOK.header, HANDBOOK.content)
    assert check_answer(answer, {"S1": undated}, today=TODAY).ungrounded_facts >= 1   # the year isn't in the text


def test_todays_date_is_grounded_in_any_cited_sentence() -> None:
    answer = "The $500 stipend still applies as of today, 2026-10-13 [S1]."
    assert check_answer(answer, {"S1": HANDBOOK}, today=TODAY).ungrounded_facts == 0
    # 2026, 10 and 13 are checked as numbers again when the date is not the one the prompt showed
    assert check_answer(answer, {"S1": HANDBOOK}, today=date(2026, 10, 14)).ungrounded_facts == 3
    assert check_answer(answer, {"S1": HANDBOOK}).ungrounded_facts == 3      # no "Today's date" line (answer.v1)


# --- what is still flagged ----------------------------------------------------------------------------------------
def test_a_date_shown_nowhere_is_still_flagged() -> None:
    check = check_answer("The handbook dated 2026-03-27 lists $500 per year [S1].", {"S1": HANDBOOK}, today=TODAY)
    assert check.ungrounded_facts == 2 and check.citations[0].weak_support          # 2026 and 27
    assert _badge(check) == "low"
    near = check_answer("The handbook dated January 16, 2026 lists $500 per year [S1].", {"S1": HANDBOOK},
                        today=TODAY)
    assert near.ungrounded_facts == 2                                                 # off by one day: 16, 2026
    assert check_answer("It was updated in February 2026 [S1].", {"S1": HANDBOOK}, today=TODAY).ungrounded_facts == 1


def test_a_wrong_figure_next_to_a_shown_date_is_still_flagged() -> None:
    wrong = check_answer("The handbook dated 2026-01-15 lists $600 per year [S1].", {"S1": HANDBOOK}, today=TODAY)
    assert wrong.ungrounded_facts == 1                                                # $600; the date is fine
    real = check_answer("The handbook dated 2026-01-15 lists $600 per year, effective as of today, 2026-10-13 [S1].",
                        STIPEND_SOURCES, today=TODAY)
    assert real.ungrounded_facts == 1
    # A shown date grounds only itself: "15" from 2026-01-15 does not vouch for "15 days" (the source says 10).
    days = check_answer("The handbook dated 2026-01-15 says equipment is returned within 15 days [S1].",
                        STIPEND_SOURCES, today=TODAY)
    assert days.ungrounded_facts == 1


def test_another_sources_date_needs_that_sources_citation() -> None:
    sources = {"S1": HANDBOOK, "S2": Source("Board update (p. 5)", "The stipend rises to $750.", BOARD_DATE)}
    assert check_answer("The board update dated 2026-10-02 raised it [S1].", sources,
                        today=TODAY).ungrounded_facts == 2      # S1 is dated 2026-01-15; 2026 and 10 aren't in S1
    assert check_answer("The board update dated 2026-10-02 raised it [S2].", sources, today=TODAY).ungrounded_facts == 0


# Each states a shown date as the date of an EVENT, and each is wrong: the increase took effect on October 1, 2026,
# and the handbook's own date is not a payment date. Mixing up the document date, today and the effective date is
# exactly what m-stipend-conflict probes, so a shown date is excused only where it is presented as that date.
@pytest.mark.parametrize(("answer", "flags"), [
    ("The increase to $750 per year took effect on 2026-10-02 [S2].", 1),             # the board update's date: 10
    ("The increase to $750 per year took effect as of 2026-10-02 [S2].", 1),          # "as of" presents today only
    ("The increase to $750 per year takes effect on 2026-10-13 [S2].", 2),            # today: 10, 13
    ("The board approved the $750 stipend on October 13, 2026 [S2].", 1),             # today: 13
    ("The $500 stipend was paid on January 15, 2026 [S1].", 1),                       # the handbook's date: 15
])
def test_a_shown_date_used_as_an_event_date_is_still_checked(answer: str, flags: int) -> None:
    check = check_answer(answer, STIPEND_SOURCES, today=TODAY)
    assert check.ungrounded_facts == flags and _badge(check) == "low"


def test_todays_date_as_an_event_date_is_an_uncited_claim() -> None:
    check = check_answer("The board approved the increase on October 13, 2026. The stipend is $750 per year [S2].",
                         STIPEND_SOURCES, today=TODAY)
    assert check.uncited_claims == 1 and _badge(check) == "low"


def test_known_miss_a_date_presented_as_today_is_not_checked_for_its_role() -> None:
    """Accepted trade-off: after its cue a shown date is excused whatever the sentence claims about it. This answer
    says the increase took effect today (it took effect October 1); the checker matches digits, not meaning, so only
    the offline judge can catch it."""
    check = check_answer("The increase to $750 per year took effect today, 2026-10-13 [S2].", STIPEND_SOURCES,
                         today=TODAY)
    assert check.ungrounded_facts == 0


@pytest.mark.parametrize("answer", [
    "The $750 stipend is in effect as of today, October 13 [S2].",                    # no year
    "The handbook dated January 15 still lists $500 per year [S1].",                  # no year
    "The board update dated 2026/10/02 is newer [S2].",                               # slashes
])
def test_known_limit_a_shown_date_in_another_form_is_checked_as_numbers(answer: str) -> None:
    """Accepted limit: only the forms in _DATE_RES are recognised, so these correct answers still get one flag (badge
    'low'): the behaviour before shown dates were grounded, never a false 'grounded'. No stored eval answer used these
    forms, and matching year-less dates would widen what is excused ("may 12" is also a verb and a number)."""
    assert check_answer(answer, STIPEND_SOURCES, today=TODAY).ungrounded_facts == 1


# --- sentences without a citation -----------------------------------------------------------------------------
def test_uncited_sentence_may_state_today_but_not_a_document_date() -> None:
    """Decision: today's date needs no citation (no [S#] could vouch for it, like a sentence with no number at all);
    a document date does (it is a fact about one document, and rule 3 asks the model to say which)."""
    def uncited(answer: str) -> int:
        return check_answer(answer, {"S1": HANDBOOK}, today=TODAY).uncited_claims

    assert uncited("Today is 2026-10-13. The stipend is $500 per year [S1].") == 0
    assert uncited("As of today (October 13, 2026), nothing has changed. The stipend is $500 per year [S1].") == 0
    assert uncited("The handbook is dated 2026-01-15. The stipend is $500 per year [S1].") == 1
    assert uncited("As of 2026-10-13 the stipend is $750. The stipend is $500 per year [S1].") == 1   # $750 is a claim
    assert check_answer("Today is 2026-10-13. The stipend is $500 per year [S1].", {"S1": HANDBOOK}
                        ).uncited_claims == 1                    # no today shown (v1): the date is just a number


# --- date parsing ------------------------------------------------------------------------------------------------
def test_date_mentions_prefer_day_precision_and_skip_impossible_dates() -> None:
    mentions = date_mentions("From 15 January 2026 (see January 2026) to 2026-02-30.")
    assert [(m.day, m.month_only) for m in mentions] == [(date(2026, 1, 15), False), (date(2026, 1, 1), True)]
    assert without_shown_dates("Dated 2026-01-15, $500.", document_dates=[HANDBOOK_DATE]) == "Dated           , $500."
    assert without_shown_dates("Dated 2026-01-15.") == "Dated 2026-01-15."


def test_a_shown_date_is_excused_only_right_after_its_cue() -> None:
    """A document date after "dated" (the <source date="..."> attribute); today's date after "today", "today is",
    "today's date" or "as of" (the "Today's date" line). Markdown emphasis and punctuation may sit in between."""
    def blanked(text: str, *, document_dates: tuple[date, ...] = (), today: date | None = None) -> bool:
        return "2026" not in without_shown_dates(text, document_dates=document_dates, today=today)

    handbook = (HANDBOOK_DATE,)
    for text in ("dated 2026-01-15", "Dated: **2026-01-15**", "(dated January 15, 2026)", "dated January 2026"):
        assert blanked(text, document_dates=handbook), text
    for text in ("Due 2026-01-15", "paid on 2026-01-15", "outdated 2026-01-15", "as of 2026-01-15",
                 "today, 2026-01-15"):
        assert not blanked(text, document_dates=handbook), text
    for text in ("today, 2026-10-13", "Today is October 13, 2026", "today's date is 2026-10-13",
                 "today’s date: 2026-10-13", "As of 2026-10-13", "as of today (13 October 2026)", "as of October 2026"):
        assert blanked(text, today=TODAY), text
    for text in ("on 2026-10-13", "dated 2026-10-13", "effective 2026-10-13"):
        assert not blanked(text, today=TODAY), text


@pytest.mark.parametrize("month", ["Auguſt", "ſept", "Aprıl", "Aprİl"])
def test_a_month_name_matched_only_through_unicode_case_folding_is_not_a_date(month: str) -> None:
    """Regression: re.IGNORECASE folds Unicode, so 'ſ' (U+017F) matches 's' and 'ı' / 'İ' match 'i'. The pattern
    accepted these words and the month lookup raised KeyError, which failed the whole turn after the answer had
    streamed. Such a word is not a month the checker knows: no date, its digits are checked as numbers."""
    assert date_mentions(f"{month} 12, 2026; 12 {month} 2026; {month} 2026") == []
    check = check_answer(f"The $500 stipend is paid in the January payroll, as of {month} 2026 [S1].",
                         {"S1": HANDBOOK}, today=TODAY)
    assert check.ungrounded_facts == 1                                                # 2026 isn't in the source


# --- the prompt and the checker see the same "today" ---------------------------------------------------------------
def test_answer_prompt_carries_the_date_its_today_line_shows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The clock is read once per prompt: if the checker asked again after a stream that crossed midnight it would
    ground the wrong day. Simulated with a clock that moves forward on every read."""
    days = iter([date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15)])
    monkeypatch.setattr(Settings, "today", lambda self: next(days))
    prompt = build_answer_prompt(history=[], question="q", standalone=None, numbered={}, version="v2")
    assert prompt.today == date(2026, 10, 13)
    assert prompt.input[-1]["content"].startswith("Today's date: 2026-10-13\n")
    v1 = build_answer_prompt(history=[], question="q", standalone=None, numbered={}, version="v1")
    assert v1.today is None and "Today's date" not in v1.input[-1]["content"]
    assert answer_prompt.shown_today("v1", settings.today()) is None
