"""Unit tests for evals/scoring.py: every number in an eval report comes from these pure functions."""

import re

import pytest

from app.core.textnorm import normalize as base_normalize
from evals import golden, scoring

_LEGACY_MARKDOWN = re.compile(r"[*_`]+")
_LEGACY_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")


def legacy_contains(haystack: str, needle: str) -> bool:
    """The matcher this branch replaced (evals/scoring.contains up to commit 330610e): one regex per phrase, whole
    words, no filler words, no negation guard. Kept verbatim so every row below shows the old verdict next to the
    new one, i.e. the table proves what changed instead of only describing the new behaviour."""
    def norm(text: str) -> str:
        return _LEGACY_THOUSANDS.sub("", _LEGACY_MARKDOWN.sub("", base_normalize(text)))

    n = norm(needle)
    if not n:
        return False
    left = r"(?<![a-z0-9])(?<![0-9][.,])" if n[0].isalnum() else ""
    right = r"(?![a-z0-9])(?![.,][0-9])" if n[-1].isalnum() else ""
    return re.search(left + re.escape(n) + right, norm(haystack)) is not None


CONTRACTORS = next(c for c in golden.load(starters=None).cases if c.id == "f-contractors-parental").expect["any_of"]
TODAY = ("No. Contractors are explicitly excluded from parental leave under clause 4.1 of the Larkspur Employee "
         "Handbook 2026. [S1]")      # f-contractors-parental in 20261021T163824Z, verbatim: a correct answer it failed

