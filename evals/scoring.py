"""Pure scoring functions for the eval (no I/O, no HTTP): text matching, retrieval metrics, answer checks, refusal
precision/recall, the reliability table and latency percentiles. Kept pure so every number in a report can be
unit-tested and re-derived by hand.

Matching rules (one matcher for any_of, all_of, forbid and gold_chunk):
  - both sides are normalized: NFKC, straight quotes/dashes, casefold, collapsed whitespace (app.core.textnorm), plus
    zero-width characters, markdown emphasis and [S1]-style citation markers removed and thousands separators
    dropped, so "**$120,000** [S1]" matches "$120000";
  - then split into tokens: words and numbers ("4.2m", "99.9", "2026-10-01", "aren't" are one token each), symbols
    ("$", "%", "=") and punctuation. A phrase matches as a token sequence, so "18 days" does not match "118 days",
    "1.5 days" or "15-18 days", and "eligible" does not match "ineligible";
  - up to MAX_FILLERS plain words may sit between two words of a phrase ("are explicitly excluded" matches "are
    excluded"), never a number, a negation or punctuation the phrase doesn't have;
  - in an answer, an occurrence does not count when it is negated ("the handbook does not say contractors are
    excluded") or sits in a question ("are they not eligible?"). A phrase with its own negation ("not eligible")
    matches normally. Retrieved chunks skip this guard: they are the documents' text, not claims.
Golden labels still list the variants they accept ("instruction", "instructions"): this is matching, not meaning.
"""

import math
import re
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from functools import lru_cache
from itertools import takewhile
from typing import Any

from app.core.textnorm import normalize as base_normalize
from app.rag.citations import CITATION_GROUP_RE

# How many extra words may sit between two words of an expected phrase. Answers add adverbs and adjectives that
# change nothing ("are explicitly excluded", "18 PTO days", "up to five (5)"); two covers those, while a longer gap
# usually means a different statement. The gap words must be plain words: a number ("20 to 25 days") or a negation
# ("are not excluded") in the gap means the phrase is not what the answer says.
MAX_FILLERS = 2
# A negation this many tokens before an occurrence, in the same clause, flips it ("were never excluded", "does not say
# contractors are excluded"). Small on purpose: "employees with no prior service get 18 days" must still count.
NEGATION_WINDOW = 3

NEGATORS = frozenset({"not", "no", "never", "neither", "nor", "without", "cannot"})      # plus any "...n't"
# After a negator these make a bound, not a negation: "will not exceed $2,000,000", "no later than October 1".
BOUND_WORDS = frozenset({"more", "less", "fewer", "greater", "later", "earlier", "longer", "shorter", "sooner",
                         "exceed", "exceeds", "exceeding", "above", "below", "over", "under", "beyond", "before",
                         "after", "until", "only"})
# After a negator these negate the rest of the clause however far it runs: "does not state anywhere in section 4
# that ...", "it isn't true that ...", "it is not the case that ...".
CLAUSE_NEGATING = frozenset({"say", "says", "said", "state", "states", "stated", "mention", "mentions", "mentioned",
                             "specify", "specifies", "specified", "indicate", "indicates", "indicated", "confirm",
                             "confirms", "confirmed", "true", "case", "clear"})
# These start a new clause, like punctuation does: "employees are not excluded, but contractors are excluded".
CONJUNCTIONS = frozenset({"but", "however", "whereas", "although", "though", "while"})

# Zero-width and soft-hyphen characters: a zero-width space separates words, the others sit inside a word.
_INVISIBLE = str.maketrans({"\u200b": " ", "\u200c": None, "\u200d": None, "\u2060": None, "\ufeff": None,
                            "\u00ad": None})
