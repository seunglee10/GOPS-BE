from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "systems/market-data/shared"))

from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider, feed_profile_priority  # noqa: E402


class CapturingClickHouseProvider(ClickHouseMarketDataProvider):
    def __init__(self):
        super().__init__(url="http://clickhouse.invalid", database="market_data", user="u", password="p")
        self.last_query = None
        self.last_parameters = None

    def query_json_each_row(self, query, parameters):
        self.last_query = query
        self.last_parameters = parameters
        return []


class ClickHouseProviderContractTests(unittest.TestCase):
    def test_monthly_query_applies_configured_history_floor(self) -> None:
        with mock.patch.dict("os.environ", {
            "MARKET_DATA_MAX_HISTORY_YEARS": "6",
            "MARKET_DATA_HISTORY_NOW": "2026-07-01T00:00:00.000Z",
        }):
            provider = CapturingClickHouseProvider()
            provider.aggregated_daily_candles("AAPL", "1M", limit=160)

        self.assertIn("event_time >= parseDateTime64BestEffort({historyFromTime:String})", provider.last_query)
        self.assertEqual(provider.last_parameters["historyFromTime"], "2020-07-01T00:00:00.000Z")

    def test_latest_chart_source_uses_canonical_feed_priority(self) -> None:
        provider = CapturingClickHouseProvider()

        query = provider.latest_chart_candles_source("symbol = {symbol:String}")

        self.assertIn("market_session = 'overnight'", query)
        self.assertIn("feed_profile = 'sip'", query)
        self.assertIn("feed_profile = 'boats'", query)
        self.assertIn("ORDER BY if(market_session = 'overnight'", query)
        self.assertIn("inserted_at DESC", query)

    def test_daily_query_buckets_by_trading_date_string(self) -> None:
        provider = CapturingClickHouseProvider()

        provider.daily_candles(
            "AAPL",
            "1D",
            limit=10,
            from_time="2024-06-03T00:00:00.000Z",
            to_time="2024-06-08T00:00:00.000Z",
        )

        self.assertIn("toDate(event_time) AS bucket_date", provider.last_query)
        self.assertIn("'regular' AS marketSession", provider.last_query)
        self.assertIn("concat(toString(bucket_date), 'T00:00:00.000Z') AS timestamp", provider.last_query)
        self.assertIn("event_time < parseDateTime64BestEffort({toTime:String})", provider.last_query)

    def test_weekly_monthly_query_buckets_from_daily_dates(self) -> None:
        provider = CapturingClickHouseProvider()

        provider.aggregated_daily_candles(
            "AAPL",
            "1W",
            limit=10,
            from_time="2024-01-03T00:00:00.000Z",
            to_time="2024-01-17T00:00:00.000Z",
        )
        self.assertIn("toMonday(toDate(event_time)) AS bucket_date", provider.last_query)
        self.assertIn("toDateTime64(toMonday(toDate(event_time)), 3, 'UTC') AS bucket_timestamp", provider.last_query)
        self.assertIn("bucket_timestamp >= parseDateTime64BestEffort({bucketFromTime:String})", provider.last_query)
        self.assertIn("bucket_timestamp < parseDateTime64BestEffort({bucketToTime:String})", provider.last_query)
        self.assertIn("event_time >= parseDateTime64BestEffort({sourceFromTime:String})", provider.last_query)
        self.assertIn("event_time < parseDateTime64BestEffort({sourceToTime:String})", provider.last_query)
        self.assertNotIn("event_time >= parseDateTime64BestEffort({fromTime:String})", provider.last_query)
        self.assertNotIn("event_time < parseDateTime64BestEffort({toTime:String})", provider.last_query)
        self.assertEqual(provider.last_parameters["sourceFromTime"], "2024-01-01T00:00:00.000Z")
        self.assertEqual(provider.last_parameters["sourceToTime"], "2024-01-22T00:00:00.000Z")
        self.assertEqual(provider.last_parameters["bucketFromTime"], "2024-01-03T00:00:00.000Z")
        self.assertEqual(provider.last_parameters["bucketToTime"], "2024-01-17T00:00:00.000Z")

        provider.aggregated_daily_candles(
            "AAPL",
            "1M",
            limit=10,
            from_time="2024-01-15T00:00:00.000Z",
            to_time="2024-04-01T00:00:00.000Z",
        )
        self.assertIn("toStartOfMonth(toDate(event_time)) AS bucket_date", provider.last_query)
        self.assertIn("toDateTime64(toStartOfMonth(toDate(event_time)), 3, 'UTC') AS bucket_timestamp", provider.last_query)
        self.assertIn("bucket_timestamp >= parseDateTime64BestEffort({bucketFromTime:String})", provider.last_query)
        self.assertIn("bucket_timestamp < parseDateTime64BestEffort({bucketToTime:String})", provider.last_query)
        self.assertEqual(provider.last_parameters["sourceFromTime"], "2024-01-01T00:00:00.000Z")
        self.assertEqual(provider.last_parameters["sourceToTime"], "2024-04-01T00:00:00.000Z")
        self.assertEqual(provider.last_parameters["bucketFromTime"], "2024-01-15T00:00:00.000Z")
        self.assertEqual(provider.last_parameters["bucketToTime"], "2024-04-01T00:00:00.000Z")

    def test_intraday_range_uses_exclusive_to_time(self) -> None:
        provider = CapturingClickHouseProvider()

        provider.candles(
            "AAPL",
            "1m",
            limit=10,
            from_time="2026-06-29T13:30:00.000Z",
            to_time="2026-06-29T14:00:00.000Z",
        )

        self.assertIn("event_time >= parseDateTime64BestEffort({fromTime:String})", provider.last_query)
        self.assertIn("event_time < parseDateTime64BestEffort({toTime:String})", provider.last_query)

    def test_daily_coverage_uses_canonical_daily_dates(self) -> None:
        provider = CapturingClickHouseProvider()

        provider.candle_coverage("AAPL", "1D")

        self.assertIn("uniqExactIf(toDate(event_time)", provider.last_query)
        self.assertIn("concat(toString(minIf(toDate(event_time)", provider.last_query)
        self.assertIn("'T00:00:00.000Z') AS availableFrom", provider.last_query)

    def test_weekly_monthly_coverage_reads_daily_source_interval(self) -> None:
        provider = CapturingClickHouseProvider()

        provider.candle_coverage("AAPL", "1W")

        self.assertIn("interval IN ('1D', '1d')", provider.last_query)
        self.assertNotIn("interval = {interval:String}", provider.last_query)
        self.assertNotIn("interval", provider.last_parameters)

    def test_daily_timestamps_are_distinct_canonical_dates(self) -> None:
        provider = CapturingClickHouseProvider()

        provider.candle_timestamps(
            "AAPL",
            "1D",
            "2026-01-01T00:00:00.000Z",
            "2026-06-30T00:00:00.000Z",
        )

        self.assertIn("SELECT\n          DISTINCT concat(toString(toDate(event_time)), 'T00:00:00.000Z') AS timestamp", provider.last_query)
        self.assertIn("ORDER BY timestamp ASC", provider.last_query)
        self.assertNotIn("interval", provider.last_parameters)

    def test_feed_profile_priority_keeps_unknown_fallback(self) -> None:
        self.assertEqual(feed_profile_priority("iex,sip,iex", "sip"), ["iex", "sip", "unknown"])

    def test_hot_symbols_default_to_top_ten_and_use_vwap_dollar_volume(self) -> None:
        provider = CapturingClickHouseProvider()

        provider.hot_symbols_by_dollar_volume(["AAPL", "MSFT"])

        self.assertEqual(provider.last_parameters["limit"], 10)
        self.assertIn("sum(toFloat64(row_volume) * coalesce(vwap, close)) AS sessionDollarVolume", provider.last_query)
        self.assertIn("vwap,", provider.last_query)


if __name__ == "__main__":
    unittest.main()