# (why, answer, accepted phrases, old verdict, new verdict). The verdict is "some phrase counts" (any_of).
MATCH_TABLE = [
    ("today's false negative: a filler word inside 'are ... excluded'", TODAY, CONTRACTORS, False, True),
    ("filler: a word inside a number phrase", "New hires get 18 PTO days per calendar year. [S1]",
     ["18 days"], False, True),
    ("filler: the number spelled out before the digits", "You may carry over up to five (5) unused days.",
     ["up to 5"], False, True),
    ("filler: markdown emphasis around it", "Contractors are **explicitly** _excluded_.", ["are excluded"], False,
     True),
    ("a citation marker inside the phrase", "Larkspur provides 16 weeks [S1] of paid parental leave.",
     ["16 weeks of paid parental leave"], False, True),
    ("a lower-case range marker inside the phrase", "Contractors are [s1-3] excluded.", ["are excluded"], False,
     True),
    ("a zero-width space between the words", "Contractors are not\u200beligible.", ["not eligible"], False, True),
    ("negated: 'does not say' right before", "The handbook does not say contractors are excluded.",
     ["are excluded"], True, False),
    ("negated and quoted", 'The handbook does not say "contractors are excluded" anywhere; it lists employee benefits.',
     ["are excluded"], True, False),
    ("negated by a reporting verb far before it", "The handbook does not state anywhere in section 4 that contractors "
     "are excluded.", ["are excluded"], True, False),
    ("negated: 'never' just before", "Contractors were never excluded from parental leave.", ["excluded"], True,
     False),
    ("a phrase with its own negation, negated again", "It isn't true that contractors are not eligible.",
     ["not eligible"], True, False),
    ("negated: 'not the case that'", "It is not the case that contractors are excluded.", ["are excluded"], True,
     False),
    ("quoted in a question, then answered the other way", 'You asked "are contractors not eligible?" Yes, they can '
     "take leave after 90 days.", ["not eligible"], True, False),
    ("a negator is never a filler", "Contractors are not excluded from parental leave.", ["are excluded"], False,
     False),
    ("a filler cannot make a question an answer", "Are contractors excluded? The handbook lists employee benefits.",
     ["are excluded"], False, False),
    ("whole numbers: '10 days' is not inside '110 days'", "Employees get 110 days.", ["10 days"], False, False),
    ("whole numbers: '5 days' is not inside '1.5 days'", "PTO accrues at 1.5 days per month.", ["5 days"], False,
     False),
    ("whole numbers: '4.2' is not inside '4.25'", "Revenue was $4.25M.", ["4.2"], False, False),
    ("whole numbers: a range is not its upper end", "Employees get 15\u201318 days of PTO.", ["18 days"], True, False),
    ("a number is never a filler", "Employees get 20 to 25 days.", ["20 days"], False, False),
    ("a phrase never spans punctuation it doesn't have", "Interns: not applicable. Eligible after 90 days: employees.",
     ["not eligible"], False, False),
    ("... but may span punctuation it does have", "The stipend rises on Oct. 1, 2026.", ["oct. 1", "oct 1"], True,
     True),
    ("a negation in an earlier sentence does not leak", "Interns are not eligible until day 90. Contractors are "
     "excluded.", ["are excluded"], True, True),
    ("'No,' answering the question does not negate what follows", "No, contractors are not eligible for parental "
     "leave.", CONTRACTORS, True, True),
    ("'but' ends what a reporting negation covers", "The handbook does not mention interns but contractors are "
     "excluded.", ["are excluded"], True, True),
    ("a bound is not a negation", "Brightline's liability will not exceed $2,000,000.", ["2000000"], True, True),
    ("a bound is not a negation (dates)", "The new stipend applies no later than October 1, 2026.", ["october 1"],
     True, True),
    ("curly apostrophe and a non-breaking space", "Contractors aren\u2019t\u00a0eligible.", ["aren't eligible"], True,
     True),
    ("an em dash separates clauses", "Contractors\u2014not eligible.", ["not eligible"], True, True),
    ("full-width digits", "Employees get \uff11\uff18 days.", ["18 days"], True, True),
    ("multi-sentence: the number in a later sentence", "The handbook covers PTO. After three years, employees get "
     "**20 days** in total. [S1]", ["20 days"], True, True),
    ("limitation, kept visible: who the phrase is about is not checked", "Only interns are not eligible; contractors "
     "are eligible.", ["not eligible"], True, True),
    # A bound word only makes a bound when it sits BETWEEN the negator and the phrase. When the phrase itself starts
    # with it, the negator negates the phrase: these answers say the opposite of the phrase.
    ("a negated phrase that starts with a bound word: 'not only to employees'", "The stipend is available not only "
     "to employees but also to contractors, at $500 per year.", ["only to employees"], True, False),
    ("a negated phrase that starts with a bound word: 'not below'", "Larkspur's Q3 availability of 99.62% was not "
     "below 99.9%.", ["below"], True, False),
    ("a negated phrase that starts with a bound word: 'not less than'", "Yes: 99.62% is not less than 99.9%.",
     ["less than"], True, False),
    ("a bound word anywhere between the negator and the phrase: 'can't go over'", "You can't go over 30 days in any "
     "12-month period.", ["30 days"], True, True),
    ("a bound word anywhere between: 'may not stay over'", "You may not stay over 30 days.", ["30 days"], True, True),
    ("an idiom is not a negation: 'without delay (within 24 hours)'", "Report it without delay (within 24 hours).",
     ["24 hours"], True, True),
    ("'within' is not a bound: 'not within 24 hours' says the opposite", "Not within 24 hours; you have 7 days to "
     "report it.", ["24 hours"], True, False),
    ("an idiom is not a negation: 'no doubt'", "No doubt contractors are excluded.", ["are excluded"], True, True),
    ("an idiom is not doubt either: 'without delay if ...' is a condition", "Report it without delay if it is stolen "
     "within 24 hours.", ["24 hours"], True, True),
    ("an idiom is not a negation: 'not to mention'", "Not to mention full-timers get 18 days.", ["18 days"], True,
     True),
    ("'in no case later than' is a bound, not 'not the case'", "Report it immediately, and in no case later than 24 "
     "hours.", ["24 hours"], True, True),
    # The window (NEGATION_WINDOW = 3) from both sides, and the reporting rule's own reach (2 tokens after the negator)
    ("a negation 3 tokens back is inside the window", "Contractors were not ever formally excluded.", ["excluded"],
     True, False),
    ("a negation 4 tokens back is outside it", "Employees with no prior service get 18 days.", ["18 days"], True,
     True),
    ("a reporting word counts only right after the negator", "Employees with no prior service are confirmed to get "
     "18 days.", ["18 days"], True, True),
    ("an em dash is a clause break on its own", "Not interns—contractors are excluded.",
     ["contractors are excluded"], True, True),
    ("a hyphen inside a word is not a clause break", "Contractors are a not-excluded group.", ["excluded"], True,
     False),
    ("a newline is a clause break in an answer: '**No**' on its own line negates nothing", "**No**\nContractors "
     "don't receive the remote work stipend.", ["don't receive"], True, True),
    ("a newline is a clause break in an answer: a question on a later line is its own", "Full-time employees get 18 "
     "days of PTO per year [S1]\n\nAnything else I can help with?", ["18 days"], True, True),
    ("a quote mark inside the phrase is ignored", 'Contractors are "excluded" from it.', ["are excluded"], False,
     True),
    ("three filler words are one too many (MAX_FILLERS = 2)", "Contractors are also expressly specifically "
     "excluded.", ["are excluded"], False, False),
    ("a conjunction never fills a gap, even where the phrase's own punctuation lets one cross", "The stipend rises "
     "on Oct. but 1.", ["oct. 1"], False, False),
    # Negations beyond "not": "nowhere", the clause-negating "appear" / "evidence", and two-word negations
    ("'nowhere' is a negation", "The handbook nowhere states contractors are excluded.", ["are excluded"], True,
     False),
    ("'doesn't appear that' negates the clause", "It doesn't appear that contractors are excluded.", ["are excluded"],
     True, False),
    ("'no evidence that' negates the clause", "There is no evidence that contractors are excluded.",
     ["are excluded"], True, False),
    ("'fails to mention' negates the clause", "The handbook fails to mention that contractors are excluded.",
     ["are excluded"], True, False),
    ("contrast: 'instead of' before the phrase", "Full-timers get 20 days instead of 18 days.", ["18 days"], True,
     False),
    ("contrast: 'rather than' before the phrase", "Rather than 18 days, full-timers get 20 days.", ["18 days"], True,
     False),
    ("contrast: 'rather than forfeited' says they are not forfeited", "Yes, unused days are paid out at year end "
     "rather than forfeited.", ["forfeited"], True, False),
    ("contrast after the phrase changes nothing", "Full-timers get 18 days rather than 20.", ["18 days"], True, True),
    # Hedges: what follows "not sure whether" / "unclear whether" / "nothing says" is not asserted
    ("hedge: 'not sure whether'", "I'm not sure whether contractors are excluded.", ["are excluded"], True, False),
    ("hedge: 'can't tell whether'", "I can't tell whether contractors are excluded.", ["are excluded"], True, False),
    ("hedge: 'cannot determine whether'", "I cannot determine whether contractors are excluded from parental leave.",
     ["are excluded"], True, False),
    ("hedge: 'not known if'", "It's not known if contractors are excluded.", ["are excluded"], True, False),
    ("hedge: 'unclear whether'", "It is unclear whether contractors are excluded.", ["are excluded"], True, False),
    ("hedge: 'nothing says'", "Nothing says contractors are excluded.", ["are excluded"], True, False),
    ("hedge: 'can't tell if'", "I can't tell if contractors are excluded.", ["are excluded"], True, False),
    ("hedge: 'silent on whether'", "The documents are silent on whether contractors are excluded.",
     ["are excluded"], True, False),
    ("hedge: 'don't think'", "I don't think contractors are excluded.", ["are excluded"], True, False),
    ("a plain 'if' is a condition, not a hedge", "If you joined this year you get 18 days.", ["18 days"], True, True),
    ("'whether' after the phrase changes nothing", "Contractors are excluded whether or not they work full time.",
     ["are excluded"], True, True),
    ("only doubt BEFORE 'whether' makes a hedge: 'whether or not' asserts what follows", "Whether or not they work "
     "full time contractors are excluded.", ["are excluded"], True, True),
    # Limitations, kept visible (the judge is for these): rhetorical negation and contrast are not read
    ("limitation: 'it is false that' is not read as a negation", "It is false that contractors are excluded.",
     ["are excluded"], True, True),
    ("limitation: 'nowhere does ... say' (the verb is too far from 'nowhere')", "Nowhere does the handbook say "
     "contractors are excluded.", ["are excluded"], True, True),
    ("limitation: 'none of ... say' (too far, and 'none' is not a negation)", "None of the documents say contractors "
     "are excluded.", ["are excluded"], True, True),
    ("limitation: 'neither ... nor ... says' (the verb is too far from 'nor')", "Neither the handbook nor the board "
     "update says contractors are excluded.", ["are excluded"], True, True),
    ("limitation: a reporting verb 3 words after the negation", "The handbook does not specifically or clearly say "
     "that contractors are excluded.", ["are excluded"], True, True),
    ("limitation: a negation 4 tokens back ('need not report it within')", "You need not report it within 24 "
     "hours.", ["24 hours"], True, True),
    ("limitation: a hyphenated word pushes the negation out of the window", "The handbook does not give new "
     "full-time employees 18 days.", ["18 days"], True, True),
    ("limitation: a comma cuts the negation off", "You do not, under the current policy, get 18 days.",
     ["18 days"], True, True),
    ("limitation: a correction after the phrase is not read", "18 days is wrong; full-timers get 20 days.",
     ["18 days"], True, True),
    ("limitation: a question in parentheses makes the sentence a question", "You get 18 days per year (did you mean "
     "including the service increase?)", ["18 days"], True, False),
    ("limitation: a hedging adverb is a filler ('does not explicitly apply')", "The stipend section does not "
     "explicitly apply to contractors.", ["does not apply"], False, True),
    ("limitation: doubt AFTER the phrase is not read (live answer, 20261021T185603Z)", "The sources provide no "
     "monthly figures, so whether Larkspur reached 99.5% in every month cannot be determined.", ["reached"], True,
     True),
]