_EM_DASH = re.compile(r"\s*\u2014\s*")       # an em dash separates clauses; textnorm would make it a hyphen
_MARKDOWN = re.compile(r"[*_`~]+")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_WS = re.compile(r"\s+")
# A word or number, with joiners inside it: digit-punctuation-digit ("4.2", "2026-10-01", "10/1/2026", "15-18",
# "10:00") and letter-apostrophe-letter ("aren't"). Anything else is one character per token.
_TOKEN = re.compile(r"[^\W_]+(?:(?:(?<=\d)[.,/:\-](?=\d)|(?<=[^\W\d_])'(?=[^\W\d_]))[^\W_]+)*|\S")
_BREAKS = frozenset(".,;:!?")                  # clause punctuation (a spaced "-" too: bullets, dashes)
_TRANSPARENT = frozenset("\"'«»()[]{}<>#|")    # quotes, brackets, markdown leftovers: ignored entirely


def normalize(text: str | None) -> str:
    s = _EM_DASH.sub(" - ", (text or "").translate(_INVISIBLE))
    s = CITATION_GROUP_RE.sub(" ", base_normalize(s))
    s = _THOUSANDS.sub("", _MARKDOWN.sub("", s))
    return _WS.sub(" ", s).strip()


@dataclass(frozen=True)
class _Tok:
    text: str
    kind: str          # "word" (letters/digits) or "symbol" ("$", "%", "=", a hyphen inside "24-month")
    start: int         # offsets in the normalized text, for reporting what matched
    end: int
    pause: str         # the clause punctuation between the previous token and this one ("" = none)
    clause: int        # increases at every pause and conjunction


@dataclass(frozen=True)
class _Text:
    normalized: str
    tokens: tuple[_Tok, ...]
    tail: str          # punctuation after the last token (a final "?")


@lru_cache(maxsize=4096)
def _tokenize(text: str) -> _Text:
    s = normalize(text)
    tokens: list[_Tok] = []
    pause, clause = "", 0
    for m in _TOKEN.finditer(s):
        t = m.group()
        if t in _TRANSPARENT:
            continue
        tight = 0 < m.start() and m.end() < len(s) and not s[m.start() - 1].isspace() and not s[m.end()].isspace()
        if t in _BREAKS or (t == "-" and not tight):
            pause += t
            continue
        if pause or t in CONJUNCTIONS:
            clause += 1
        kind = "word" if t[0].isalnum() else "symbol"
        tokens.append(_Tok(t, kind, m.start(), m.end(), pause, clause))
        pause = ""
    return _Text(s, tuple(tokens), pause)


def _same(token: str, wanted: str) -> bool:
    """Whole tokens only: "10" never matches inside "110", "10.5" or "15-10"."""
    return token == wanted


def _is_negator(word: str) -> bool:
    return word in NEGATORS or word.endswith("n't")


def _boundary_before(tok: _Tok) -> bool:
    return bool(tok.pause) or tok.text in CONJUNCTIONS


def _is_filler(tok: _Tok) -> bool:
    return (tok.kind == "word" and not any(c.isdigit() for c in tok.text) and not _is_negator(tok.text)
            and tok.text not in CONJUNCTIONS)


def _extend(hay: tuple[_Tok, ...], phrase: tuple[_Tok, ...], at: int, j: int) -> int | None:
    """Index of the token that ends the phrase when phrase[j:] can follow hay[at], else None. Tries every allowed gap
    (backtracking), since the first place the next word fits is not always the one the rest of the phrase fits."""
    if j == len(phrase):
        return at
    gap = MAX_FILLERS if phrase[j - 1].kind == phrase[j].kind == "word" else 0
    for k in range(at + 1, min(at + 2 + gap, len(hay))):
        if _boundary_before(hay[k]) and not _boundary_before(phrase[j]):
            return None                        # the gap would cross punctuation (or a "but") the phrase doesn't have
        if _same(hay[k].text, phrase[j].text):
            end = _extend(hay, phrase, k, j + 1)
            if end is not None:
                return end
        if not _is_filler(hay[k]):
            return None
    return None


def _spans(hay: tuple[_Tok, ...], phrase: tuple[_Tok, ...]) -> Iterator[tuple[int, int]]:
    for i, tok in enumerate(hay):
        if _same(tok.text, phrase[0].text):
            end = _extend(hay, phrase, i, 1)
            if end is not None:
                yield i, end


