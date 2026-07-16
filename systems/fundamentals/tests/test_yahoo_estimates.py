import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))

from fundamentals.yahoo_estimates import YahooEstimatesConfig, dedupe_rows, earnings_event_datetime, estimate_row, period_from_yahoo_label, rows_from_earnings_dates, run_yahoo_estimates_sync


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
        )

        self.assertEqual(stats.rows, 1)
        self.assertEqual(client.inserted[0][0], "yahoo_earnings_estimates")
        self.assertEqual(client.inserted[0][1][0]["symbol"], "NVDA")
        self.assertEqual(client.inserted[0][1][0]["average"], 1.23)
        self.assertTrue(any("ADD COLUMN IF NOT EXISTS event_at" in query for query in client.executed))

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
            )


if __name__ == "__main__":
    unittest.main()