@pytest.mark.parametrize(("why", "answer", "phrases", "old", "new"), MATCH_TABLE, ids=[r[0] for r in MATCH_TABLE])
def test_match_table_old_and_new_verdicts(why: str, answer: str, phrases: list[str], old: bool, new: bool) -> None:
    assert any(legacy_contains(answer, p) for p in phrases) is old, "old verdict"
    assert scoring.matches_any(answer, phrases) is new, "new verdict"


def test_todays_false_negative_now_passes_the_whole_case() -> None:
    case = next(c for c in golden.load(starters=None).cases if c.id == "f-contractors-parental")
    answer = {"content": TODAY, "answer_type": "answered", "citations": [
        {"ref": "S1", "filename": "Larkspur_Employee_Handbook_2026.pdf"}],
        "signals": {"valid_citations": 1, "invalid_citations": 0, "ungrounded_facts": 0, "gate": False}}
    result = scoring.score_answer(case.expect, answer)
    assert result["passed"], result["failed"]
    assert result["evidence"]["any_of"] == [{"phrase": "are excluded", "matched": "are explicitly excluded"}]


def test_evidence_says_why_an_occurrence_did_not_count() -> None:
    answer = {"content": "The handbook does not say contractors are excluded. Are they not eligible?",
              "answer_type": "answered", "citations": [], "signals": {}}
    evidence = scoring.score_answer({"any_of": ["are excluded", "not eligible", "ineligible"]}, answer)["evidence"]
    assert evidence["any_of"] == [
        {"phrase": "are excluded", "rejected": "negated by 'not' + 'say'", "text": "are excluded"},
        {"phrase": "not eligible", "rejected": "in a question", "text": "not eligible"},
    ]


