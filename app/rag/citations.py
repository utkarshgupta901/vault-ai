"""Deterministic answer checks: parse [S#] citations, verify that the facts in each sentence appear in the sources that
sentence cites, and pick a supporting snippet per citation.

No second LLM call: these checks are cheap, run on every answer, and are explainable line by line. They catch the
failures that matter most in a document assistant — a citation id that was never provided, a number or code that
isn't in the cited source, a factual sentence with no citation at all. Claim-level faithfulness ("is this paraphrase
true?") is left to the offline LLM judge.

Facts are the things a model gets subtly wrong: amounts, percentages, codes (LRK-4471), clause numbers (9.2) and
numbers >= 10. Units are normalized before comparing, so "$60k", "$60,000" and "60000" are the same fact.

Dates the model was SHOWN outside the chunk text are grounded too: each source's document date (the `date` attribute
of its <source> tag) and today's date (the "Today's date" line). answer.v2 rule 5 asks the model to use both, so an
answer that says "the handbook dated 2026-01-15 [S1]" or "effective as of today, 2026-10-13" is doing its job; the
chunk text alone can't vouch for those dates, which used to flag them and drag a correct answer to a low badge.
Such a date is excused only where it is presented AS that date (right after "dated", or "today" / "as of"): the same
date stated as when something happened ("took effect on 2026-10-02") is still checked as numbers. Known limits: after
its cue a date is excused whatever the sentence claims about it, and only the forms in _DATE_RES are recognised (a
year-less "October 13", "2026/10/02", "Jan. 15, 2026" or "10/13/2026" is checked as plain numbers).
"""

import html
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Protocol

from app.core.textnorm import normalize, normalize_reply_start
from app.rag.query_terms import STOPWORDS

REFUSAL_PREFIX = "i couldn't find this in your documents"
WEAK_OVERLAP = 0.2              # below this, the cited chunk barely shares words with the sentences citing it
MIN_BARE_NUMBER = Decimal(10)   # "3 years" is not checked; "18 days" and "2026" are
SNIPPET_MAX_CHARS = 300

# [S1]  [S1, S2]  [S1; 3]  and the forms models also write: ranges [S1-3] / [S1–S3] and lower case [s1]. Parsing is
# read-only: the stored and displayed answer keeps the marker exactly as the model wrote it. A range may use any dash
# models write: hyphen-minus, U+2010 hyphen, U+2011 non-breaking hyphen (common from GPT-5-family models), en and em
# dash, U+2212 minus. frontend/src/lib/citations.ts mirrors this grammar to draw the chips.
_RANGE_DASH = r"\s*[-\u2010\u2011\u2013\u2014\u2212]\s*"
_REF_ITEM = rf"S?\d+(?:{_RANGE_DASH}S?\d+)?"
CITATION_GROUP_RE = re.compile(rf"\[(S\d+(?:{_RANGE_DASH}S?\d+)?(?:\s*[,;]\s*{_REF_ITEM})*)\]", re.IGNORECASE)
_REF_ITEM_RE = re.compile(rf"(\d+)(?:{_RANGE_DASH}S?(\d+))?", re.IGNORECASE)
MAX_REF_RANGE = 20      # [S1-20] expands; a wider or reversed range ([S1-999], [S3-1]) keeps only its two ends

# Abbreviations whose period does not end a sentence. Split after "vs.", "up to 18 days vs. 10 days [S1]." would leave
# "up to 18 days vs.": a fact with no citation, i.e. an uncited claim that forces a low badge on a correct answer.
_ABBREVIATIONS = ("vs", "e.g", "i.e", "approx", "etc", "no", "inc", "ltd", "dr", "mr", "ms", "cf")
_NOT_AN_ABBREVIATION = "".join(rf"(?<!\b(?i:{re.escape(a)})\.)" for a in _ABBREVIATIONS)

# One sentence per line item; inside a line, split after . ! ? — but not after an abbreviation, and not when a
# citation follows ("days. [S1]" belongs to the sentence before it) — and after a citation group that is followed by
# a new capitalised sentence.
_SENTENCE_SPLIT_RE = re.compile(rf"(?<=[.!?]){_NOT_AN_ABBREVIATION}\s+(?![\s\[])|(?<=\])\s+(?=[A-Z])|\n+")

