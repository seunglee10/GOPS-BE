from backend.dummy_market import DummyMarketData
from backend.market_summary import calculate_market_summary


def test_market_summary_uses_visible_candles() -> None:
    market = DummyMarketData()
    candles = market.snapshot(["AAPL"], "1m", 80).candlesBySymbol["AAPL"]
    summary = calculate_market_summary("AAPL", "1m", candles)

    assert summary.latestPrice == candles[-1].close
    assert summary.visibleHigh == max(candle.high for candle in candles)
    assert summary.visibleLow == min(candle.low for candle in candles)
    assert summary.trend in {"strong_up", "up", "sideways", "down", "strong_down"}