@pytest.mark.parametrize(("answer", "forbidden", "old", "new"), [
    ("The handbook does not say contractors are eligible.", "contractors are eligible", False, True),
    ("Contractors are not eligible.", "contractors are eligible", True, True),
    ("Yes, contractors are fully eligible.", "contractors are eligible", True, False),
])
def test_forbid_uses_the_same_matcher(answer: str, forbidden: str, old: bool, new: bool) -> None:
    """forbid = the answer must not ASSERT the phrase, so a negated mention is fine and a filler can't hide it."""
    assert (not legacy_contains(answer, forbidden)) is old
    assert scoring.check_forbid(answer, [forbidden]) is new


def test_all_of_uses_the_same_matcher() -> None:
    answer = "Employees get 18 calendar days and may carry up to five (5) days over."
    groups = [["18 days"], ["up to 5", "5 unused"]]
    assert not all(any(legacy_contains(answer, p) for p in g) for g in groups)
    assert scoring.check_all_of(answer, groups) is True


def test_a_repeated_word_does_not_hide_a_match() -> None:
    """The first place the next word fits is not always the one the rest of the phrase fits: from the first 'from',
    'parental' is 3 words away (more than MAX_FILLERS), from the second it is 2. _extend has to try both."""
    answer = "Contractors are excluded from from any paid parental leave."
    assert scoring.contains(answer, "excluded from parental leave")
    assert not scoring.contains("Contractors are excluded from all of the paid parental leave.",
                                "excluded from parental leave")


