from datetime import UTC, datetime

from backend.dummy_market import DummyMarketData, floor_to_timeframe, parse_iso


def test_snapshot_returns_sorted_requested_limit() -> None:
    market = DummyMarketData()
    snapshot = market.snapshot(["AAPL"], "1m", 5)

    candles = snapshot.candlesBySymbol["AAPL"]
    assert len(candles) == 5
    assert [candle.timestamp for candle in candles] == sorted(candle.timestamp for candle in candles)
    assert all(candle.symbol == "AAPL" for candle in candles)


def test_next_batch_updates_known_symbols() -> None:
    market = DummyMarketData()
    events = market.next_event_batch(["AAPL", "MSFT"], "1m")

    assert {event["symbol"] for event in events} == {"AAPL", "MSFT"}
    assert all(event["type"] in {"bar", "updatedBar"} for event in events)


def test_dummy_market_does_not_drift_into_future() -> None:
    market = DummyMarketData()
    for _ in range(30):
        market.next_event_batch(["AAPL"], "1m")

    snapshot = market.snapshot(["AAPL"], "1m", 300)
    latest = max(parse_iso(candle.timestamp) for candle in snapshot.candlesBySymbol["AAPL"])
    assert latest <= floor_to_timeframe(datetime.now(UTC), "1m")
