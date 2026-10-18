"""Cost ledger prices: exact Decimal arithmetic (the ai_usage column is NUMERIC(12,6)), and a model missing from the
price table is still recorded at $0 but no longer silently."""

from decimal import Decimal
from typing import Any

import pytest

from app.ai import pricing
from app.ai.pricing import PRICES_PER_1M, cost_usd
from app.ai.provider import Usage


@pytest.mark.parametrize(("usage", "expected"), [
    # 500k uncached * $2 + 500k cached * $0.10 + 100k out * $10 (per 1M)
    (Usage(model="gpt-6.1-sol", input_tokens=1_000_000, cached_input_tokens=500_000, output_tokens=100_000),
     Decimal("2.050000")),
    # $0.0000005 exactly: rounds half up to the ledger's 6 places, as Postgres NUMERIC does (floats made it $0)
    (Usage(model="gpt-5-nano", input_tokens=10), Decimal("0.000001")),
    # 54 * $0.05 + 7 * $0.40 = $5.5 per 1M (floats gave 0.000005)
    (Usage(model="gpt-5-nano", input_tokens=54, output_tokens=7), Decimal("0.000006")),
])
def test_known_model_cost_is_exact(usage: Usage, expected: Decimal) -> None:
    cost = cost_usd(usage)
    assert isinstance(cost, Decimal) and cost == expected


def test_prices_are_decimals() -> None:
    assert all(isinstance(p, Decimal) for prices in PRICES_PER_1M.values() for p in prices)


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kw: Any) -> None:
        self.events.append((event, kw))


def test_unknown_model_costs_zero_and_warns_once(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder()
    monkeypatch.setattr(pricing, "log", recorder, raising=False)
    monkeypatch.setattr(pricing, "_unpriced", set(), raising=False)
    usage = Usage(model="gpt-unlisted", input_tokens=1000, output_tokens=1000)
    assert cost_usd(usage) == Decimal("0")          # still recorded, so the call shows up in the metrics
    assert cost_usd(usage) == Decimal("0")
    assert [kw.get("model") for _, kw in recorder.events] == ["gpt-unlisted"]   # once per model per process
    cost_usd(Usage(model="gpt-also-unlisted"))
    assert len(recorder.events) == 2
