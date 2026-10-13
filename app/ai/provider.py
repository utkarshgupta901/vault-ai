"""The single seam between Vault and any LLM vendor.

Everything that talks to a model goes through `LLMProvider`. `openai` is imported only in openai_provider.py, so:
  - tests run offline against FakeProvider (deterministic, free),
  - switching vendors (e.g. Claude + Voyage embeddings) means adding one class, not touching RAG code,
  - every call returns a `Usage`, which feeds the cost ledger (ai_usage table).
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol, TypedDict, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class Msg(TypedDict):
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class Usage:
    model: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    latency_ms: int = 0
    estimated: bool = False      # True when computed locally (e.g. client disconnected mid-stream)


@dataclass
class EmbedResult:
    vectors: list[list[float]]
    usage: Usage


@dataclass
class StreamEnd:
    """Last item of astream_text(): carries usage and how the stream ended."""

    usage: Usage
    status: str = "completed"    # completed | incomplete
    extra: dict = field(default_factory=dict)


class ProviderError(Exception):
    """Vendor-neutral error. `kind` decides what the caller does:
    transient   -> retry (429 / 5xx / timeout / connection), honouring retry_after when given
    refusal     -> the model declined (do not retry)
    incomplete  -> output hit max_output_tokens (retry once with a bigger budget)
    bad_request -> our request was invalid, or the reply could not be parsed (status_code is None then)

    `usage` is set when the model DID answer (and was billed) but the answer is unusable: truncated, refused or not
    parsable. The caller still owes the cost ledger a row for it (success=false), or the daily quota under-counts.
    """

    def __init__(
        self,
        kind: Literal["transient", "refusal", "incomplete", "bad_request"],
        message: str,
        retry_after: float | None = None,
        status_code: int | None = None,
        usage: Usage | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retry_after = retry_after
        self.status_code = status_code
        self.usage = usage


class LLMProvider(Protocol):
    name: str

    # Embeddings — sync for workers, async for the API (query embedding).
    def embed(self, texts: list[str]) -> EmbedResult: ...
    async def aembed(self, texts: list[str]) -> EmbedResult: ...

    # Structured output: the reply is parsed into `schema` (a Pydantic model) or a ProviderError is raised.
    def complete_json(
        self, *, model: str, effort: str, instructions: str, input: str | list[Msg], schema: type[T],
        max_output_tokens: int,
    ) -> tuple[T, Usage]: ...
    async def acomplete_json(
        self, *, model: str, effort: str, instructions: str, input: str | list[Msg], schema: type[T],
        max_output_tokens: int,
    ) -> tuple[T, Usage]: ...

    # Free text, non-streamed (custom summaries).
    async def acomplete_text(
        self, *, model: str, effort: str, instructions: str, input: str | list[Msg], max_output_tokens: int,
    ) -> tuple[str, Usage]: ...

    # Streamed text: yields str deltas, then exactly one StreamEnd.
    def astream_text(
        self, *, model: str, effort: str, instructions: str, input: str | list[Msg], max_output_tokens: int,
    ) -> AsyncIterator[str | StreamEnd]: ...