_SCALE = {"k": 1_000, "thousand": 1_000, "m": 1_000_000, "mm": 1_000_000, "million": 1_000_000,
          "b": 1_000_000_000, "bn": 1_000_000_000, "billion": 1_000_000_000}
_NUM = r"(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?"
_SUFFIX = r"(?:\s?(k|mm|m|bn|b|thousand|million|billion)\b)?"
_CODE_RE = re.compile(r"\b[A-Z]{2,}-\d+\b")
_AMOUNT_RE = re.compile(r"[$€£]\s?" + _NUM + _SUFFIX, re.IGNORECASE)
_PERCENT_RE = re.compile(r"(?<![\w.])" + _NUM + r"\s?(?:%|percent\b)", re.IGNORECASE)
_DOTTED_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)+(?!\w)")
_NUMBER_RE = re.compile(r"(?<![\w.])" + _NUM + _SUFFIX, re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z0-9]+")
# A calculation's result ends where its clause does: ';', a comma that is not a thousands separator, the next '=', a
# line break, or a joining word. An operator between two numbers marks an expression, not a result.
_RESULT_END_RE = re.compile(r";|,(?!\d)|=|\n|\s(?:and|but|so|which|while|whereas|assuming|if)\s", re.IGNORECASE)
_ARITHMETIC_RE = re.compile(r"[\d%)]\s*[-+−×x*/÷]\s*[$€£(]?\s?\d")

# Dates as the model writes them. Day precision first, so "15 January 2026" is one date, not "January 2026" + "15".
# Abbreviations are matched without a period: split_sentences already cuts "Jan. 15" after "Jan.", so such a date
# never reaches these patterns whole (it is checked as numbers, as before).
_MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
           "september": 9, "october": 10, "november": 11, "december": 12,
           "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10,
           "nov": 11, "dec": 12}
_MONTH = r"(?P<month>" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b"
_DAY = r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
_YEAR = r"(?P<year>\d{4})\b"
_DASH = "[-‐-–]"     # models sometimes write ISO dates with a non-breaking hyphen (U+2011)
_DATE_RES = (
    re.compile(rf"\b(?P<year>\d{{4}}){_DASH}(?P<month>\d{{2}}){_DASH}(?P<day>\d{{2}})\b"),   # 2026-01-15
    re.compile(rf"\b{_MONTH}\s+{_DAY},?\s+{_YEAR}", re.IGNORECASE),             # January 15, 2026 / Jan 15th 2026
    re.compile(rf"\b{_DAY}\s+{_MONTH},?\s+{_YEAR}", re.IGNORECASE),             # 15 January 2026
    re.compile(rf"\b{_MONTH},?\s+{_YEAR}", re.IGNORECASE),                       # January 2026 (month precision)
)
# What presents a shown date as itself. A document date follows "dated" (it is the <source>'s date attribute); today's
# date follows "today", "today is", "today's date" or "as of" ("as of" also introduces effective dates, so it does
# not excuse a document date). Markdown emphasis and punctuation may sit in between: "dated **2026-01-15**".
_CUE_GAP = r"[\s,:(*_]*$"
_DATED_CUE_RE = re.compile(r"\bdated" + _CUE_GAP, re.IGNORECASE)
_TODAY_CUE_RE = re.compile(r"\b(?:today(?:['’]s\s+date)?(?:\s+is)?|as\s+of)" + _CUE_GAP, re.IGNORECASE)


# --- Citation parsing ------------------------------------------------------------------------------------------
def parse_refs(text: str) -> list[str]:
    """All cited refs in order of appearance, normalized to "S<n>" ([S1, 2] -> S1, S2; [s1-3] -> S1, S2, S3).
    Duplicates kept. Ids that were never provided ([S0], [S9] of 8) are returned too: check_answer reports them."""
    refs: list[str] = []
    for group in CITATION_GROUP_RE.finditer(text or ""):
        for first, last in _REF_ITEM_RE.findall(group.group(1)):
            refs.extend(f"S{n}" for n in _expand(int(first), int(last) if last else None))
    return refs


def _expand(first: int, last: int | None) -> list[int]:
    """A range's ids, capped: a typo like [S1-999] must not turn into hundreds of refs, so it keeps its two ends (and
    S999 is then reported as an invalid citation)."""
    if last is None:
        return [first]
    if first <= last < first + MAX_REF_RANGE:
        return list(range(first, last + 1))
    return [first, last]


