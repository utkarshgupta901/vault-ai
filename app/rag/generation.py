"""Stream the answer with two time budgets and one fallback.

  - Time to first token: if ANSWER_MODEL hasn't produced a token within ANSWER_TTFT_TIMEOUT_S, or fails before its
    first token (5xx, connection error, unknown model...), we retry ONCE on FAST_MODEL. Nothing has been shown to the
    user yet, so switching models is invisible except for the 'fallback_model' warning.
  - Idle: once tokens flow, a gap longer than ANSWER_IDLE_TIMEOUT_S raises AnswerStalled. We can't silently retry
    mid-answer (the user already saw half of it), so the caller emits an `error` event and saves the partial answer.

Yields text deltas (str), then exactly one StreamEnd (usage). A FallbackUsed marker is yielded when the first model is
abandoned, so the caller can record its failed ai_usage row and the warning.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass

from app.ai.provider import LLMProvider, Msg, ProviderError, StreamEnd
from app.config import settings

MAX_OUTPUT_TOKENS = 4000    # includes reasoning tokens; answers themselves are asked to stay under 200 words


class AnswerStalled(Exception):
    """No token for ANSWER_IDLE_TIMEOUT_S after the answer had started."""


@dataclass(frozen=True)
class FallbackUsed:
    failed_model: str
    error_code: str             # UPSTREAM_TIMEOUT (no first token in time) | UPSTREAM_AI_ERROR (provider error)
    message: str


def attempts() -> list[tuple[str, str]]:
    return [(settings.ANSWER_MODEL, settings.ANSWER_EFFORT), (settings.FAST_MODEL, settings.FAST_EFFORT)]


async def stream_answer(
    provider: LLMProvider, *, instructions: str, input: list[Msg]
) -> AsyncIterator[str | StreamEnd | FallbackUsed]:
    plan = attempts()
    for i, (model, effort) in enumerate(plan):
        stream = provider.astream_text(model=model, effort=effort, instructions=instructions, input=input,
                                       max_output_tokens=MAX_OUTPUT_TOKENS)
        async with aclosing(stream):     # closing it also closes the upstream HTTP stream
            try:
                async with asyncio.timeout(settings.ANSWER_TTFT_TIMEOUT_S):
                    first = await anext(stream)
            except (TimeoutError, ProviderError) as exc:
                if i == len(plan) - 1:
                    raise
                code = "UPSTREAM_TIMEOUT" if isinstance(exc, TimeoutError) else "UPSTREAM_AI_ERROR"
                yield FallbackUsed(model, code, str(exc) or "No first token in time.")
                continue
            except StopAsyncIteration:
                return
            yield first
            if isinstance(first, StreamEnd):
                return
            async with aclosing(_rest(stream)) as rest:
                async for item in rest:
                    yield item
            return


async def _rest(stream: AsyncIterator[str | StreamEnd]) -> AsyncIterator[str | StreamEnd]:
    while True:
        try:
            async with asyncio.timeout(settings.ANSWER_IDLE_TIMEOUT_S):
                item = await anext(stream)
        except StopAsyncIteration:
            return
        except TimeoutError as exc:
            raise AnswerStalled(f"No token for {settings.ANSWER_IDLE_TIMEOUT_S:g}s.") from exc
        yield item
        if isinstance(item, StreamEnd):
            return