def test_the_content_verdict_counts_every_content_check_and_nothing_else() -> None:
    """The judge is compared with these checks only: an injected 'APPROVED' at the start is a content failure, an
    invalid citation is not (the judge never sees citation ids). The names are spelled out here, not read from
    scoring.CONTENT_CHECKS, so dropping one there fails this test."""
    for name in ("any_of", "all_of", "mentions", "forbid", "forbid_start", "not_found"):
        assert scoring.content_verdict({name: False}) is False, name
    assert scoring.content_verdict({"forbid_start": False, "any_of": True}) is False
    assert scoring.content_verdict({"citations_valid": False, "gold_citation": False, "grounded": False,
                                    "any_of": True, "not_found": True}) is True
    assert scoring.content_verdict({"any_of": None, "forbid_start": None}) is True


def test_mentions_only_needs_the_words_there_negated_or_not() -> None:
    """`mentions` names what the answer must be about. "Contractors don't get the stipend" is about the stipend
    although "stipend" sits right after the negation, so as an any_of phrase it would not count."""
    answer = "No. Contractors don't get the stipend."
    assert scoring.check_mentions(answer, ["stipend"]) is True
    assert not scoring.contains(answer, "stipend")
    assert scoring.check_mentions(answer, ["stipend", "parental leave"]) is False        # every phrase
    assert scoring.check_mentions("No. Contractors don't get it.", ["stipend"]) is False
    assert scoring.check_mentions(answer, None) is None
    expect = {"mentions": ["stipend"], "any_of": ["don't get"]}
    result = scoring.score_answer(expect, _answer(answer))
    assert result["passed"] and result["evidence"]["mentions"] == [{"phrase": "stipend", "matched": "stipend"}]
    assert scoring.score_answer(expect, _answer("No. Contractors don't get it."))["failed"] == ["mentions"]


# --- every word of every rule list does its job (each list spelled out, so a word dropped there fails a test) ------
def test_every_negator_negates() -> None:
    words = {"not", "no", "never", "neither", "nor", "without", "cannot", "nothing", "nowhere"}
    assert scoring.NEGATORS == words
    assert scoring.contains("Contractors are excluded.", "excluded")
    for word in sorted(words) + ["aren't"]:
        assert not scoring.contains(f"Contractors are {word} excluded.", "excluded"), word


def test_every_two_word_negation_negates_and_one_word_of_it_does_not() -> None:
    pairs = {("rather", "than"), ("instead", "of"), ("fails", "to"), ("failed", "to"), ("fail", "to")}
    assert scoring.NEGATOR_PAIRS == pairs
    for first, second in sorted(pairs):
        rest = "say you get 18 days" if first in scoring.REPORTING_ONLY else "get 18 days"     # see the next test
        assert not scoring.contains(f"They {first} {second} {rest}.", "18 days"), (first, second)
        assert scoring.contains(f"They {second} {rest}.", "18 days"), second
        assert scoring.contains(f"They {first}, {second} {rest}.", "18 days"), (first, second)  # not adjacent


def test_fail_to_negates_only_before_a_reporting_verb() -> None:
    """"fails to mention that ..." denies what follows like "does not mention that ..."; before any other verb "fail
    to" is a condition or a description that states it (round-3 review: both answers below are right)."""
    assert scoring.REPORTING_ONLY == {"fail", "fails", "failed"}
    for word in sorted(scoring.REPORTING_ONLY):
        assert not scoring.contains(f"The handbook {word} to mention that contractors are excluded.", "are excluded")
        assert not scoring.contains(f"It {word} to clearly say they are excluded.", "are excluded"), word
        assert scoring.contains(f"Days you {word} to use are forfeited.", "forfeited"), word
    assert scoring.contains("If you fail to report within 24 hours you breach policy.", "24 hours")


def test_every_idiom_starting_with_a_negator_negates_nothing() -> None:
    idioms = (("without", "delay"), ("without", "fail"), ("without", "exception"), ("no", "doubt"),
              ("not", "to", "mention"))
    assert scoring.NOT_NEGATIONS == idioms
    for idiom in idioms:       # the negator is within NEGATION_WINDOW (or, for "mention", the clause rule) of "18"
        assert scoring.contains(f"{' '.join(idiom)} get 18 days.", "18 days"), idiom
        assert not scoring.contains(f"{idiom[0]} get 18 days.", "18 days"), idiom
    # the whole idiom or nothing: "not to take" starts like "not to mention" and still negates
    assert not scoring.contains("You are told not to take 18 days.", "18 days")


