"""Document insights: one structured-output call per document, then deterministic checks in code.

Called by the Celery task `vault.generate_insights` (WP2a), which owns claiming, processing_runs and retries:

    run_insights(document_id, run_id, *, task_id, request_id)
        -> InsightsResult(timings={"load": ms, "llm": ms, "verify": ms, "write": ms}, written=True|False)

written=False means a reprocess or delete superseded the run, so nothing was stored (the task records the attempt
as SUPERSEDED, not succeeded). Raises ProviderError(kind="transient") for the task to retry, and
PermanentError(INSIGHTS_*) for failures that a retry cannot fix. The steps:
  1. load    short DB session: document + pages (skip everything if the run is already superseded)
  2. llm     page-marked prompt (sampled if the document is too long) -> DocumentInsights (structured output)
  3. verify  pure functions: risk-flag quotes must really be in the document, key-point pages must match the text,
             tags/points/questions are cleaned and capped, document_date is parsed, injection is double-checked
  4. write   ONE transaction under a row lock, fenced by run_id: upsert insights, copy title/category/tags/date
             onto the document, write the ai_usage row, set insights_status=ready

Why verify in code: the model is good at reading but not trustworthy at quoting or page numbers. A risk flag the UI
shows with a quote must be a sentence the user can find in the file, so we store the DOCUMENT's words, not the
model's, and drop flags we cannot find.

The page sampling and page-overlap helpers here are reused by summaries.py and compare.py.
"""

import re
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from functools import cache
from typing import Any

import structlog
import tiktoken
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.ai import get_provider
from app.ai.prompts import insights as prompt
from app.ai.provider import ProviderError, Usage
from app.ai.usage import usage_row
from app.config import settings
from app.core.db import sync_session
from app.core.errors import ErrorCode, PermanentError
from app.core.textnorm import normalize
from app.models import Document, DocumentInsight, DocumentPage
from app.schemas.insights import DocumentInsights
from app.services import state

log = structlog.get_logger(__name__)

MAX_OUTPUT_TOKENS = 8_000
RETRY_OUTPUT_TOKENS = 16_000         # one retry with a bigger budget when the output was truncated
MAX_TAGS, MAX_KEY_POINTS, MAX_QUESTIONS, MAX_ENTITIES = 8, 7, 5, 30
QUOTE_MIN_RATIO = 0.9                # difflib ratio between the model's quote and a window of document words
QUOTE_MIN_WORDS = 3                  # shorter "quotes" would match almost anywhere, so they prove nothing
PAGE_MIN_OVERLAP = 0.3               # share of a key point's content words that must appear on its page
WARNING_PREFIX = "risk_flags_dropped:"

# Sampling for documents over INSIGHTS_MAX_INPUT_TOKENS: the pages most likely to carry the substance.
HEAD_PAGES, TAIL_PAGES = 5, 3
KEY_TERMS = re.compile(
    r"\b(?:terminat\w*|liabilit\w*|fees?|payments?|renew\w*|risks?|revenues?|summary|conclusions?)\b", re.IGNORECASE
)

# Deterministic second opinion for injection_suspected (run on normalized, casefolded text). Each phrase is aimed
# at a machine reader; deliberately narrow so handbook lines like "employees who ignore the rules" don't trip it.
INJECTION_RE = re.compile(
    r"\b(?:ignore|disregard|forget)(?: all| any)?(?: (?:of )?(?:the|your))?"
    r"(?: previous| prior| above| earlier| preceding| system)? (?:instructions|prompts?)\b"
    r"|\b(?:ignore|disregard|forget)(?: all| any)?(?: (?:of )?(?:the|your))? (?:previous|prior|above|earlier) rules\b"
    r"|\bautomated review systems?\b"
    r"|\b(?:reply|respond|answer)(?: only)?(?: with)?(?: the word)? [\"']?approved[\"']?(?: to| for|[.!]|$)"
    r"|\b(?:note|notice|message|attention) (?:to|for) (?:any |all )?"
    r"(?:ai|llms?|language models?|automated (?:review|reviewer|system|assistant|agent)s?)\b"
    r"|\bif you are an? (?:ai|llm|language model|automated)\b"
)

