"""Local token counting (tiktoken o200k_base, the encoding of the current OpenAI chat models).

Used where the API gives us no usage numbers: capping conversation history to HISTORY_MAX_TOKENS, and estimating the
cost of a stream the client abandoned (the provider's final usage event never arrives, so we count what we sent and
what we received, and mark the ai_usage row `estimated=true`).

If the encoding can't be loaded (first run offline), we fall back to ~4 characters per token: an estimate is still
better than recording zero cost."""

from functools import cache
from typing import Any

import structlog

log = structlog.get_logger(__name__)
ENCODING = "o200k_base"


@cache
def _encoding() -> Any | None:
    try:
        import tiktoken

        return tiktoken.get_encoding(ENCODING)
    except Exception as exc:  # noqa: BLE001 - offline / missing cache: use the character heuristic
        log.warning("tiktoken_unavailable", encoding=ENCODING, error=str(exc))
        return None


def count_tokens(text: str) -> int:
    if not text:
        return 0
    enc = _encoding()
    if enc is None:
        return max(1, len(text) // 4)
    return len(enc.encode(text, disallowed_special=()))


def truncate_tokens(text: str, max_tokens: int) -> str:
    """The beginning of `text` in at most about `max_tokens` tokens, ending in '…' when something was cut (so the
    model can tell the text is incomplete). One token of the budget is left for the marker."""
    if count_tokens(text) <= max_tokens:
        return text
    keep = max(0, max_tokens - 1)
    enc = _encoding()
    if enc is None:
        head = text[: keep * 4]
    else:
        # A cut inside a multi-byte character decodes to U+FFFD; drop it rather than feed the model a broken glyph.
        head = enc.decode(enc.encode(text, disallowed_special=())[:keep]).rstrip("\ufffd")
    return head.rstrip() + "…"
