"""Follow-up suggestions: best effort, after the answer is already saved.

Never raises and never delays the answer: it runs after `citations` (and the DB save), under FOLLOWUPS_TIMEOUT_S, and
on any failure the chips are simply omitted. A failed call the provider billed (refused, truncated, unparsable) keeps
its usage for the ledger.
"""

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass

import structlog

from app.ai.prompts import followups as prompt
from app.ai.provider import LLMProvider, Usage
from app.config import settings
from app.rag.retrieval import RetrievedChunk
from app.rag.tokens import count_tokens

log = structlog.get_logger(__name__)
MAX_EXCERPTS = 6
MAX_QUESTION_CHARS = 200
MAX_OUTPUT_TOKENS = 400


@dataclass(frozen=True)
class Followups:
    questions: list[str]
    usage: Usage | None = None
    error: str | None = None
    ms: int = 0


def pick_excerpts(chunks: Sequence[RetrievedChunk], cited_chunk_ids: set[int]) -> list[prompt.Excerpt]:
    """Retrieved-but-uncited chunks first (they lead somewhere new), then the cited ones."""
    ordered = [c for c in chunks if c.chunk_id not in cited_chunk_ids] + \
              [c for c in chunks if c.chunk_id in cited_chunk_ids]
    return [prompt.Excerpt(header=c.header, content=c.content) for c in ordered[:MAX_EXCERPTS]]


def render_input(question: str, answer: str, chunks: Sequence[RetrievedChunk], cited_chunk_ids: set[int]) -> str:
    return prompt.render(question=question, answer=answer, excerpts=pick_excerpts(chunks, cited_chunk_ids))


def estimated_usage(question: str, answer: str, chunks: Sequence[RetrievedChunk], cited_chunk_ids: set[int]) -> Usage:
    """Usage of a follow-ups call that was cancelled mid-flight (client gone): the provider still processes and bills
    it but its usage never reaches us, so count the input we sent locally (estimated=true)."""
    text = prompt.INSTRUCTIONS + render_input(question, answer, chunks, cited_chunk_ids)
    return Usage(model=settings.FAST_MODEL, input_tokens=count_tokens(text), estimated=True)


def clean_questions(raw: Sequence[str], asked: str) -> list[str]:
    seen = {asked.strip().casefold()}
    out: list[str] = []
    for q in raw:
        q = " ".join((q or "").split()).lstrip("-*0123456789. ")[:MAX_QUESTION_CHARS]
        if q and q.casefold() not in seen:
            seen.add(q.casefold())
            out.append(q)
    return out[:3]


async def suggest_followups(
    provider: LLMProvider, *, question: str, answer: str, chunks: Sequence[RetrievedChunk], cited_chunk_ids: set[int]
) -> Followups:
    if not chunks:
        return Followups([])
    started = time.perf_counter()
    billed: Usage | None = None
    try:
        async with asyncio.timeout(settings.FOLLOWUPS_TIMEOUT_S):
            result, usage = await provider.acomplete_json(
                model=settings.FAST_MODEL, effort=settings.FAST_EFFORT, instructions=prompt.INSTRUCTIONS,
                input=render_input(question, answer, chunks, cited_chunk_ids),
                schema=prompt.FollowupQuestions, max_output_tokens=MAX_OUTPUT_TOKENS,
            )
        return Followups(clean_questions(result.questions, question), usage,
                         ms=int((time.perf_counter() - started) * 1000))
    except TimeoutError:
        error = "UPSTREAM_TIMEOUT"
    except Exception as exc:  # noqa: BLE001 - best effort
        log.warning("followups_failed", error=str(exc))
        error = "UPSTREAM_AI_ERROR"
        billed = getattr(exc, "usage", None)
    return Followups([], billed, error, int((time.perf_counter() - started) * 1000))