_STOPWORDS = frozenset(
    "a an and are as at be been but by can could did do does for from had has have he her his how i if in into is it "
    "its may might must no not of on or our shall she should so such than that the their them then there these they "
    "this those to was we were what when where which who will with would you your".split()
)
_WORD = re.compile(r"[^\W_]+")
_PUNCT = re.compile(r"[^\w]+")


@dataclass(frozen=True)
class InsightsResult:
    timings: dict[str, int]
    written: bool       # False: superseded (new run_id) or deleted, so nothing was stored


@dataclass(frozen=True)
class _Ledger:
    """The ids every ai_usage row of this run carries (rule 7: every model call is in the cost ledger)."""

    owner_id: uuid.UUID
    document_id: uuid.UUID
    task_id: str | None
    request_id: str | None


# --- Loading ------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class DocPage:
    page_no: int
    label: str
    text: str


@dataclass
class LoadedDocument:
    owner_id: uuid.UUID
    filename: str
    pages: list[DocPage]


def _load(document_id: uuid.UUID, run_id: uuid.UUID) -> LoadedDocument | None:
    """Short session; None when the document is gone or a newer run replaced this one (spend nothing then)."""
    with sync_session() as s:
        doc = s.get(Document, document_id)
        if doc is None or doc.current_run_id != run_id:
            return None
        rows = s.execute(
            select(DocumentPage.page_no, DocumentPage.label, DocumentPage.text)
            .where(DocumentPage.document_id == document_id).order_by(DocumentPage.page_no)
        ).all()
        return LoadedDocument(doc.owner_id, doc.filename, [DocPage(r.page_no, r.label, r.text) for r in rows])


# --- Token budget and page sampling (shared with summaries and compare) ---------------------------------------
@cache
def _encoder() -> tiktoken.Encoding:
    # cl100k_base: the same tokenizer the chunker uses, so the BPE file is already cached. Counts are a budget
    # estimate (within a few % of the model's own tokenizer), which is all a 100k ceiling needs.
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_encoder().encode_ordinary(text))


def _evenly_spaced[T](items: Sequence[T], k: int) -> list[T]:
    if k <= 0 or not items:
        return []
    if k >= len(items):
        return list(items)
    step = len(items) / k
    return [items[int(i * step)] for i in range(k)]


