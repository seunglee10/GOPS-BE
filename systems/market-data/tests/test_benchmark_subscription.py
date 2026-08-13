from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "systems/market-data/shared"))

from market_data.alpaca.subscription import (  # noqa: E402
    configured_benchmark_symbols,
    configured_collection_symbols,
    load_symbols_and_channels,
)
from market_data.alpaca.websocket_collector import benchmark_subscription_health  # noqa: E402


def test_spy_is_benchmark_subscription_but_not_collection_universe(monkeypatch) -> None:
    monkeypatch.setenv("ALPACA_COLLECTION_SYMBOLS", "AAA,BBB")
    monkeypatch.setenv("ALPACA_BENCHMARK_SYMBOLS", "SPY")
    monkeypatch.setenv("ALPACA_CHANNELS", "bars,updatedBars,dailyBars,statuses")
    assert configured_collection_symbols() == ["AAA", "BBB"]
    assert configured_benchmark_symbols() == ["SPY"]
    symbols, channels = load_symbols_and_channels()
    assert symbols == ["AAA", "BBB", "SPY"]
    assert channels == ["bars", "updatedBars", "dailyBars", "statuses"]


def test_benchmark_health_requires_spy_on_every_collection_channel() -> None:
    health = benchmark_subscription_health(
        {"bars": ["SPY"], "updatedBars": ["SPY"], "dailyBars": ["SPY"], "statuses": []},
        ["SPY"],
        ["bars", "updatedBars", "dailyBars", "statuses"],
    )
    assert health["benchmarkSubscriptionReady"] is False
    assert health["benchmarkMissingSubscriptions"] == ["statuses:SPY"]