def test_every_bound_word_makes_a_negation_a_bound() -> None:
    words = {"more", "less", "fewer", "greater", "later", "earlier", "longer", "shorter", "sooner", "exceed", "exceeds",
             "exceeding", "above", "below", "over", "under", "beyond", "before", "after", "until", "only", "past"}
    assert scoring.BOUND_WORDS == words
    assert not scoring.contains("You may not stay 30 days.", "30 days")
    for word in sorted(words):
        assert scoring.contains(f"You may not stay {word} 30 days.", "30 days"), word


def test_every_clause_negating_word_negates_the_rest_of_the_clause() -> None:
    """'not' is 5 tokens before the phrase here, outside NEGATION_WINDOW: only the clause rule can reject it."""
    words = {"say", "says", "said", "state", "states", "stated", "mention", "mentions", "mentioned", "specify",
             "specifies", "specified", "indicate", "indicates", "indicated", "confirm", "confirms", "confirmed",
             "true", "case", "clear", "sure", "certain", "known", "determine", "think", "believe", "appear", "appears",
             "seem", "seems", "evidence"}
    assert scoring.CLAUSE_NEGATING == words
    for word in sorted(words - {"case"}):
        assert not scoring.contains(f"It does not {word} that the contractors are excluded.", "are excluded"), word
        assert scoring.contains(f"It does {word} that the contractors are excluded.", "are excluded"), word
    assert not scoring.contains("It is not the case that the contractors are excluded.", "are excluded")


def test_every_doubt_word_and_indirect_question_word_makes_a_hedge() -> None:
    doubts, questions = {"unclear", "unsure", "uncertain", "unknown", "silent"}, {"whether", "if"}
    assert scoring.DOUBT_WORDS == doubts and scoring.INDIRECT_QUESTION == questions
    for doubt in sorted(doubts):
        for word in sorted(questions):
            assert not scoring.contains(f"It is {doubt} {word} contractors are excluded.", "are excluded"), (doubt,
                                                                                                         word)
        assert scoring.contains(f"It is {doubt}. Contractors are excluded.", "are excluded"), doubt
    for word in sorted(questions):
        assert not scoring.contains(f"I can't tell {word} the contractors are excluded.", "are excluded"), word


def test_every_conjunction_starts_a_new_clause() -> None:
    words = {"but", "however", "whereas", "although", "though", "while"}
    assert scoring.CONJUNCTIONS == words
    assert not scoring.contains("The handbook does not mention interns and contractors are excluded.", "are excluded")
    for word in sorted(words):
        assert scoring.contains(f"The handbook does not mention interns {word} contractors are excluded.",
                                "are excluded"), word


def test_retrieval_relevance_ignores_negation_but_keeps_word_boundaries() -> None:
    """gold_chunk phrases are document text, not claims: a chunk that says 'does not carry over' IS the passage."""
    chunk = "Each employee may take 1 paid volunteer day per year. The volunteer day does not carry over."
    assert scoring.is_relevant(chunk, ["carry over"])
    assert not scoring.contains(chunk, "carry over")                        # as an answer it would be negated
    assert scoring.is_relevant("Brightline's liability will not exceed $2,000,000 [S2].", ["exceed $2,000,000"])
    assert not scoring.is_relevant("PTO accrues at 11.5 days.", ["1.5 days"])


# --- text matching ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("haystack", "needle"), [
    ("Employees get **18 days** of PTO.", "18 days"),            # markdown emphasis is ignored
    ("It cost $120,000 in total.", "120000"),                     # thousands separators are dropped
    ("It cost $120000 in total.", "120,000"),
    ("Give 60 days’ written notice", "60 days' written"),   # curly apostrophe == straight
    ("REVENUE WAS $4.2M", "4.2m"),                                # casefold
    ("gross margin of 68%.", "68%"),                              # needle ending in a symbol
    ("multi \t line   text", "multi line text"),                # whitespace collapsed
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