def _negates(hay: tuple[_Tok, ...], n: int) -> bool:
    return _is_negator(hay[n].text) and not (n + 1 < len(hay) and hay[n + 1].text in BOUND_WORDS)


def _pause_after(text: _Text, last: int) -> str:
    """The first clause punctuation after token `last` (the end of the text counts)."""
    return next((t.pause for t in text.tokens[last + 1:] if t.pause), text.tail)


def _rejection(text: _Text, first: int, last: int) -> str | None:
    """Why an occurrence in an answer does not count as the answer saying it, or None when it does."""
    hay = text.tokens
    before = list(takewhile(lambda n: hay[n].clause == hay[first].clause, range(first - 1, -1, -1)))  # nearest first
    for n in before:
        reporting = [t.text for t in hay[n + 1:min(n + 3, first)] if t.text in CLAUSE_NEGATING]
        if _is_negator(hay[n].text) and reporting:
            return f"negated by {hay[n].text!r} + {reporting[0]!r}"
    for n in before[:NEGATION_WINDOW]:
        if _negates(hay, n):
            return f"negated by {hay[n].text!r}"
    if "?" in _pause_after(text, last):
        return "in a question"
    return None


@dataclass(frozen=True)
class Occurrence:
    phrase: str
    text: str                   # the matched words of the normalized haystack, e.g. "are explicitly excluded"
    start: int
    rejected: str | None = None  # why it does not count ("negated by 'not'", "in a question"); None = it counts

    def as_dict(self) -> dict[str, str]:
        if self.rejected is None:
            return {"phrase": self.phrase, "matched": self.text}
        return {"phrase": self.phrase, "rejected": self.rejected, "text": self.text}


def occurrences(haystack: str, phrase: str, *, assertive: bool = True) -> list[Occurrence]:
    """Every place `phrase` occurs in `haystack`. assertive=True (answers): each says whether it counts as the answer
    stating the phrase. assertive=False (retrieved chunks): every occurrence counts."""
    text, wanted = _tokenize(haystack or ""), _tokenize(phrase or "").tokens
    if not wanted:
        return []
    found = []
    for first, last in _spans(text.tokens, wanted):
        span = text.normalized[text.tokens[first].start:text.tokens[last].end]
        found.append(Occurrence(phrase, span, text.tokens[first].start,
                                _rejection(text, first, last) if assertive else None))
    return found


def find(haystack: str, phrase: str, *, assertive: bool = True) -> Occurrence | None:
    """The first occurrence that counts, or None."""
    return next((o for o in occurrences(haystack, phrase, assertive=assertive) if o.rejected is None), None)


def contains(haystack: str, needle: str, *, assertive: bool = True) -> bool:
    return find(haystack, needle, assertive=assertive) is not None


def matches_any(text: str, options: Iterable[str], *, assertive: bool = True) -> bool:
    return any(contains(text, o, assertive=assertive) for o in options)


def evidence(answer: str, phrases: Iterable[str]) -> list[dict[str, str]]:
    """For the report: each phrase that occurs, with the words that matched, or why no occurrence counted. Phrases
    that don't occur at all are left out (the failed check already says so)."""
    out = []
    for phrase in dict.fromkeys(phrases):
        found = occurrences(answer, phrase)
        if found:
            out.append(next((o for o in found if o.rejected is None), found[0]).as_dict())
    return out


def _as_group(entry: str | Sequence[str]) -> list[str]:
    return [entry] if isinstance(entry, str) else list(entry)


# ----------------------------------------------------------------------------------------------------------------
# Answer expectations
# ----------------------------------------------------------------------------------------------------------------
def check_any_of(answer: str, options: Sequence[str] | None) -> bool | None:
    """At least one acceptable phrase is present. None = no expectation."""
    return None if not options else matches_any(answer, options)


def check_all_of(answer: str, groups: Sequence[str | Sequence[str]] | None) -> bool | None:
    """Every entry must be present; an entry that is a list is satisfied by any one of its alternatives."""
    return None if not groups else all(matches_any(answer, _as_group(g)) for g in groups)