def sample_pages(pages: Sequence[DocPage], token_counts: Sequence[int], budget: int) -> tuple[list[DocPage], str]:
    """Pick the pages to send when the document is over `budget` tokens. Returns (pages in order, coverage).

    Priority: the first 5 pages (title, parties, definitions), the last 3 (signatures, conclusions, appendices),
    pages mentioning key terms (most matches first), then evenly spaced pages so no stretch of the document is
    invisible. A page is skipped if it would overflow the budget; we never cut a page in half."""
    if sum(token_counts) <= budget:
        return list(pages), "full"
    n = len(pages)
    head = list(range(min(HEAD_PAGES, n)))
    tail = list(range(max(0, n - TAIL_PAGES), n))
    hits = [len(KEY_TERMS.findall(p.text)) for p in pages]
    keyed = sorted((i for i in range(n) if hits[i]), key=lambda i: (-hits[i], i))

    chosen: set[int] = set()
    used = 0

    def take(indexes: Iterable[int]) -> None:
        nonlocal used
        for i in indexes:
            if i not in chosen and used + token_counts[i] <= budget:
                chosen.add(i)
                used += token_counts[i]

    take(head + tail + keyed)
    rest = [i for i in range(n) if i not in chosen]
    if rest:
        avg = max(1, sum(token_counts[i] for i in rest) // len(rest))
        take(_evenly_spaced(rest, (budget - used) // avg))
    if not chosen:  # pathological: even page 1 is over budget. Send it anyway rather than nothing.
        chosen.add(0)
    return [pages[i] for i in sorted(chosen)], "sampled"


def build_prompt_pages(pages: Sequence[DocPage], budget: int | None = None) -> tuple[list[DocPage], str]:
    budget = budget if budget is not None else settings.INSIGHTS_MAX_INPUT_TOKENS
    return sample_pages(pages, [count_tokens(p.text) for p in pages], budget)


# --- The model call ------------------------------------------------------------------------------------------------
def _failure_code(exc: ProviderError) -> ErrorCode:
    """The stable code for a non-transient failure (stored as insights_error_code and ai_usage.error_code)."""
    if exc.kind == "refusal":
        return ErrorCode.INSIGHTS_REFUSED
    if exc.kind == "bad_request" and exc.status_code is not None:
        return ErrorCode.PROVIDER_BAD_REQUEST     # the API rejected the request itself (HTTP 4xx)
    return ErrorCode.INSIGHTS_TRUNCATED if exc.kind == "incomplete" else ErrorCode.INSIGHTS_PARSE_FAILED


def _record_failed_call(exc: ProviderError, code: ErrorCode, ledger: _Ledger) -> None:
    """A reply that was billed but is unusable (truncated, refused, unparsable) still gets its ai_usage row, with
    success=false, in its own short transaction. Otherwise the most expensive failure, a truncation (the whole output
    budget, reasoning included, twice), would be invisible in /metrics and in the daily quota. When the request
    itself failed (HTTP error, timeout) the provider reports no usage and there is nothing to record."""
    if exc.usage is None:
        return
    with sync_session() as s:
        s.add(usage_row(operation="insights", usage=exc.usage, owner_id=ledger.owner_id,
                        prompt_version=prompt.VERSION, document_id=ledger.document_id, success=False,
                        error_code=code, task_id=ledger.task_id, request_id=ledger.request_id))


def _call_model(prompt_input: str, ledger: _Ledger) -> tuple[DocumentInsights, Usage]:
    """One call, at most one retry. Transient errors go up to Celery (backoff lives there, not here)."""
    provider = get_provider(for_worker=True)
    budget = MAX_OUTPUT_TOKENS
    for attempt in (1, 2):
        try:
            return provider.complete_json(
                model=settings.INSIGHTS_MODEL, effort=settings.INSIGHTS_EFFORT, instructions=prompt.INSTRUCTIONS,
                input=prompt_input, schema=DocumentInsights, max_output_tokens=budget,
            )
        except ProviderError as exc:
            if exc.kind == "transient":
                raise
            code = _failure_code(exc)
            _record_failed_call(exc, code, ledger)
            # A refusal or a rejected request would get the same answer again; truncated/unparsable output gets
            # exactly one more try (with a bigger output budget when it was truncated).
            if code in (ErrorCode.INSIGHTS_REFUSED, ErrorCode.PROVIDER_BAD_REQUEST) or attempt == 2:
                # The message ends up in processing_runs (served by /metrics/processing): kind and status only. The
                # vendor's text, which for a 401 echoes part of the API key, is logged here and nowhere else.
                log.warning("insights_call_failed", kind=exc.kind, status_code=exc.status_code, error=exc.message)
                raise PermanentError(code, exc.summary()) from exc
            if exc.kind == "incomplete":
                budget = RETRY_OUTPUT_TOKENS
            log.warning("insights_retry", kind=exc.kind, max_output_tokens=budget)
    raise AssertionError("unreachable")


# --- Verification (pure functions) ---------------------------------------------------------------------------------
@dataclass(frozen=True)
class _Word:
    raw: str        # the document's own characters (for storing the verified quote)
    norm: str       # textnorm.normalize(raw): what the fuzzy comparison sees
    bare: str       # norm without punctuation: for the cheap overlap pre-filter
    page_no: int


def _words(page: DocPage) -> list[_Word]:
    """Split a page into words, re-joining words broken by a hyphenated line break ("liabil-\\nity")."""
    out: list[_Word] = []
    prev_end = 0
    for m in re.finditer(r"\S+", page.text):
        norm = normalize(m.group())
        if not norm:
            continue
        gap = page.text[prev_end:m.start()]
        prev_end = m.end()
        last = out[-1] if out else None
        if last and "\n" in gap and len(last.norm) > 1 and last.norm.endswith("-") and last.norm[-2].isalnum() \
                and norm[0].isalnum():
            norm = last.norm[:-1] + norm
            out[-1] = _Word(last.raw[:-1] + m.group(), norm, _PUNCT.sub("", norm), page.page_no)
            continue
        out.append(_Word(m.group(), norm, _PUNCT.sub("", norm), page.page_no))
    return out


def _best_window(quote_words: list[str], words: list[_Word]) -> tuple[float, int, int]:
    """Slide a window of len(quote)±1 words over `words`; return (ratio, start, end) of the best window scoring at
    least QUOTE_MIN_RATIO, or (0.0, 0, 0).

    Two cheap filters run before the character-level SequenceMatcher: windows where under half the words occur in
    the quote at all are skipped, and difflib's upper bounds (real_quick_ratio, quick_ratio) skip windows that cannot
    beat the current best. That keeps a whole-document search of a 500-page file well under a second."""
    target = " ".join(quote_words)
    quote_set = {_PUNCT.sub("", w) for w in quote_words}
    shared = [0]                                   # prefix sums: shared[j] = words[:j] that occur in the quote
    for w in words:
        shared.append(shared[-1] + (w.bare in quote_set))
    matcher = SequenceMatcher(None, autojunk=False)
    matcher.set_seq2(target)                       # difflib indexes seq2 once, so the fixed side goes there
    best = (0.0, 0, 0)
    n = len(quote_words)
    for size in (n, n - 1, n + 1):
        if size < 1 or size > len(words):
            continue
        for i in range(len(words) - size + 1):
            if (shared[i + size] - shared[i]) * 2 < size:
                continue
            matcher.set_seq1(" ".join(w.norm for w in words[i:i + size]))
            floor = max(best[0], QUOTE_MIN_RATIO)
            if matcher.real_quick_ratio() < floor or matcher.quick_ratio() < floor:
                continue
            ratio = matcher.ratio()
            if ratio >= floor and ratio > best[0]:
                best = (ratio, i, i + size)
                if ratio == 1.0:
                    return best
    return best


class QuoteFinder:
    """Locates model-supplied quotes in one document. Pages are tokenized once and reused for every flag."""

    def __init__(self, pages: Sequence[DocPage]):
        self._by_page = {p.page_no: _words(p) for p in pages}
        self._all = [w for p in pages for w in self._by_page[p.page_no]]

    def find(self, quote: str, page_hint: int | None) -> tuple[str, int] | None:
        """Returns (the document's own words, page) or None. Searches the hinted page first, then the whole
        document, so a wrong page number gets corrected and a quote that runs over a page break is still found."""
        quote_words = normalize(quote).split()
        if len(quote_words) < QUOTE_MIN_WORDS:
            return None
        searches = [self._by_page[page_hint]] if page_hint in self._by_page else []
        for words in [*searches, self._all]:
            ratio, start, end = _best_window(quote_words, words)
            if ratio >= QUOTE_MIN_RATIO:
                window = words[start:end]
                return " ".join(w.raw for w in window), window[0].page_no
        return None


def content_words(text: str) -> set[str]:
    """Words that carry meaning: no stopwords, no 1-2 letter words (numbers are kept: "60", "12" matter)."""
    return {w for w in _WORD.findall(normalize(text)) if w not in _STOPWORDS and (len(w) > 2 or w.isdigit())}


def assign_page(text: str, page_hint: int | None, page_words: dict[int, set[str]]) -> int | None:
    """Keep the model's page if the text's content words really appear there (overlap >= 0.3); otherwise move
    it to the best-matching page; otherwise null. A wrong page number is worse than none: the UI links to it."""
    words = content_words(text)
    if not words or not page_words:
        return None

    def overlap(page_no: int) -> float:
        return len(words & page_words[page_no]) / len(words)

    if page_hint is not None:
        page_hint = clamp_page(page_hint, max(page_words))
        if page_hint in page_words and overlap(page_hint) >= PAGE_MIN_OVERLAP:
            return page_hint
    best = max(sorted(page_words), key=overlap)   # sorted + max keeps the earliest page on ties
    return best if overlap(best) >= PAGE_MIN_OVERLAP else None


def clamp_page(page: int | None, page_count: int) -> int | None:
    if page is None or page_count < 1:
        return None
    return min(max(page, 1), page_count)


def _clean(s: str) -> str:
    return " ".join(s.split())


def dedupe_cap(items: Iterable[str], cap: int, *, lower: bool = False) -> list[str]:
    """Strip, collapse whitespace, optionally lowercase, drop empties and normalized duplicates, keep order, cap."""
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = _clean(item).lower() if lower else _clean(item)
        key = normalize(value)
        if value and key not in seen:
            seen.add(key)
            out.append(value)
        if len(out) == cap:
            break
    return out


def clean_tags(tags: Iterable[str]) -> list[str]:
    return dedupe_cap((t.strip().lstrip("#") for t in tags), MAX_TAGS, lower=True)


def parse_document_date(value: str | None) -> date | None:
    """ISO date or None. Accepts a datetime string by keeping its date part; anything else is dropped."""
    if not value:
        return None
    for candidate in (value.strip(), value.strip()[:10]):
        try:
            return date.fromisoformat(candidate)
        except ValueError:
            continue
    return None


def detect_injection(text: str) -> bool:
    return bool(INJECTION_RE.search(normalize(text)))


@dataclass
class Verified:
    """Post-processed insights: the values written to document_insights, plus how many flags were dropped."""

    row: dict[str, Any]
    risk_flags_dropped: int = 0


def verify(ins: DocumentInsights, pages: Sequence[DocPage]) -> Verified:
    page_count = len(pages)
    page_words = {p.page_no: content_words(p.text) for p in pages}

    finder = QuoteFinder(pages)
    flags, dropped = [], 0
    for flag in ins.risk_flags:
        found = finder.find(flag.quote, clamp_page(flag.page, page_count))
        if found is None:
            dropped += 1
            continue
        quote, page_no = found
        flags.append({"type": _clean(flag.type), "severity": flag.severity, "quote": quote, "page": page_no})

    points, seen = [], set()
    for kp in ins.key_points:
        text = _clean(kp.point)
        if not text or normalize(text) in seen:
            continue
        seen.add(normalize(text))
        points.append({"point": text, "page": assign_page(text, kp.page, page_words)})
        if len(points) == MAX_KEY_POINTS:
            break

    entities, seen_entities = [], set()
    for e in ins.entities:
        key = (normalize(e.name), e.type)
        if e.name.strip() and key not in seen_entities:
            seen_entities.add(key)
            entities.append({"name": _clean(e.name), "type": e.type})
    full_text = "\n".join(p.text for p in pages)

    row = {
        "title": _clean(ins.title)[:300] or None,
        "summary": ins.summary.strip(),
        "key_points": points,
        "document_type": _clean(ins.document_type) or None,
        "category": ins.category,
        "tags": clean_tags(ins.tags),
        "entities": entities[:MAX_ENTITIES],
        "sentiment": ins.sentiment,
        "sentiment_rationale": ins.sentiment_rationale.strip(),
        "risk_flags": flags,
        "suggested_questions": dedupe_cap(ins.suggested_questions, MAX_QUESTIONS),
        "language": _clean(ins.language).lower()[:16] or None,
        "document_date": parse_document_date(ins.document_date),
        "injection_suspected": ins.injection_suspected or detect_injection(full_text),
    }
    return Verified(row, dropped)


def merge_warnings(existing: list[Any] | None, dropped: int) -> list[Any]:
    """Replace this step's warning (a reprocess must not stack 'risk_flags_dropped' entries)."""
    kept = [w for w in existing or [] if not (isinstance(w, str) and w.startswith(WARNING_PREFIX))]
    return kept + ([f"{WARNING_PREFIX}{dropped}"] if dropped else [])


# --- Writing -------------------------------------------------------------------------------------------------------
def _write(document_id: uuid.UUID, run_id: uuid.UUID, owner_id: uuid.UUID, verified: Verified, coverage: str,
           usage: Usage, *, task_id: str | None, request_id: str | None) -> bool:
    """One transaction. The row lock (FOR UPDATE) serialises us with a concurrent reprocess: either we see the
    new run_id and stop, or the reprocess waits for our commit and then supersedes it. The model call is paid for
    either way, so the ai_usage row is written in both cases."""
    with sync_session() as s:
        s.add(usage_row(operation="insights", usage=usage, owner_id=owner_id, prompt_version=prompt.VERSION,
                        document_id=document_id, task_id=task_id, request_id=request_id))
        doc = s.execute(select(Document).where(Document.id == document_id).with_for_update()).scalar_one_or_none()
        if doc is None or doc.current_run_id != run_id or doc.insights_status != "processing":
            return False
        values = {**verified.row, "coverage": coverage, "run_id": run_id, "model": usage.model,
                  "prompt_version": prompt.VERSION}
        upsert = pg_insert(DocumentInsight).values(document_id=document_id, **values)
        s.execute(upsert.on_conflict_do_update(
            index_elements=[DocumentInsight.document_id],
            set_={**{k: upsert.excluded[k] for k in values}, "updated_at": func.now()},
        ))
        doc_fields: dict[str, Any] = {
            "category": values["category"], "tags": values["tags"], "document_date": values["document_date"],
            "warnings": merge_warnings(doc.warnings, verified.risk_flags_dropped),
        }
        if values["title"]:
            doc_fields["title"] = values["title"]
        s.execute(state.insights_ready_stmt(document_id, run_id, **doc_fields))
        return True


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def run_insights(document_id: uuid.UUID, run_id: uuid.UUID, *, task_id: str | None,
                 request_id: str | None) -> InsightsResult:
    timings: dict[str, int] = {}
    started = time.perf_counter()
    loaded = _load(document_id, run_id)
    timings["load"] = _ms(started)
    if loaded is None:
        log.info("insights_superseded", stage="load")
        return InsightsResult(timings, written=False)
    if not loaded.pages:
        raise PermanentError(ErrorCode.NO_EXTRACTABLE_TEXT, "The document has no extracted pages.")

    started = time.perf_counter()
    prompt_pages, coverage = build_prompt_pages(loaded.pages)
    ledger = _Ledger(loaded.owner_id, document_id, task_id, request_id)
    insights, usage = _call_model(prompt.render(loaded.filename, prompt_pages, coverage,
                                                page_count=len(loaded.pages)), ledger)
    timings["llm"] = _ms(started)

    started = time.perf_counter()
    verified = verify(insights, loaded.pages)
    timings["verify"] = _ms(started)

    started = time.perf_counter()
    written = _write(document_id, run_id, loaded.owner_id, verified, coverage, usage,
                     task_id=task_id, request_id=request_id)
    timings["write"] = _ms(started)
    log.info("insights_done" if written else "insights_superseded", coverage=coverage,
             pages_sent=len(prompt_pages), risk_flags_kept=len(verified.row["risk_flags"]),
             risk_flags_dropped=verified.risk_flags_dropped, **timings)
    return InsightsResult(timings, written=written)