def test_a_newline_ends_a_clause_in_an_answer_but_not_in_a_retrieved_chunk() -> None:
    """A model's line break ends a list item or a paragraph; a PDF's line break is layout inside a sentence."""
    assert not scoring.contains("Employees get 18\ndays.", "18 days")
    assert scoring.is_relevant("Employees get 18 days of paid\ntime off per year.", ["18 days of paid time off"])
    assert scoring.contains("No\nContractors don't receive it.", "don't receive")
    assert not scoring.contains("No contractors don't receive it.", "don't receive")
    assert not scoring.contains("You get 18 days\r\nnot 20.", "18 days not 20")


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
    assert scoring.check_forbid_start("- \"Approved\": yes", ["approved"]) is False
    # whole words: a forbidden "no" or "20" is not the start of "Notably", "Nonetheless" or "2026"
    assert scoring.check_forbid_start("No: 99.62% is below it.", ["no"]) is False
    assert scoring.check_forbid_start("Notably, yes. Nonetheless ...", ["no"]) is True
    assert scoring.check_forbid_start("2026 policy: 18 days.", ["20"]) is True
    assert scoring.check_forbid_start("20 days: 18 + 2.", ["20"]) is False
    assert scoring.check_forbid_start("20 days: 18 + 2.", ["20 days"]) is False
    assert scoring.check_forbid_start("20 weeks of leave.", ["20 days"]) is True        # every word of the prefix
    assert scoring.check_forbid_start("No: 99.62% is below it.", ["**", "[S1]", "..."]) is True   # no words: ignored
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


def test_only_gate_refusals_are_not_model_generated() -> None:
    assert not scoring.model_generated(_answer("x", answer_type="not_found", gate=True))
    assert scoring.model_generated(_answer("x", answer_type="not_found", gate=False))     # the model refused
    assert scoring.model_generated(_answer("x"))


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


@pytest.mark.parametrize("signals", [None, {}, {"signal": {"invalid_citations": 0}}])
def test_missing_signals_fail_the_checks_that_read_them(signals: dict | None) -> None:
    """Regression: a missing (or renamed) `signals` used to read as 0 invalid citations and 0 ungrounded facts, so
    citations_valid and grounded passed without anything being checked, and min_citations counted raw citations."""
    answer = _answer("You get 18 days [S1].")
    answer["signals"] = signals
    expect = {"any_of": ["18 days"], "min_citations": 1, "grounded": True}
    result = scoring.score_answer(expect, answer)
    assert not result["passed"]
    assert set(result["failed"]) == {"citations_valid", "min_citations", "grounded"}
    del answer["signals"]                                               # absent altogether: the same
    assert set(scoring.score_answer(expect, answer)["failed"]) == {"citations_valid", "min_citations", "grounded"}


def test_each_missing_signal_fails_only_its_own_check() -> None:
    answer = _answer("You get 18 days [S1].")
    del answer["signals"]["ungrounded_facts"]
    reported = scoring.score_answer({"any_of": ["18 days"]}, answer)
    assert reported["passed"] and reported["checks"]["grounded"] is False      # reported as not verified
    assert scoring.score_answer({"any_of": ["18 days"], "grounded": True}, answer)["failed"] == ["grounded"]
    del answer["signals"]["invalid_citations"]
    assert scoring.score_answer({"any_of": ["18 days"]}, answer)["failed"] == ["citations_valid"]


# --- retrieval --------------------------------------------------------------------------------------------------
def test_all_relevant_rank_is_the_rank_by_which_every_gold_phrase_was_retrieved() -> None:
    """A question that needs two passages (18 PTO days + 8 sick days) is only answerable once both are in the
    context: first_relevant_rank says when the first one arrived, all_relevant_rank when the last one did."""
    contents = ["Employees get 18 days of paid time off.", "holiday policy", "Employees get 8 paid sick days per year."]
    gold = ["18 days of paid time off", "8 paid sick days per year"]
    assert scoring.first_relevant_rank(contents, gold) == 1
    assert scoring.all_relevant_rank(contents, gold) == 3
    assert scoring.all_relevant_rank(contents[:2], gold) is None             # one passage never retrieved
    assert scoring.all_relevant_rank(["18 days of paid time off and 8 paid sick days per year"], gold) == 1
    assert scoring.all_relevant_rank([], gold) is None


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
    assert summary == {"n": 2, "p50": 100.0, "p95": 300.0, "avg": 200.0, "max": 300.0}
    assert scoring.latency_summary([None])["max"] is None
