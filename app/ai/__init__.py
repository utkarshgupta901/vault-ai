"""Provider factory. The API and workers each get one provider instance per process.
Tests swap in a FakeProvider with set_provider()."""

from app.ai.provider import LLMProvider
from app.config import settings

_provider: LLMProvider | None = None


def get_provider(*, for_worker: bool = False) -> LLMProvider:
    global _provider
    if _provider is None:
        if settings.LLM_PROVIDER == "fake":
            from app.ai.fake_provider import FakeProvider

            _provider = FakeProvider()
        else:
            from app.ai.openai_provider import OpenAIProvider

            _provider = OpenAIProvider(for_worker=for_worker)
    return _provider


def set_provider(provider: LLMProvider | None) -> None:
    global _provider
    _provider = provider
