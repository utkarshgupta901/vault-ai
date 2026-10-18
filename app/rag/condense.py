"""Condense step: turn a follow-up into a standalone query for retrieval (only when there is history).

Never raises. On timeout or error the raw question is used (warning 'condense_fallback'); if the raw question is
very short ("and contractors?"), the previous turn's query is prepended so retrieval still has a topic to work with.
A failed call the provider billed (refused, truncated, unparsable, or an empty query) keeps its usage for the ledger.
"""

import asyncio
import time
from dataclasses import dataclass

import structlog

from app.ai.prompts import condense as prompt
from app.ai.provider import LLMProvider, ProviderError, Usage
from app.config import settings
from app.rag.history import History
from app.rag.tokens import count_tokens

log = structlog.get_logger(__name__)
SHORT_FOLLOWUP_WORDS = 4
MAX_OUTPUT_TOKENS = 400


@dataclass(frozen=True)
class Condensed:
    query: str                  # what retrieval searches for
    rewritten: str | None       # stored as standalone_query and shown as "Interpreted as ..." (None = unchanged)
    called: bool                # a model call was made (=> one ai_usage row)
    usage: Usage | None = None
    fallback: bool = False
    error: str | None = None
    ms: int = 0


def fallback_query(question: str, previous_query: str | None) -> str:
    if previous_query and len(question.split()) <= SHORT_FOLLOWUP_WORDS:
        return f"{previous_query} {question}"
    return question


def estimated_usage(history: History, question: str) -> Usage:
    """Usage of a condense call that was cancelled mid-flight (client gone): the provider still processes and bills
    it but its usage never reaches us, so count the input we sent locally (estimated=true)."""
    text = prompt.INSTRUCTIONS + prompt.render(history.messages, question)
    return Usage(model=settings.FAST_MODEL, input_tokens=count_tokens(text), estimated=True)


def _same(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


async def condense_question(provider: LLMProvider, history: History, question: str) -> Condensed:
    if not history.messages:
        return Condensed(query=question, rewritten=None, called=False)
    started = time.perf_counter()
    billed: Usage | None = None
    try:
        async with asyncio.timeout(settings.CONDENSE_TIMEOUT_S):
            result, usage = await provider.acomplete_json(
                model=settings.FAST_MODEL, effort=settings.FAST_EFFORT, instructions=prompt.INSTRUCTIONS,
                input=prompt.render(history.messages, question), schema=prompt.CondensedQuery,
                max_output_tokens=MAX_OUTPUT_TOKENS,
            )
        query = " ".join(result.query.split())
        if not query:
            raise ProviderError("bad_request", "Condense returned an empty query.", usage=usage)
        ms = int((time.perf_counter() - started) * 1000)
        return Condensed(query=query, rewritten=None if _same(query, question) else query, called=True,
                         usage=usage, ms=ms)
    except TimeoutError:
        error = "UPSTREAM_TIMEOUT"
    except Exception as exc:  # noqa: BLE001 - any failure degrades to the raw question
        log.warning("condense_failed", error=str(exc))
        error = "UPSTREAM_AI_ERROR"
        billed = getattr(exc, "usage", None)
    query = fallback_query(question, history.previous_query)
    return Condensed(query=query, rewritten=None if _same(query, question) else query, called=True, usage=billed,
                     fallback=True, error=error, ms=int((time.perf_counter() - started) * 1000))