def check_forbid(answer: str, forbidden: Sequence[str] | None) -> bool | None:
    """True when the answer states none of the forbidden phrases (a negated mention is not stating it)."""
    return None if not forbidden else not matches_any(answer, forbidden)


def check_forbid_start(answer: str, prefixes: Sequence[str] | None) -> bool | None:
    """True when the reply does not START with any of the prefixes (e.g. an injected 'APPROVED'). Quoting the word
    later in the answer is fine, which is why this is separate from `forbid`."""
    if not prefixes:
        return None
    start = normalize(answer).lstrip(" >#-:.\"'")
    return not any(start.startswith(normalize(p)) for p in prefixes)


def _filenames(must_cite: str | Sequence[str] | None) -> list[str]:
    if not must_cite:
        return []
    return [must_cite] if isinstance(must_cite, str) else list(must_cite)


def check_gold_citation(citations: Sequence[dict[str, Any]], must_cite: str | Sequence[str] | None) -> bool | None:
    """Every required filename is among the cited documents."""
    required = _filenames(must_cite)
    if not required:
        return None
    cited = {c.get("filename") for c in citations}
    return all(name in cited for name in required)


# The server signals the checks and the refusal split read (app/schemas/chat.Signals always sends all of them).
SCORED_SIGNALS = ("valid_citations", "invalid_citations", "ungrounded_facts", "gate")


def missing_signals(answer: dict[str, Any]) -> list[str]:
    """Which of SCORED_SIGNALS the answer doesn't carry (all of them when `signals` itself is absent)."""
    signals = answer.get("signals")
    if not isinstance(signals, dict):
        return list(SCORED_SIGNALS)
    return [name for name in SCORED_SIGNALS if name not in signals]


def _signal(answer: dict[str, Any], name: str) -> Any:
    """One server-side signal, or None when the answer doesn't carry it. Never a default of 0: a missing or renamed
    `signals` would then read as "0 invalid citations, 0 ungrounded facts" and pass checks nothing ran."""
    return (answer.get("signals") or {}).get(name)


def _count_is(answer: dict[str, Any], name: str, test: Callable[[int], bool]) -> bool:
    """`test` on a count signal; False when the signal is missing, so a check that gates on it fails closed."""
    value = _signal(answer, name)
    return value is not None and test(int(value))


def model_generated(answer: dict[str, Any]) -> bool:
    """False for a gate refusal: the server replied without calling the answer model (signals.gate)."""
    return not _signal(answer, "gate")


def refusal_path(answer: dict[str, Any]) -> str | None:
    """Which layer refused: 'gate' (no LLM call: low similarity, no identifier hit) or 'model' (the LLM said
    'I couldn't find this...'). None when the answer is not a refusal."""
    if answer.get("answer_type") != "not_found":
        return None
    return "gate" if _signal(answer, "gate") else "model"


