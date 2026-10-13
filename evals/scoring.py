"""Pure scoring functions for the eval (no I/O, no HTTP): text matching, retrieval metrics, answer checks, refusal
precision/recall, the reliability table and latency percentiles. Kept pure so every number in a report can be
unit-tested and re-derived by hand.

Matching rules (the same for answers and for retrieved chunks):
  - both sides are normalized: NFKC, straight quotes/dashes, casefold, collapsed whitespace (app.core.textnorm), plus
    markdown emphasis removed and thousands separators dropped, so "**$120,000**" matches "$120000";
  - a needle matches only as a whole phrase: "18 days" does not match "118 days" or "1.18 days", "eligible" does not
    match "ineligible". Golden labels therefore list the variants they accept ("instruction", "instructions").
"""

import math
import re
from collections.abc import Iterable, Sequence
from typing import Any

from app.core.textnorm import normalize as base_normalize

_MARKDOWN = re.compile(r"[*_`]+")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")


def normalize(text: str | None) -> str:
    s = base_normalize(text)
    s = _MARKDOWN.sub("", s)
    return _THOUSANDS.sub("", s)


def contains(haystack: str, needle: str) -> bool:
    """Whole-phrase match of `needle` in `haystack` after normalizing both. Numbers are whole numbers too: "5 days"
    does not match inside "1.5 days" and "4.2" does not match inside "4.25"."""
    n = normalize(needle)
    if not n:
        return False
    left = r"(?<![a-z0-9])(?<![0-9][.,])" if n[0].isalnum() else ""
    right = r"(?![a-z0-9])(?![.,][0-9])" if n[-1].isalnum() else ""
    return re.search(left + re.escape(n) + right, normalize(haystack)) is not None


def matches_any(text: str, options: Iterable[str]) -> bool:
    return any(contains(text, o) for o in options)


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
    """True when none of the forbidden phrases appear."""
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


def _signal(answer: dict[str, Any], name: str, default: Any = 0) -> Any:
    return (answer.get("signals") or {}).get(name, default)


def refusal_path(answer: dict[str, Any]) -> str | None:
    """Which layer refused: 'gate' (no LLM call: low similarity, no identifier hit) or 'model' (the LLM said
    'I couldn't find this...'). None when the answer is not a refusal."""
    if answer.get("answer_type") != "not_found":
        return None
    return "gate" if _signal(answer, "gate", False) else "model"


def score_answer(expect: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
    """Run every applicable check on one answer (an AskResponse.answer / MessageOut dict).

    Checks that decide `passed`: any_of, all_of, forbid, forbid_start, not_found, gold_citation, citations_valid,
    plus min_citations and grounded when the case asks for them. `grounded` is always reported, but only gates the
    pass when expect.grounded is true (number grounding is a heuristic, so it is a metric first)."""
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
        "citations_valid": int(_signal(answer, "invalid_citations", 0)) == 0,
        "min_citations": (int(_signal(answer, "valid_citations", len(citations))) >= int(expect["min_citations"])
                          if expect.get("min_citations") else None),
        "grounded": int(_signal(answer, "ungrounded_facts", 0)) == 0,
    }
    gating = [k for k in checks if k != "grounded"] + (["grounded"] if expect.get("grounded") else [])
    failed = [k for k in gating if checks[k] is False]
    return {"passed": not failed, "failed": failed, "checks": checks}


# ----------------------------------------------------------------------------------------------------------------
# Retrieval metrics
# ----------------------------------------------------------------------------------------------------------------
def is_relevant(content: str, gold: Sequence[str]) -> bool:
    """A retrieved chunk is relevant when it contains any of the case's gold substrings."""
    return matches_any(content, gold)


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
    present = [float(v) for v in values if v is not None]
    return {"n": len(present), "p50": percentile(present, 50), "p95": percentile(present, 95),
            "avg": sum(present) / len(present) if present else None}
