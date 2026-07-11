from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SHARED = ROOT / "systems" / "market-data" / "shared"
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

from alfaka.analytics.analysis_candles import (  # noqa: E402
    AnalysisCandleSource,
    aggregate_analysis_candles,
    analysis_input_digest,
    canonicalize_candle_identity,
    choose_canonical_winner,
    compute_analysis_coverage,
    merge_canonical_candles,
)


class AnalysisCandleContractTest(unittest.TestCase):
    def test_daily_timestamp_is_market_midnight_across_dst(self):
        winter = canonicalize_candle_identity(candle("2026-01-05T00:00:00.000Z"), "1D")
        summer = canonicalize_candle_identity(candle("2026-07-06T00:00:00.000Z"), "1D")
        self.assertEqual(winter["timestamp"], "2026-01-05T05:00:00.000Z")
        self.assertEqual(summer["timestamp"], "2026-07-06T04:00:00.000Z")
        self.assertEqual((winter["candleKey"], summer["candleKey"]), ("2026-01-05", "2026-07-06"))

    def test_winner_does_not_depend_on_input_order(self):
        older = candle("2026-07-06T00:00:00.000Z", close=100, updatedAt="2026-07-06T21:00:00Z")
        newer = candle("2026-07-06T00:00:00.000Z", close=101, updatedAt="2026-07-06T22:00:00Z")
        first = choose_canonical_winner([older, newer])
        second = choose_canonical_winner([newer, older])
        self.assertEqual(first["close"], 101)
        self.assertEqual(first, second)

    def test_closed_clickhouse_wins_analysis_and_redis_wins_current(self):
        direct = candle("2026-07-06T00:00:00.000Z", close=100, sourceClass="clickhouse_direct")
        redis = candle("2026-07-06T00:00:00.000Z", close=101, sourceClass="redis_closed")
        self.assertEqual(choose_canonical_winner([direct, redis], view="analysis_closed")["close"], 100)
        merged = merge_canonical_candles([direct], [redis], interval="1D", view="chart_current")
        self.assertEqual(merged[0]["close"], 101)

    def test_weekly_and_monthly_use_same_daily_source_and_drop_open_bucket(self):
        rows = [
            candle("2026-06-29T00:00:00.000Z", open=100, high=105, low=99, close=104, volume=10),
            candle("2026-06-30T00:00:00.000Z", open=104, high=108, low=103, close=107, volume=20),
            candle("2026-07-06T00:00:00.000Z", open=107, high=110, low=106, close=109, volume=30),
        ]
        now = datetime(2026, 7, 11, tzinfo=timezone.utc)
        weekly = aggregate_analysis_candles(rows, "1W", now=now)
        monthly = aggregate_analysis_candles(rows, "1M", now=now)
        self.assertEqual(len(weekly), 1)
        self.assertEqual(weekly[0]["candleKey"], "2026-06-29")
        self.assertEqual((weekly[0]["open"], weekly[0]["close"], weekly[0]["volume"]), (100, 107, 30))
        self.assertEqual(len(monthly), 1)
        self.assertEqual(monthly[0]["candleKey"], "2026-06")

    def test_digest_changes_for_ohlcv_correction_and_is_stable(self):
        rows = aggregate_analysis_candles([candle("2026-07-06T00:00:00.000Z")], "1D")
        before = analysis_input_digest("NVDA", "1D", rows)
        self.assertEqual(before, analysis_input_digest("NVDA", "1D", rows))
        rows[0]["close"] += 0.01
        self.assertNotEqual(before, analysis_input_digest("NVDA", "1D", rows))

    def test_coverage_uses_expected_sessions_and_flags_stale_input(self):
        rows = aggregate_analysis_candles([
            candle("2026-07-08T00:00:00.000Z"), candle("2026-07-09T00:00:00.000Z")
        ], "1D")
        coverage = compute_analysis_coverage(rows, "1D", display_bars=2, now=datetime(2026, 7, 11, tzinfo=timezone.utc))
        self.assertEqual(coverage["lastExpectedClosedAt"], "2026-07-10T04:00:00.000Z")
        self.assertIn("stale_input", coverage["qualityFlags"])
        self.assertFalse(coverage["renderable"])

    def test_symbol_source_queries_daily_once_for_all_intervals(self):
        provider = FakeProvider([candle("2026-06-29T00:00:00.000Z"), candle("2026-06-30T00:00:00.000Z")])
        source = AnalysisCandleSource(provider, now_provider=lambda: datetime(2026, 7, 11, tzinfo=timezone.utc))
        bundle = source.load_symbol("NVDA", ("1M", "1W", "1D"))
        self.assertEqual(provider.calls, 1)
        self.assertEqual(set(bundle.rows), {"1M", "1W", "1D"})
        self.assertTrue(all(value.startswith("sha256:") for value in bundle.digests.values()))


class FakeProvider:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def daily_candles(self, *_args, **_kwargs):
        self.calls += 1
        return list(self.rows)


def candle(timestamp: str, **values) -> dict:
    result = {
        "symbol": "NVDA", "timestamp": timestamp,
        "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000,
        "isClosed": True, "canonicalVersion": "v2", "priceAdjustment": "split",
        "marketSession": "regular", "sourceClass": "clickhouse_direct",
    }
    result.update(values)
    return result


if __name__ == "__main__":
    unittest.main()