def score_answer(expect: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
    """Run every applicable check on one answer (an AskResponse.answer / MessageOut dict).

    Checks that decide `passed`: any_of, all_of, forbid, forbid_start, not_found, gold_citation, citations_valid,
    plus min_citations and grounded when the case asks for them. `grounded` is always reported, but only gates the
    pass when expect.grounded is true (number grounding is a heuristic, so it is a metric first). The last three
    read server signals: a signal the answer doesn't carry fails its check instead of passing it vacuously.
    `evidence` records, per phrase check, which words matched each phrase or why an occurrence didn't count."""
    content = answer.get("content") or ""
    citations = answer.get("citations") or []
    expected_not_found = bool(expect.get("not_found", False))
    checks: dict[str, bool | None] = {
        "any_of": check_any_of(content, expect.get("any_of")),
        "all_of": check_all_of(content, expect.get("all_of")),
        "forbid": check_forbid(content, expect.get("forbid")),
        "forbid_start": check_forbid_start(content, expect.get("forbid_start")),
        "not_found": (answer.get("answer_type") == "not_found") == expected_not_found,
        "gold_citation": check_gold_citation(citations, expect.get("must_cite")),
        "citations_valid": _count_is(answer, "invalid_citations", lambda n: n == 0),
        "min_citations": (_count_is(answer, "valid_citations", lambda n: n >= int(expect["min_citations"]))
                          if expect.get("min_citations") else None),
        "grounded": _count_is(answer, "ungrounded_facts", lambda n: n == 0),
    }
    gating = [k for k in checks if k != "grounded"] + (["grounded"] if expect.get("grounded") else [])
    failed = [k for k in gating if checks[k] is False]
    phrases = {"any_of": expect.get("any_of") or [],
               "all_of": [p for g in expect.get("all_of") or [] for p in _as_group(g)],
               "forbid": expect.get("forbid") or []}
    found = {key: evidence(content, ps) for key, ps in phrases.items() if ps}
    return {"passed": not failed, "failed": failed, "checks": checks, "evidence": found}


# ----------------------------------------------------------------------------------------------------------------
# Retrieval metrics
# ----------------------------------------------------------------------------------------------------------------
def is_relevant(content: str, gold: Sequence[str]) -> bool:
    """A retrieved chunk is relevant when it contains any of the case's gold phrases. Same matcher as answers, minus
    the negation guard: a chunk is the document's own text, so "does not carry over" there IS the passage."""
    return matches_any(content, gold, assertive=False)


def first_relevant_rank(contents: Sequence[str], gold: Sequence[str]) -> int | None:
    """1-based rank of the first relevant chunk, or None if no retrieved chunk is relevant."""
    for rank, content in enumerate(contents, start=1):
        if is_relevant(content, gold):
            return rank
    return None


def hit_at_k(ranks: Sequence[int | None], k: int) -> int:
    """How many cases had a relevant chunk in the top k."""
    return sum(1 for r in ranks if r is not None and r <= k)


def mrr(ranks: Sequence[int | None]) -> float | None:
    """Mean reciprocal rank: 1/rank of the first relevant chunk (0 when missed), averaged over cases."""
    if not ranks:
        return None
    return sum(1.0 / r if r else 0.0 for r in ranks) / len(ranks)


def retrieval_metrics(ranks: Sequence[int | None], ks: Sequence[int] = (1, 5, 8)) -> dict[str, Any]:
    """Raw counts and rates, so a report never shows '100%' without saying '3/3'."""
    n = len(ranks)
    out: dict[str, Any] = {"n": n, "mrr": mrr(ranks)}
    for k in ks:
        hits = hit_at_k(ranks, k)
        out[f"hit@{k}"] = {"hits": hits, "n": n, "rate": hits / n if n else None}
    return out


# ----------------------------------------------------------------------------------------------------------------
# Refusals, reliability, stability, latency
# ----------------------------------------------------------------------------------------------------------------
def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def refusal_stats(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Precision/recall of refusing, overall and split by the layer that refused.

    Each record: {"expected": bool (should refuse), "predicted": bool (did refuse), "path": "gate"|"model"|None,
    "expected_path": "gate"|"model"|None}.
      precision = correct refusals / all refusals   (refusing answerable questions hurts precision)
      recall    = correct refusals / all should-refuse cases   (answering unanswerable ones hurts recall)
    Per path, `recall` is that layer's share of the should-refuse cases, so gate + model recall = overall recall."""
    positives = sum(1 for r in records if r["expected"])
    tp = sum(1 for r in records if r["expected"] and r["predicted"])
    fp = sum(1 for r in records if not r["expected"] and r["predicted"])
    fn = positives - tp
    by_path: dict[str, Any] = {}
    for path in ("gate", "model"):
        refused = [r for r in records if r["predicted"] and r.get("path") == path]
        correct = sum(1 for r in refused if r["expected"])
        by_path[path] = {"refusals": len(refused), "correct": correct, "false_refusals": len(refused) - correct,
                         "precision": _ratio(correct, len(refused)), "recall": _ratio(correct, positives)}
    with_expectation = [r for r in records if r["expected"] and r.get("expected_path")]
    path_ok = sum(1 for r in with_expectation if r.get("path") == r["expected_path"])
    return {"should_refuse": positives, "refused": tp + fp, "tp": tp, "fp": fp, "fn": fn,
            "precision": _ratio(tp, tp + fp), "recall": _ratio(tp, positives), "by_path": by_path,
            "expected_path": {"matched": path_ok, "n": len(with_expectation)}}


CONFIDENCE_ORDER = ("high", "medium", "low")


def reliability_table(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Accuracy per confidence badge. If 'high' isn't clearly more accurate than 'medium', the badge is noise.
    Each record: {"confidence": "high"|"medium"|"low"|None, "passed": bool}."""
    rows = []
    for bucket in (*CONFIDENCE_ORDER, None):
        hits = [r for r in records if r.get("confidence") == bucket]
        if bucket is None and not hits:
            continue
        correct = sum(1 for r in hits if r["passed"])
        rows.append({"confidence": bucket or "none", "n": len(hits), "correct": correct,
                     "accuracy": _ratio(correct, len(hits))})
    return rows


# ----------------------------------------------------------------------------------------------------------------
# LLM judge agreement (opt-in, never gates)
# ----------------------------------------------------------------------------------------------------------------
# The checks about WHAT the answer says. The judge grades that against the golden facts; it doesn't see citation ids
# or grounding signals, so citations_valid, gold_citation, min_citations and grounded stay out of the comparison.
CONTENT_CHECKS = ("any_of", "all_of", "forbid", "forbid_start", "not_found")


def content_verdict(checks: dict[str, bool | None]) -> bool:
    return not any(checks.get(name) is False for name in CONTENT_CHECKS)


def judge_agreement(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Agreement between the code's content verdict and the judge's. Each row: {"id", "set": "core"|"hard", "code":
    bool, "judge": bool | None (None = the judge call failed), "model"}. Disagreements are the point: "code fail,
    judge pass" is a candidate false fail of the phrase labels, "code pass, judge fail" a candidate false pass."""
    judged = [r for r in rows if r["judge"] is not None]
    agree = [r for r in judged if r["code"] == r["judge"]]
    sets = dict.fromkeys(r["set"] for r in rows)
    return {
        "judged": len(judged), "agree": len(agree), "disagree": len(judged) - len(agree),
        "rate": _ratio(len(agree), len(judged)),
        "code_pass_judge_fail": [r["id"] for r in judged if r["code"] and not r["judge"]],
        "code_fail_judge_pass": [r["id"] for r in judged if not r["code"] and r["judge"]],
        "by_set": {s: {"judged": sum(1 for r in judged if r["set"] == s),
                       "agree": sum(1 for r in agree if r["set"] == s)} for s in sets},
        "models": dict(Counter(r.get("model") or "unknown" for r in judged)),
        "errors": [r["id"] for r in rows if r["judge"] is None],
    }


def stability(passes: Sequence[bool]) -> dict[str, Any]:
    """pass^N for repeated runs: a demo case is stable only if it passed every time; flaky if it passed sometimes."""
    n, ok = len(passes), sum(1 for p in passes if p)
    return {"passes": ok, "runs": n, "stable": n > 0 and ok == n, "flaky": 0 < ok < n}


def percentile(values: Sequence[float], p: float) -> float | None:
    """Nearest-rank percentile (no interpolation): the value at rank ceil(p/100 * n). Easy to verify by hand."""
    data = sorted(v for v in values if v is not None)
    if not data:
        return None
    rank = max(1, math.ceil(p / 100 * len(data)))
    return float(data[rank - 1])


def latency_summary(values: Sequence[float | None]) -> dict[str, float | None]:
    """p50/p95 plus the max: with ~40 answers p95 skips the slowest one or two, so an outlier (a cold start) that
    a user would still feel only shows up in `max`."""
    present = [float(v) for v in values if v is not None]
    return {"n": len(present), "p50": percentile(present, 50), "p95": percentile(present, 95),
            "avg": sum(present) / len(present) if present else None, "max": max(present) if present else None}
