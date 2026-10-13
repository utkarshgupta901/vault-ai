"""Deterministic answer checks: parse [S#] citations, verify that the facts in each sentence appear in the sources that
sentence cites, and pick a supporting snippet per citation.

No second LLM call: these checks are cheap, run on every answer, and are explainable line by line. They catch the
failures that matter most in a document assistant — a citation id that was never provided, a number or code that
isn't in the cited source, a factual sentence with no citation at all. Claim-level faithfulness ("is this paraphrase
true?") is left to the offline LLM judge.

Facts are the things a model gets subtly wrong: amounts, percentages, codes (LRK-4471), clause numbers (9.2) and
numbers >= 10. Units are normalized before comparing, so "$60k", "$60,000" and "60000" are the same fact.
"""

import html
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol

from app.core.textnorm import normalize, normalize_reply_start
from app.rag.query_terms import STOPWORDS

REFUSAL_PREFIX = "i couldn't find this in your documents"
WEAK_OVERLAP = 0.2              # below this, the best snippet barely shares words with the answer
MIN_BARE_NUMBER = Decimal(10)   # "3 years" is not checked; "18 days" and "2026" are
SNIPPET_MAX_CHARS = 300

CITATION_GROUP_RE = re.compile(r"\[(S\d+(?:\s*[,;]\s*S?\d+)*)\]")   # [S1]  [S1, S2]  [S1; 3]
_REF_NUMBER_RE = re.compile(r"\d+")

# One sentence per line item; inside a line, split after . ! ? — but not when a citation follows ("days. [S1]"
# belongs to the sentence before it) — and after a citation group that is followed by a new capitalised sentence.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?![\s\[])|(?<=\])\s+(?=[A-Z])|\n+")

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


# --- Citation parsing ------------------------------------------------------------------------------------------
def parse_refs(text: str) -> list[str]:
    """All cited refs in order of appearance, normalized to "S<n>" ([S1, 2] -> S1, S2). Duplicates kept."""
    refs: list[str] = []
    for group in CITATION_GROUP_RE.finditer(text or ""):
        refs.extend(f"S{int(n)}" for n in _REF_NUMBER_RE.findall(group.group(1)))
    return refs


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
    """Results of calculations shown anywhere in the answer (the part after the last '=' of each sentence).

    A derived value inherits its grounding from the calculation that produced it, whose inputs ARE checked. So it may
    be stated before its calculation ("20 days: 18 + 2 = 20") or reused as an input later ("$665k - $600k = $65k")
    without being mistaken for a fact the sources should contain."""
    derived: set[str] = set()
    for sentence in split_sentences(answer):
        body = strip_citations(sentence)
        if "=" in body:
            derived |= extract_facts(body[body.rindex("=") + 1:])
    return derived


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
    """What the model saw for one [S#]: the breadcrumb header and the chunk content (RetrievedChunk fits)."""

    header: str
    content: str


@dataclass(frozen=True)
class Source:
    header: str
    content: str


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


def check_answer(answer: str, sources: Mapping[str, SourceText]) -> AnswerCheck:
    """`sources` maps each provided ref ("S1") to the chunk the model saw for it."""
    facts_by_ref = {ref: source_facts(f"{src.header}\n{src.content}") for ref, src in sources.items()}
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
            if not refusal and extract_facts(sentence) - derived:
                uncited += 1
            continue
        allowed = set().union(*(facts_by_ref[r] for r in valid))
        missing = set() if refusal else checked_facts(sentence) - allowed - derived
        ungrounded += len(missing)
        for ref in valid:
            if ref not in order:
                order.append(ref)
            tokens_by_ref.setdefault(ref, set()).update(content_tokens(strip_citations(sentence)))
            if missing:
                weak_refs.add(ref)

    citations = []
    for ref in order:
        snippet, score = best_sentence(tokens_by_ref.get(ref, set()), sources[ref].content)
        citations.append(CitationCheck(ref, snippet, round(score, 3), score < WEAK_OVERLAP or ref in weak_refs))

    if refusal:
        answer_type = "not_found"
    elif not citations:
        answer_type = "ungrounded"
    else:
        answer_type = "answered"
    return AnswerCheck(citations, invalid, uncited, ungrounded, answer_type)

