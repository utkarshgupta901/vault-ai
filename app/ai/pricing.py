"""Model prices in USD per 1M tokens: (input, cached_input, output).

Source: https://developers.openai.com/api/docs/pricing — checked 2026-10-13.
Reasoning tokens are billed as output and are already included in `output_tokens`.
Cached input tokens are a subset of `input_tokens`, billed at the cached rate."""

from decimal import Decimal

from app.ai.provider import Usage

PRICES_PER_1M: dict[str, tuple[float, float, float]] = {
    "gpt-6.1-sol": (2.00, 0.10, 10.00),
    "gpt-6-sol": (2.00, 0.20, 10.00),
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-6-astra": (10.00, 1.00, 50.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "gpt-5-nano": (0.05, 0.005, 0.40),
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
    "text-embedding-3-small": (0.02, 0.0, 0.0),
    "text-embedding-3-large": (0.13, 0.0, 0.0),
    "fake": (0.0, 0.0, 0.0),
}


def cost_usd(usage: Usage) -> Decimal:
    inp, cached, out = PRICES_PER_1M.get(usage.model, (0.0, 0.0, 0.0))  # unknown model -> $0, visible in metrics
    uncached = max(0, usage.input_tokens - usage.cached_input_tokens)
    dollars = (uncached * inp + usage.cached_input_tokens * cached + usage.output_tokens * out) / 1_000_000
    return Decimal(str(round(dollars, 6)))