def strip_citations(text: str) -> str:
    """Remove [S#] markers. Used for conversation history: source numbers restart every turn, so an old [S2] would
    point at a different chunk now."""
    return re.sub(r"[ \t]+([.,;:!?])", r"\1", CITATION_GROUP_RE.sub("", text or "")).strip()


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text or "") if s and s.strip()]


def is_refusal(reply: str) -> bool:
    """Rule 4 of the answer prompt makes refusals start with a fixed phrase; normalization makes the check robust to
    curly apostrophes, leading markdown and case."""
    return normalize_reply_start(reply).startswith(REFUSAL_PREFIX)


# --- Facts -----------------------------------------------------------------------------------------------------
def _number_key(digits: str, fraction: str | None, suffix: str | None = None) -> str | None:
    try:
        value = Decimal(digits.replace(",", "") + (fraction or ""))
    except InvalidOperation:
        return None
    value *= _SCALE.get((suffix or "").lower(), 1)
    return format(value.normalize(), "f")


def _prepare(text: str) -> str:
    return unicodedata.normalize("NFKC", html.unescape(text or ""))


def _take(pattern: re.Pattern[str], text: str, keys: list[str], key_fn) -> str:
    """Collect a key for every match and blank the match out, so "$1.5M" isn't also read as clause "1.5"."""
    def repl(m: re.Match[str]) -> str:
        key = key_fn(m)
        if key is not None:
            keys.append(key)
        return " " * len(m.group(0))

    return pattern.sub(repl, text)


def extract_facts(text: str, *, min_bare: Decimal = MIN_BARE_NUMBER) -> set[str]:
    """Canonical fact keys in `text`: codes as written ("LRK-4471"), multi-level clause numbers as written ("9.2.1"),
    every other number by value ("$60k" -> "60000", "15%" -> "15", "9.2" -> "9.2"). Bare numbers below `min_bare`
    are ignored ("3 years"), but amounts, percentages and dotted numbers are always facts."""
    s = _prepare(strip_citations(text))
    keys: list[str] = []
    s = _take(_CODE_RE, s, keys, lambda m: m.group(0))
    s = _take(_AMOUNT_RE, s, keys, lambda m: _number_key(m.group(1), m.group(2), m.group(3)))
    s = _take(_PERCENT_RE, s, keys, lambda m: _number_key(m.group(1), m.group(2)))
    s = _take(_DOTTED_RE, s, keys, lambda m: m.group(0) if m.group(0).count(".") > 1 else _number_key(m.group(0), None))

    def bare(m: re.Match[str]) -> str | None:
        key = _number_key(m.group(1), m.group(2), m.group(3))
        return key if key is not None and Decimal(key) >= min_bare else None

    _take(_NUMBER_RE, s, keys, bare)
    return set(keys)


def source_facts(text: str) -> set[str]:
    """Facts a source can vouch for: everything extract_facts finds (no minimum), plus every raw number, so a code's
    digits or a date's year still ground a sentence that writes them differently."""
    raw = {_number_key(m.group(1), m.group(2), m.group(3)) for m in _NUMBER_RE.finditer(_prepare(text))}
    return extract_facts(text, min_bare=Decimal(0)) | {k for k in raw if k is not None}


def derived_facts(answer: str) -> set[str]:
    """Results of calculations shown anywhere in the answer: what follows each '=', up to the end of its clause.

    A derived value inherits its grounding from the calculation that produced it, whose inputs ARE checked. So it may
    be stated before its calculation ("20 days: 18 + 2 = 20") or reused as an input later ("$665k - $600k = $65k")
    without being mistaken for a fact the sources should contain.

    Every '=' counts, not just a sentence's last one: a bullet like "$380k + 3 × $95k = $665k, assuming ...; $665k −
    $600k = $65k" holds two results. Only the clause right after the '=' is the result ("= $665,000, valid for 1,200
    seats" derives $665,000, not 1,200), and a clause that is itself arithmetic ("= 18 + 2 = 20") is an intermediate
    step whose numbers are inputs, so it derives nothing."""
    derived: set[str] = set()
    for sentence in split_sentences(answer):
        body = strip_citations(sentence)
        for equals in re.finditer("=", body):
            rest = body[equals.end():]
            end = _RESULT_END_RE.search(rest)
            result = rest[:end.start()] if end else rest
            if not _ARITHMETIC_RE.search(result):
                derived |= extract_facts(result)
    return derived


