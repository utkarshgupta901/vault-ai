"""Model prices in USD per 1M tokens: (input, cached_input, output).

Source: https://developers.openai.com/api/docs/pricing — checked 2026-10-13.
Reasoning tokens are billed as output and are already included in `output_tokens`.
Cached input tokens are a subset of `input_tokens`, billed at the cached rate.

Decimal end to end: the ledger column is NUMERIC(12,6), and float math lost half-millionths on small calls (e.g. 10
input tokens at $0.05/1M came out as $0). Rounding is half-up to 6 places, as Postgres NUMERIC does."""

from decimal import ROUND_HALF_UP, Decimal

import structlog

from app.ai.provider import Usage

log = structlog.get_logger(__name__)


def _usd(inp: str, cached: str, out: str) -> tuple[Decimal, Decimal, Decimal]:
    return Decimal(inp), Decimal(cached), Decimal(out)


PRICES_PER_1M: dict[str, tuple[Decimal, Decimal, Decimal]] = {
    "gpt-6.1-sol": _usd("2.00", "0.10", "10.00"),
    "gpt-6-sol": _usd("2.00", "0.20", "10.00"),
    "gpt-6-luna": _usd("0.10", "0.01", "0.50"),
    "gpt-6-astra": _usd("10.00", "1.00", "50.00"),
    "gpt-5.4-mini": _usd("0.75", "0.075", "4.50"),
    "gpt-5-mini": _usd("0.25", "0.025", "2.00"),
    "gpt-5-nano": _usd("0.05", "0.005", "0.40"),
    "gpt-4.1-mini": _usd("0.40", "0.10", "1.60"),
    "text-embedding-3-small": _usd("0.02", "0", "0"),
    "text-embedding-3-large": _usd("0.13", "0", "0"),
    "fake": _usd("0", "0", "0"),
}
_FREE = _usd("0", "0", "0")
_PER_TOKEN = Decimal(1_000_000)
_LEDGER_PLACES = Decimal("0.000001")
_unpriced: set[str] = set()      # models already warned about in this process


def cost_usd(usage: Usage) -> Decimal:
    prices = PRICES_PER_1M.get(usage.model)
    if prices is None:
        # Still $0, so the call is recorded and visible in the metrics; but a configured model missing here means
        # the spend shown and the daily quota under-count, so say so (once per model, not once per call).
        if usage.model not in _unpriced:
            _unpriced.add(usage.model)
            log.warning("ai_price_unknown", model=usage.model)
        prices = _FREE
    inp, cached, out = prices
    uncached = max(0, usage.input_tokens - usage.cached_input_tokens)
    dollars = (uncached * inp + usage.cached_input_tokens * cached + usage.output_tokens * out) / _PER_TOKEN
    return dollars.quantize(_LEDGER_PLACES, rounding=ROUND_HALF_UP)
