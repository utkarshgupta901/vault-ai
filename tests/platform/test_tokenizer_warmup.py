"""Regression (DECISIONS D-07): tiktoken loads its encoding on first use, downloading it when there is no cache. In the
API the first use was the first follow-up question after a restart (history is capped by tokens), and the synchronous
download blocked the event loop: TTFT 23 s in the eval. The image now bakes every encoding the code uses, and the API
loads it at startup."""

import re
from pathlib import Path

import pytest
from fastapi import FastAPI

from app import main
from app.ingestion import chunking
from app.rag import tokens

ROOT = Path(__file__).resolve().parents[2]


def test_image_bakes_every_encoding_the_code_uses() -> None:
    lines = (ROOT / "Dockerfile").read_text().splitlines()
    env = next(i for i, ln in enumerate(lines) if re.match(r"ENV TIKTOKEN_CACHE_DIR=/\S+", ln))
    bake = next(i for i, ln in enumerate(lines) if ln.startswith("RUN") and "tiktoken.get_encoding" in ln)
    sync = next(i for i, ln in enumerate(lines) if ln.startswith("RUN uv sync"))
    assert sync < env < bake                     # tiktoken is installed, and the files land in the cache the app reads
    for encoding in (tokens.ENCODING, chunking.ENCODING):
        assert f"'{encoding}'" in lines[bake]


async def test_api_loads_the_tokenizer_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(tokens, "warm_up", lambda: calls.append("warm"))
    async with main.lifespan(FastAPI()):
        assert calls == ["warm"]                 # before the first request is served


def test_warm_up_survives_an_unloadable_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    import tiktoken

    def offline(_: str) -> None:
        raise OSError("no network")

    monkeypatch.setattr(tiktoken, "get_encoding", offline)
    tokens._encoding.cache_clear()
    try:
        tokens.warm_up()                         # startup must not fail: counting falls back to ~4 chars per token
        assert tokens.count_tokens("x" * 40) == 10
    finally:
        tokens._encoding.cache_clear()