@dataclass(frozen=True)
class DateMention:
    start: int
    end: int
    day: date               # for a month-precision mention ("January 2026"), the 1st of that month
    month_only: bool


def _date_mention(m: re.Match[str], month_only: bool) -> DateMention | None:
    month = m.group("month")
    # .get, not []: re.IGNORECASE folds Unicode, so "Auguſt" (long s) and "Aprıl" (dotless i) match the pattern but
    # aren't keys. Not a month we know: no date, and its digits are checked as numbers. A KeyError here would fail the
    # whole turn after the answer had streamed.
    month_no = int(month) if month.isdigit() else _MONTHS.get(month.lower())
    if month_no is None:
        return None
    try:
        day = date(int(m.group("year")), month_no, 1 if month_only else int(m.group("day")))
    except ValueError:              # 2026-02-30 is not a date: leave it to the number check
        return None
    return DateMention(m.start(), m.end(), day, month_only)


def date_mentions(text: str) -> list[DateMention]:
    """Every date written in one of the forms in _DATE_RES, in order, without overlaps."""
    found: list[DateMention] = []
    for pattern in _DATE_RES:
        month_only = "day" not in pattern.groupindex
        for m in pattern.finditer(text):
            if any(m.start() < f.end and f.start < m.end() for f in found):
                continue
            mention = _date_mention(m, month_only)
            if mention is not None:
                found.append(mention)
    return sorted(found, key=lambda f: f.start)


def _is_one_of(mention: DateMention, days: set[date]) -> bool:
    """A month-precision mention ("January 2026") is one of `days` when one of them falls in that month."""
    if mention.month_only:
        return any((d.year, d.month) == (mention.day.year, mention.day.month) for d in days)
    return mention.day in days


def without_shown_dates(text: str, *, document_dates: Iterable[date] = (), today: date | None = None) -> str:
    """`text` with every shown date that is presented as such blanked out, so its digits aren't checked as numbers.

    Presented as such = right after its cue (see _DATED_CUE_RE / _TODAY_CUE_RE): "the handbook dated 2026-01-15",
    "as of today, 2026-10-13". The same date stated as when something happened ("took effect on 2026-10-02", "paid on
    January 15, 2026") is a claim about the documents and is left in place, checked exactly as before (its year and day
    must appear in the cited sources): confusing the document date or today with an effective date is the mistake a
    policy-conflict question invites. Only the date itself is removed: "$600, dated 2026-01-15" still grounds the $600."""
    todays = {today} if today else set()
    cued = [(cue, days) for cue, days in ((_DATED_CUE_RE, set(document_dates)), (_TODAY_CUE_RE, todays)) if days]
    if not cued:
        return text
    for mention in reversed(date_mentions(text)):
        # endpos: the cue must END where the mention starts (its pattern is anchored with $).
        if any(_is_one_of(mention, days) and cue.search(text, 0, mention.start) for cue, days in cued):
            text = text[:mention.start] + " " * (mention.end - mention.start) + text[mention.end:]
    return text


def checked_facts(sentence: str) -> set[str]:
    """Facts that must be grounded. In a shown calculation ("$420k + 3 x $60k = $600k [S2]") the result after the
    last '=' is computed, not quoted, so only the inputs before it are checked (rule 2 of answer.v2)."""
    body = strip_citations(sentence)
    if "=" in body:
        body = body[: body.rindex("=")]
    return extract_facts(body)


# --- Snippets ----------------------------------------------------------------------------------------------------
def content_tokens(text: str) -> set[str]:
    """Lowercased words minus stopwords, with a naive plural fold (days -> day) so overlap isn't defeated by it."""
    tokens = set()
    for w in _WORD_RE.findall(normalize(text)):
        if w in STOPWORDS:
            continue
        tokens.add(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)
    return tokens


def overlap(answer_tokens: set[str], sentence: str) -> float:
    """Share of the answer's content words that appear in `sentence`."""
    if not answer_tokens:
        return 0.0
    return len(answer_tokens & content_tokens(sentence)) / len(answer_tokens)


def best_sentence(answer_tokens: set[str], source_text: str) -> tuple[str | None, float]:
    best, best_score = None, -1.0
    for sentence in split_sentences(source_text):
        score = overlap(answer_tokens, sentence)
        if score > best_score:
            best, best_score = sentence, score
    if best is None:
        return None, 0.0
    return best[:SNIPPET_MAX_CHARS], max(best_score, 0.0)


