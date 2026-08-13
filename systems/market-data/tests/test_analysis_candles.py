from __future__ import annotations

import sys
import unittest
from datetime import datetime, time, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SHARED = ROOT / "systems" / "market-data" / "shared"
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

from market_data.analytics.analysis_candles import (  # noqa: E402
    CANDLE_CONTRACT_VERSION,
    AnalysisCandleSource,
    aggregate_analysis_candle_bundle,
    aggregate_analysis_candles,
    analysis_input_digest,
    canonicalize_candle_identity,
    choose_canonical_winner,
    compute_analysis_coverage,
    is_analysis_candle_bucket_complete,
    merge_canonical_candles,
)
from market_data.backfill.gapfill import TradingCalendar  # noqa: E402


class AnalysisCandleContractTest(unittest.TestCase):
    def test_daily_timestamp_is_market_midnight_across_dst(self):
        winter = canonicalize_candle_identity(candle("2026-01-05T00:00:00.000Z"), "1D")
        summer = canonicalize_candle_identity(candle("2026-07-06T00:00:00.000Z"), "1D")
        self.assertEqual(winter["timestamp"], "2026-01-05T05:00:00.000Z")
        self.assertEqual(summer["timestamp"], "2026-07-06T04:00:00.000Z")
        self.assertEqual((winter["candleKey"], summer["candleKey"]), ("2026-01-05", "2026-07-06"))
        self.assertEqual(CANDLE_CONTRACT_VERSION, "regular-session-derived")

    def test_intraday_identity_preserves_exact_utc_bucket_timestamp(self):
        identity = canonicalize_candle_identity(candle("2026-07-10T13:35:00.000Z"), "5m")

        self.assertEqual(identity["candleKey"], "2026-07-10T13:35:00.000Z")
        self.assertEqual(identity["timestamp"], "2026-07-10T13:35:00.000Z")
        self.assertEqual(identity["interval"], "5m")

    def test_intraday_bucket_is_complete_only_after_its_interval_end(self):
        self.assertFalse(is_analysis_candle_bucket_complete(
            "2026-07-10T13:35:00.000Z", "5m",
            now=datetime(2026, 7, 10, 13, 39, 59, tzinfo=timezone.utc),
        ))
        self.assertTrue(is_analysis_candle_bucket_complete(
            "2026-07-10T13:35:00.000Z", "5m",
            now=datetime(2026, 7, 10, 13, 40, 0, tzinfo=timezone.utc),
        ))

    def test_weekly_and_monthly_identity_use_utc_bucket_midnight(self):
        weekly_utc = canonicalize_candle_identity(candle("2026-07-06T00:00:00.000Z"), "1W")
        weekly_market = canonicalize_candle_identity(candle("2026-07-06T04:00:00.000Z"), "1W")
        monthly_utc = canonicalize_candle_identity(candle("2026-07-01T00:00:00.000Z"), "1M")
        monthly_market = canonicalize_candle_identity(candle("2026-07-01T04:00:00.000Z"), "1M")
        self.assertEqual((weekly_utc["candleKey"], weekly_market["candleKey"]), ("2026-07-06", "2026-07-06"))
        self.assertEqual((weekly_utc["timestamp"], weekly_market["timestamp"]), ("2026-07-06T00:00:00.000Z",) * 2)
        self.assertEqual((monthly_utc["candleKey"], monthly_market["candleKey"]), ("2026-07", "2026-07"))
        self.assertEqual((monthly_utc["timestamp"], monthly_market["timestamp"]), ("2026-07-01T00:00:00.000Z",) * 2)

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

    def test_weekly_uses_daily_source_and_drops_open_bucket(self):
        rows = [
            candle("2026-06-29T00:00:00.000Z", open=100, high=105, low=99, close=104, volume=10),
            candle("2026-06-30T00:00:00.000Z", open=104, high=108, low=103, close=107, volume=20),
            candle("2026-07-06T00:00:00.000Z", open=107, high=110, low=106, close=109, volume=30),
        ]
        now = datetime(2026, 7, 8, tzinfo=timezone.utc)
        weekly = aggregate_analysis_candles(rows, "1W", now=now)
        self.assertEqual(len(weekly), 1)
        self.assertEqual(weekly[0]["candleKey"], "2026-06-29")
        self.assertEqual((weekly[0]["open"], weekly[0]["close"], weekly[0]["volume"]), (100, 107, 30))

    def test_bundle_matches_individual_interval_derivation(self):
        rows = [
            candle("2026-06-29T00:00:00.000Z", close=100),
            candle("2026-06-30T00:00:00.000Z", close=101),
            candle("2026-07-01T00:00:00.000Z", close=102),
        ]
        now = datetime(2026, 7, 11, tzinfo=timezone.utc)
        bundle = aggregate_analysis_candle_bundle(rows, ("1W", "1D"), now=now)
        for interval in ("1W", "1D"):
            self.assertEqual(
                bundle[interval],
                aggregate_analysis_candles(rows, interval, now=now),
            )

    def test_weekly_bucket_completes_at_last_session_close_including_early_close(self):
        rows = [candle("2026-11-23T00:00:00.000Z")]
        calendar = TradingCalendar(
            closed_dates=frozenset({"2026-11-26"}),
            early_closes={"2026-11-27": time(13, 0)},
        )

        before_close = aggregate_analysis_candles(
            rows,
            "1W",
            now=datetime(2026, 11, 27, 17, 59, tzinfo=timezone.utc),
            calendar=calendar,
        )
        after_close = aggregate_analysis_candles(
            rows,
            "1W",
            now=datetime(2026, 11, 27, 18, 1, tzinfo=timezone.utc),
            calendar=calendar,
        )

        self.assertEqual(before_close, [])
        self.assertEqual(after_close[0]["candleKey"], "2026-11-23")
        self.assertEqual(after_close[0]["timestamp"], "2026-11-23T00:00:00.000Z")

    def test_default_calendar_contains_standard_nyse_early_closes(self):
        calendar = TradingCalendar()
        self.assertEqual(calendar.session_close_for(datetime(2026, 7, 2).date()), time(13, 0))
        self.assertEqual(calendar.session_close_for(datetime(2026, 11, 27).date()), time(13, 0))
        self.assertEqual(calendar.session_close_for(datetime(2026, 12, 24).date()), time(13, 0))
        self.assertEqual(calendar.session_close_for(datetime(2026, 12, 23).date()), time(16, 0))

    def test_digest_changes_for_ohlcv_correction_and_is_stable(self):
        rows = aggregate_analysis_candles([candle("2026-07-06T00:00:00.000Z")], "1D")
        before = analysis_input_digest("NVDA", "1D", rows)
        self.assertEqual(before, analysis_input_digest("NVDA", "1D", rows))
        rows[0]["close"] += 0.01
        self.assertNotEqual(before, analysis_input_digest("NVDA", "1D", rows))

    def test_invalid_canonical_ohlcv_is_rejected_before_analysis(self):
        with self.assertRaisesRegex(ValueError, "open/close"):
            aggregate_analysis_candles([
                candle("2026-07-06T00:00:00.000Z", close=103, high=102),
            ], "1D")
        with self.assertRaisesRegex(ValueError, "non-negative"):
            aggregate_analysis_candles([
                candle("2026-07-06T00:00:00.000Z", volume=-1),
            ], "1D")

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
        bundle = source.load_symbol("NVDA", ("1W", "1D"))
        self.assertEqual(provider.calls, 1)
        self.assertEqual(set(bundle.rows), {"1W", "1D"})
        self.assertTrue(all(value.startswith("sha256:") for value in bundle.digests.values()))

    def test_symbol_source_reads_intraday_directly_and_long_intervals_from_one_daily_query(self):
        provider = MixedProvider(
            daily=[candle("2026-06-29T00:00:00.000Z"), candle("2026-06-30T00:00:00.000Z")],
            intraday={
                interval: [candle("2026-07-10T13:30:00.000Z", interval=interval)]
                for interval in ("1m", "5m", "10m", "1h", "4h")
            },
        )
        source = AnalysisCandleSource(provider, now_provider=lambda: datetime(2026, 7, 11, tzinfo=timezone.utc))

        bundle = source.load_symbol("NVDA", ("1W", "1D", "4h", "1h", "10m", "5m", "1m"))

        self.assertEqual(provider.daily_calls, 1)
        self.assertEqual(provider.direct_calls, ["4h", "1h", "10m", "5m", "1m"])
        self.assertEqual(set(bundle.rows), {"1m", "5m", "10m", "1h", "4h", "1D", "1W"})
        self.assertEqual(bundle.rows["5m"][0]["candleKey"], "2026-07-10T13:30:00.000Z")


class FakeProvider:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def daily_candles(self, *_args, **_kwargs):
        self.calls += 1
        return list(self.rows)


class MixedProvider:
    def __init__(self, *, daily, intraday):
        self.daily = daily
        self.intraday = intraday
        self.daily_calls = 0
        self.direct_calls = []

    def daily_candles(self, *_args, **_kwargs):
        self.daily_calls += 1
        return list(self.daily)

    def stored_interval_candles(self, _symbol, interval, **_kwargs):
        self.direct_calls.append(interval)
        return list(self.intraday[interval])


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
