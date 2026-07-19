import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))

from fundamentals.yahoo_estimates import (
    YahooEstimatesConfig,
    build_analyst_summary_row,
    dedupe_rows,
    earnings_event_datetime,
    estimate_row,
    fetch_yfinance_analyst_summary,
    fetch_yfinance_estimate_rows,
    period_from_yahoo_label,
    rows_from_analyst_consensus,
    rows_from_earnings_dates,
    rows_from_upgrades_downgrades,
    run_yahoo_estimates_sync,
    safe_call,
    yahoo_provider_symbol,
)
from fundamentals.schema import CLICKHOUSE_TABLES


class FakeClickHouseClient:
    def __init__(self):
        self.executed = []
        self.inserted = []

    def execute(self, query, parameters=None):
        self.executed.append(query)

    def insert_json_each_row(self, table, rows):
        self.inserted.append((table, list(rows)))


class FakeFrame:
    def __init__(self, rows):
        self.rows = rows

    def iterrows(self):
        return iter(self.rows)


class YahooEstimatesTests(unittest.TestCase):
    def test_analyst_summary_schema_has_one_day_ttl_and_no_raw_payload(self):
        ddl = CLICKHOUSE_TABLES["yahoo_analyst_summaries"]

        self.assertIn("ORDER BY symbol", ddl)
        self.assertIn("TTL toDateTime(collected_at) + INTERVAL 1 DAY DELETE", ddl)
        self.assertNotIn("raw String", ddl)

    def test_estimate_fetcher_uses_yahoo_class_share_alias_and_preserves_canonical_symbol(self):
        requested_symbols = []

        class FakeTicker:
            def __init__(self, symbol):
                requested_symbols.append(symbol)

            def get_earnings_estimate(self):
                return FakeFrame([("0q", {"avg": 2.5, "low": 2.0, "high": 3.0})])

            def get_revenue_estimate(self):
                return None

            def get_earnings_dates(self, **_kwargs):
                return None

        collected_at = datetime(2026, 7, 18, tzinfo=timezone.utc)
        with patch.dict(sys.modules, {"yfinance": types.SimpleNamespace(Ticker=FakeTicker)}):
            class_share_rows = fetch_yfinance_estimate_rows("BRK.B", collected_at=collected_at)
            ordinary_rows = fetch_yfinance_estimate_rows("AAPL", collected_at=collected_at)

        self.assertEqual(requested_symbols, ["BRK-B", "AAPL"])
        self.assertEqual(class_share_rows[0]["symbol"], "BRK.B")
        self.assertEqual(ordinary_rows[0]["symbol"], "AAPL")

    def test_analyst_fetcher_uses_yahoo_class_share_alias_and_preserves_canonical_symbol(self):
        requested_symbols = []

        class FakeTicker:
            def __init__(self, symbol):
                requested_symbols.append(symbol)

            def get_upgrades_downgrades(self):
                return FakeFrame([(
                    datetime(2026, 7, 17, tzinfo=timezone.utc),
                    {"Firm": "JPMorgan", "Action": "main", "ToGrade": "Overweight"},
                )])

            def get_analyst_price_targets(self):
                return {"current": 190, "mean": 210}

            def get_recommendations_summary(self):
                return FakeFrame([("0m", {"strongBuy": 2, "buy": 3, "hold": 1})])

        collected_at = datetime(2026, 7, 18, tzinfo=timezone.utc)
        with patch.dict(sys.modules, {"yfinance": types.SimpleNamespace(Ticker=FakeTicker)}):
            class_share_summary = fetch_yfinance_analyst_summary("BF.B", collected_at=collected_at)
            ordinary_summary = fetch_yfinance_analyst_summary("AAPL", collected_at=collected_at)

        self.assertEqual(requested_symbols, ["BF-B", "AAPL"])
        self.assertEqual(class_share_summary["symbol"], "BF.B")
        self.assertIn("JPMorgan", class_share_summary["statement"])
        self.assertIn("$210", class_share_summary["statement"])
        self.assertEqual(ordinary_summary["symbol"], "AAPL")
        self.assertEqual(yahoo_provider_symbol(" brk.b "), "BRK-B")

    def test_safe_call_isolates_optional_yahoo_endpoint_failure(self):
        class PartialTicker:
            def get_earnings_dates(self, **_kwargs):
                raise ImportError("optional parser is unavailable")

        self.assertIsNone(safe_call(PartialTicker(), "get_earnings_dates", limit=16))

    def test_safe_call_retries_without_kwargs_for_legacy_method(self):
        class LegacyTicker:
            def get_earnings_dates(self, *args, **kwargs):
                if kwargs:
                    raise TypeError("kwargs are unsupported")
                return "legacy-result"

        self.assertEqual(safe_call(LegacyTicker(), "get_earnings_dates", limit=16), "legacy-result")

    def test_period_from_yahoo_label_uses_stable_quarter_keys(self):
        fiscal_year, fiscal_period, period_end = period_from_yahoo_label("+1q", datetime(2026, 7, 6, tzinfo=timezone.utc).date())

        self.assertEqual(fiscal_year, 2026)
        self.assertEqual(fiscal_period, "Q4")
        self.assertEqual(period_end.isoformat(), "2026-12-31")

    def test_run_sync_inserts_estimate_rows(self):
        client = FakeClickHouseClient()
        collected_at = datetime(2026, 7, 6, tzinfo=timezone.utc)

        def fetcher(symbol, *, collected_at):
            return [
                estimate_row(
                    symbol=symbol,
                    metric="eps",
                    period=(2026, "Q3", datetime(2026, 9, 30, tzinfo=timezone.utc).date()),
                    average=1.23,
                    low=1.0,
                    high=1.5,
                    analyst_count=12,
                    collected_at=collected_at,
                    raw={"fixture": True},
                )
            ]

        stats = run_yahoo_estimates_sync(
            YahooEstimatesConfig(dry_run=False, symbols=["NVDA"], batch_size=10),
            clickhouse_client=client,
            fetcher=fetcher,
            analyst_fetcher=lambda symbol, collected_at: None,
        )

        self.assertEqual(stats.rows, 1)
        self.assertEqual(client.inserted[0][0], "yahoo_earnings_estimates")
        self.assertEqual(client.inserted[0][1][0]["symbol"], "NVDA")
        self.assertEqual(client.inserted[0][1][0]["average"], 1.23)
        self.assertTrue(any("ADD COLUMN IF NOT EXISTS event_at" in query for query in client.executed))
        self.assertTrue(any("DROP TABLE IF EXISTS market_data.yahoo_analyst_actions" in query for query in client.executed))

    def test_yahoo_analyst_payload_is_compacted_without_raw_columns(self):
        collected_at = datetime(2026, 7, 18, tzinfo=timezone.utc)
        actions = rows_from_upgrades_downgrades(
            "NVDA",
            FakeFrame([(
                datetime(2026, 7, 17, tzinfo=timezone.utc),
                {
                    "Firm": "Morgan Stanley",
                    "Action": "main",
                    "FromGrade": "Overweight",
                    "ToGrade": "Overweight",
                    "PriorPriceTarget": 180,
                    "CurrentPriceTarget": 200,
                },
            )]),
            collected_at=collected_at,
        )
        consensus = rows_from_analyst_consensus(
            "NVDA",
            {"current": 190, "low": 150, "high": 250, "mean": 210, "median": 205},
            FakeFrame([("0m", {"strongBuy": 12, "buy": 20, "hold": 5, "sell": 1, "strongSell": 0})]),
            collected_at=collected_at,
        )

        self.assertEqual(actions[0]["firm"], "Morgan Stanley")
        self.assertEqual(actions[0]["prior_price_target"], 180)
        self.assertEqual(actions[0]["price_target"], 200)
        self.assertEqual(consensus[0]["target_mean"], 210)
        self.assertEqual(consensus[0]["strong_buy"], 12)
        self.assertEqual(consensus[0]["source"], "yahoo-finance")
        self.assertNotIn("raw", actions[0])
        self.assertNotIn("raw", consensus[0])

        summary = build_analyst_summary_row("NVDA", actions, consensus, collected_at=collected_at)
        self.assertEqual(summary["symbol"], "NVDA")
        self.assertEqual(summary["tone"], "neutral")
        self.assertIn("Morgan Stanley", summary["statement"])
        self.assertIn("$180에서 $200로 상향", summary["statement"])
        self.assertIn("시장 평균 목표주가는 $210", summary["statement"])
        self.assertFalse(summary["statement"].startswith("2026-"))
        self.assertNotIn("raw", summary)

    def test_run_sync_inserts_one_analyst_summary_and_removes_legacy_tables(self):
        client = FakeClickHouseClient()
        collected_at = datetime(2026, 7, 18, tzinfo=timezone.utc)
        estimate = estimate_row(
            symbol="NVDA",
            metric="eps",
            period=(2026, "Q3", datetime(2026, 9, 30, tzinfo=timezone.utc).date()),
            average=1.23,
            low=1.0,
            high=1.5,
            analyst_count=12,
            collected_at=collected_at,
            raw={},
        )
        summary = {
            "symbol": "NVDA",
            "statement": "JPMorgan은 NVDA의 투자의견 Overweight를 유지했습니다.",
            "tone": "neutral",
            "source_as_of": "2026-07-17 00:00:00.000",
            "source": "yahoo-finance",
            "collected_at": "2026-07-18 00:00:00.000",
        }

        stats = run_yahoo_estimates_sync(
            YahooEstimatesConfig(dry_run=False, symbols=["NVDA"], batch_size=10),
            clickhouse_client=client,
            fetcher=lambda symbol, collected_at: [estimate],
            analyst_fetcher=lambda symbol, collected_at: summary,
        )

        self.assertEqual(stats.analyst_symbols_loaded, 1)
        self.assertEqual(stats.analyst_summary_rows, 1)
        self.assertEqual([table for table, _ in client.inserted], [
            "yahoo_earnings_estimates", "yahoo_analyst_summaries",
        ])
        self.assertTrue(any("DROP TABLE IF EXISTS market_data.yahoo_analyst_actions" in query for query in client.executed))
        self.assertTrue(any("DROP TABLE IF EXISTS market_data.yahoo_analyst_consensus" in query for query in client.executed))
        self.assertTrue(any("OPTIMIZE TABLE market_data.yahoo_analyst_summaries FINAL" in query for query in client.executed))

    def test_dedupe_rows_keeps_one_row_per_clickhouse_replacing_key(self):
        first = {
            "symbol": "NVDA",
            "metric": "eps",
            "fiscal_year": 2026,
            "fiscal_period": "Q3",
            "period_end": "2026-09-30",
            "average": 1.0,
        }
        second = {**first, "average": 1.1}

        rows = dedupe_rows([first, second])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["average"], 1.1)

    def test_earnings_dates_preserve_reported_eps_surprise_and_after_hours_time(self):
        collected_at = datetime(2026, 7, 16, tzinfo=timezone.utc)
        frame = FakeFrame([(
            datetime.fromisoformat("2026-07-29T16:05:00-04:00"),
            {"EPS Estimate": 1.25, "Reported EPS": 1.40, "Surprise(%)": 12.0},
        )])

        rows = rows_from_earnings_dates("AMD", frame, collected_at=collected_at)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fiscal_period"], "EVENT")
        self.assertEqual(rows[0]["event_at"], "2026-07-29 20:05:00.000")
        self.assertEqual(rows[0]["event_session"], "after")
        self.assertEqual(rows[0]["event_status"], "reported")
        self.assertEqual(rows[0]["actual_value"], 1.4)
        self.assertEqual(rows[0]["surprise_percent"], 12.0)

    def test_earnings_dates_calculate_surprise_for_scheduled_and_reported_rows(self):
        collected_at = datetime(2026, 7, 16, tzinfo=timezone.utc)
        frame = FakeFrame([
            (datetime.fromisoformat("2026-07-29T08:00:00-04:00"), {"EPS Estimate": 2.0, "Reported EPS": 2.2}),
            (datetime.fromisoformat("2026-10-29T08:00:00-04:00"), {"EPS Estimate": 2.4}),
        ])

        rows = rows_from_earnings_dates("AMD", frame, collected_at=collected_at)

        self.assertAlmostEqual(rows[0]["surprise_percent"], 10.0)
        self.assertEqual(rows[0]["event_session"], "pre")
        self.assertEqual(rows[0]["event_status"], "reported")
        self.assertEqual(rows[1]["event_status"], "scheduled")
        self.assertIsNone(rows[1]["actual_value"])

    def test_earnings_event_time_classifies_regular_and_date_only_unknown(self):
        regular_at, regular_session = earnings_event_datetime(datetime.fromisoformat("2026-07-29T12:30:00-04:00"))
        date_only_at, date_only_session = earnings_event_datetime("2026-10-29")

        self.assertEqual(regular_at.isoformat(), "2026-07-29T16:30:00+00:00")
        self.assertEqual(regular_session, "regular")
        self.assertEqual(date_only_at.isoformat(), "2026-10-29T04:00:00+00:00")
        self.assertEqual(date_only_session, "unknown")

    def test_run_sync_fails_when_universe_is_empty(self):
        with self.assertRaisesRegex(RuntimeError, "universe is empty"):
            run_yahoo_estimates_sync(
                YahooEstimatesConfig(dry_run=False, symbols=[" "]),
                clickhouse_client=FakeClickHouseClient(),
            )

    def test_run_sync_fails_when_every_symbol_returns_zero_rows(self):
        with self.assertRaisesRegex(RuntimeError, "produced zero rows"):
            run_yahoo_estimates_sync(
                YahooEstimatesConfig(dry_run=False, symbols=["AMD"]),
                clickhouse_client=FakeClickHouseClient(),
                fetcher=lambda symbol, collected_at: [],
                analyst_fetcher=lambda symbol, collected_at: None,
            )


if __name__ == "__main__":
    unittest.main()