# --- The whole check -------------------------------------------------------------------------------------------
class SourceText(Protocol):
    """What the model saw for one [S#]: the breadcrumb header and the chunk content (RetrievedChunk fits).

    A source may also carry `document_date: date | None`, the date its <source> tag showed (RetrievedChunk does). It
    is read with getattr, not declared here, so a source without one still fits and simply grounds no date."""

    header: str
    content: str


@dataclass(frozen=True)
class Source:
    header: str
    content: str
    document_date: date | None = None


def _document_date(src: SourceText) -> date | None:
    return getattr(src, "document_date", None)


@dataclass(frozen=True)
class CitationCheck:
    ref: str
    snippet: str | None
    overlap: float
    weak_support: bool


@dataclass(frozen=True)
class AnswerCheck:
    citations: list[CitationCheck]      # valid refs only, in order of first appearance
    invalid_refs: list[str]             # cited but never provided (distinct)
    uncited_claims: int                 # sentences with facts and no valid citation
    ungrounded_facts: int               # facts missing from the sentence's cited sources
    answer_type: str                    # answered | not_found | ungrounded

    @property
    def valid_citations(self) -> int:
        return len(self.citations)

    @property
    def invalid_citations(self) -> int:
        return len(self.invalid_refs)


def check_answer(answer: str, sources: Mapping[str, SourceText], *, today: date | None = None) -> AnswerCheck:
    """`sources` maps each provided ref ("S1") to the chunk the model saw for it. `today` is the date the prompt's
    "Today's date" line showed (AnswerPrompt.today; None when it had none), passed in rather than recomputed so a turn
    that straddles midnight is checked against the day the model was told.

    Shown dates (each only right after its cue, see without_shown_dates): a cited sentence may state its cited
    sources' document dates and today's date. A sentence with no citation may state today's date (there is no [S#] to
    cite for it, like a sentence with no number at all), but a document date in it still makes it an uncited claim:
    that date is a fact about one particular document, and rule 3 asks the model to say which."""
    facts_by_ref = {ref: source_facts(f"{src.header}\n{src.content}") for ref, src in sources.items()}
    dates_by_ref = {ref: _document_date(src) for ref, src in sources.items()}
    refusal = is_refusal(answer)
    # A refusal may name facts it did NOT find ("no 401(k) match"); grounding them against the sources is meaningless.
    derived = derived_facts(answer)
    order: list[str] = []                       # valid refs, first appearance
    invalid: list[str] = []
    tokens_by_ref: dict[str, set[str]] = {}
    weak_refs: set[str] = set()
    uncited = ungrounded = 0

    for sentence in split_sentences(answer):
        cited = parse_refs(sentence)
        valid = [r for r in dict.fromkeys(cited) if r in sources]
        invalid.extend(r for r in cited if r not in sources and r not in invalid)
        if not valid:
            if not refusal and extract_facts(without_shown_dates(sentence, today=today)) - derived:
                uncited += 1
            continue
        allowed = set().union(*(facts_by_ref[r] for r in valid))
        shown = [dates_by_ref[r] for r in valid if dates_by_ref[r]]
        missing = set() if refusal else (
            checked_facts(without_shown_dates(sentence, document_dates=shown, today=today)) - allowed - derived)
        ungrounded += len(missing)
        for ref in valid:
            if ref not in order:
                order.append(ref)
            tokens_by_ref.setdefault(ref, set()).update(content_tokens(strip_citations(sentence)))
            if missing:
                weak_refs.add(ref)

    citations = []
    for ref in order:
        answer_tokens = tokens_by_ref.get(ref, set())
        snippet, _ = best_sentence(answer_tokens, sources[ref].content)   # what the popover shows
        # Support is measured against the WHOLE cited chunk: one source sentence can't contain every word of several
        # answer sentences that cite it (a rich, multi-bullet answer would always look "weak" otherwise).
        score = overlap(answer_tokens, f"{sources[ref].header}\n{sources[ref].content}")
        citations.append(CitationCheck(ref, snippet, round(score, 3), score < WEAK_OVERLAP or ref in weak_refs))

    if refusal:
        answer_type = "not_found"
    elif not citations:
        answer_type = "ungrounded"
    else:
        answer_type = "answered"
    return AnswerCheck(citations, invalid, uncited, ungrounded, answer_type)

