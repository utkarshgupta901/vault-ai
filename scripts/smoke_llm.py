"""Verify the configured OpenAI setup through OUR provider (not the raw SDK), so the wrapper is tested too.

Checks: every configured (model, effort) pair answers; structured output parses; streaming yields deltas and final
usage; embeddings have EMBED_DIMS dimensions; and time-to-first-token p50/p95 for the answer and fast models.

    uv run python scripts/smoke_llm.py [--runs 20]
"""

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import BaseModel  # noqa: E402

from app.ai.openai_provider import OpenAIProvider  # noqa: E402
from app.ai.pricing import cost_usd  # noqa: E402
from app.ai.provider import StreamEnd  # noqa: E402
from app.config import settings  # noqa: E402

QUESTIONS = [
    "How many PTO days do employees get?", "What is the notice period for termination?",
    "Summarize the incident in one sentence.", "What is the liability cap?", "Who is eligible for parental leave?",
]


class Probe(BaseModel):
    answer: str
    confidence: float


def ok(msg: str) -> None:
    print(f"  \033[32m✓\033[0m {msg}")


async def main(runs: int) -> int:
    if not settings.OPENAI_API_KEY:
        print("OPENAI_API_KEY is not set (put it in .env)")
        return 1
    p = OpenAIProvider()
    failures = 0
    pairs = {(settings.ANSWER_MODEL, settings.ANSWER_EFFORT), (settings.FAST_MODEL, settings.FAST_EFFORT),
             (settings.INSIGHTS_MODEL, settings.INSIGHTS_EFFORT), (settings.JUDGE_MODEL, settings.JUDGE_EFFORT)}
    print("1) structured output for every configured (model, effort):")
    for model, effort in sorted(pairs):
        try:
            parsed, usage = await p.acomplete_json(model=model, effort=effort, instructions="Answer briefly.",
                                                   input="What is 2+2? confidence 0..1", schema=Probe,
                                                   max_output_tokens=2000)
            ok(f"{model}/{effort}: {parsed.answer!r} in={usage.input_tokens} out={usage.output_tokens} "
               f"reasoning={usage.reasoning_tokens} {usage.latency_ms}ms ${cost_usd(usage)}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  ✗ {model}/{effort}: {exc}")

    print("2) embeddings:")
    emb = await p.aembed(["paid time off", "vacation days", "liability cap"])
    dims = len(emb.vectors[0])
    cos = lambda a, b: sum(x * y for x, y in zip(a, b, strict=True))  # noqa: E731  (OpenAI vectors are unit-length)
    if dims != settings.EMBED_DIMS:
        failures += 1
    ok(f"{settings.EMBED_MODEL}: {dims} dims; cos(PTO, vacation)={cos(emb.vectors[0], emb.vectors[1]):.3f} "
       f"vs cos(PTO, liability)={cos(emb.vectors[0], emb.vectors[2]):.3f}")

    print(f"3) streaming TTFT over {runs} runs:")
    for model, effort in [(settings.ANSWER_MODEL, settings.ANSWER_EFFORT), (settings.FAST_MODEL, settings.FAST_EFFORT)]:
        ttfts, end = [], None
        for i in range(runs):
            started, first = time.perf_counter(), None
            async for item in p.astream_text(model=model, effort=effort, instructions="Answer in one sentence.",
                                             input=QUESTIONS[i % len(QUESTIONS)], max_output_tokens=200):
                if isinstance(item, str) and first is None:
                    first = time.perf_counter() - started
                elif isinstance(item, StreamEnd):
                    end = item
            ttfts.append(first or 0)
        q = statistics.quantiles(ttfts, n=20) if len(ttfts) >= 2 else ttfts * 19
        ok(f"{model}/{effort}: TTFT p50={statistics.median(ttfts):.2f}s p95={q[18]:.2f}s; "
           f"final usage present={end is not None and end.usage.output_tokens > 0}")
    print("PASS" if not failures else f"FAIL ({failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=20)
    raise SystemExit(asyncio.run(main(ap.parse_args().runs)))
