"""Deterministic, offline LLMProvider for tests and keyless local runs (LLM_PROVIDER=fake).

- Embeddings: hashed bag-of-words (1536-d, L2-normalized). Texts sharing words get high cosine similarity, which
  is enough to exercise retrieval, fusion and the gate without any network calls.
- Structured output: a valid instance of any Pydantic schema, synthesized from its type hints, unless a test
  registered a canned value with `fake.json_responses[SchemaName] = instance_or_callable`.
- Streaming: answers cite [S1] when sources are present, otherwise use the refusal phrase.
- Failure injection: `fake.fail_next.append(ProviderError(...))` makes the next call raise it.
"""

import hashlib
import math
import re
import types
from collections.abc import AsyncIterator, Callable
from datetime import date
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel

from app.ai.provider import EmbedResult, Msg, ProviderError, StreamEnd, T, Usage
from app.config import settings

_WORD = re.compile(r"[a-z0-9]+")
REFUSAL = "I couldn't find this in your documents."


def _text_of(input: str | list[Msg]) -> str:
    return input if isinstance(input, str) else "\n".join(m["content"] for m in input)


def fake_vector(text: str, dims: int | None = None) -> list[float]:
    dims = dims or settings.EMBED_DIMS
    vec = [0.0] * dims
    for word in _WORD.findall(text.lower()):
        h = int.from_bytes(hashlib.md5(word.encode()).digest()[:8], "big")
        vec[h % dims] += 1.0 if (h >> 63) & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _synth(annotation: Any, name: str = "") -> Any:
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, types.UnionType):
        non_none = [a for a in args if a is not type(None)]
        return None if len(non_none) < len(args) else _synth(non_none[0], name)
    if origin is Literal:
        return args[0]
    if origin in (list, tuple, set):
        return [_synth(args[0], name)] if args else []
    if origin is dict:
        return {}
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return synthesize(annotation)
    if annotation is str:
        return f"fake {name}".strip()
    if annotation is bool:
        return False
    if annotation is int:
        return 1
    if annotation is float:
        return 0.5
    if annotation is date:
        return date(2026, 1, 1)
    return None


def synthesize(schema: type[T]) -> T:
    values = {fname: _synth(f.annotation, fname) for fname, f in schema.model_fields.items()}
    return schema.model_validate(values)


class FakeProvider:
    name = "fake"

    def __init__(self) -> None:
        self.json_responses: dict[str, Any | Callable[[str], Any]] = {}
        self.fail_next: list[ProviderError] = []
        self.answer_override: Callable[[str], str] | None = None
        self.calls: list[dict[str, Any]] = []

    def _maybe_fail(self) -> None:
        if self.fail_next:
            raise self.fail_next.pop(0)

    def _usage(self, model: str, text_in: str, text_out: str = "") -> Usage:
        return Usage(model=model, input_tokens=max(1, len(text_in) // 4), output_tokens=len(text_out) // 4, latency_ms=1)

    # --- Embeddings ------------------------------------------------------------------------------------
    def embed(self, texts: list[str]) -> EmbedResult:
        self._maybe_fail()
        self.calls.append({"op": "embed", "n": len(texts)})
        return EmbedResult([fake_vector(t) for t in texts], self._usage(settings.EMBED_MODEL, " ".join(texts)))

    async def aembed(self, texts: list[str]) -> EmbedResult:
        return self.embed(texts)

    # --- Structured output -------------------------------------------------------------------------------
    def complete_json(self, *, model: str, effort: str, instructions: str, input: str | list[Msg],
                      schema: type[T], max_output_tokens: int) -> tuple[T, Usage]:
        self._maybe_fail()
        text = _text_of(input)
        self.calls.append({"op": "json", "schema": schema.__name__, "model": model})
        canned = self.json_responses.get(schema.__name__)
        if canned is not None:
            value = canned(text) if callable(canned) else canned
            result = value if isinstance(value, schema) else schema.model_validate(value)
        else:
            result = synthesize(schema)
        return result, self._usage(model, instructions + text, result.model_dump_json())

    async def acomplete_json(self, **kwargs: Any) -> tuple[T, Usage]:
        return self.complete_json(**kwargs)

    # --- Text ------------------------------------------------------------------------------------------
    def _answer(self, text: str) -> str:
        if self.answer_override:
            return self.answer_override(text)
        if 'id="S1"' in text:
            return "Based on the documents, here is the answer [S1]."
        if "<document" in text:
            return "This is a fake summary of the document."
        return REFUSAL

    async def acomplete_text(self, *, model: str, effort: str, instructions: str, input: str | list[Msg],
                             max_output_tokens: int) -> tuple[str, Usage]:
        self._maybe_fail()
        text = _text_of(input)
        out = self._answer(text)
        return out, self._usage(model, instructions + text, out)

    async def astream_text(self, *, model: str, effort: str, instructions: str, input: str | list[Msg],
                           max_output_tokens: int) -> AsyncIterator[str | StreamEnd]:
        self._maybe_fail()
        text = _text_of(input)
        self.calls.append({"op": "stream", "model": model})
        out = self._answer(text)
        for token in re.findall(r"\S+\s*", out):
            yield token
        yield StreamEnd(usage=self._usage(model, instructions + text, out))
