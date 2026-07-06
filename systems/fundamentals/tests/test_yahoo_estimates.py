import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))

from fundamentals.yahoo_estimates import YahooEstimatesConfig, dedupe_rows, estimate_row, period_from_yahoo_label, run_yahoo_estimates_sync


class FakeClickHouseClient:
    def __init__(self):
        self.executed = []
        self.inserted = []

    def execute(self, query, parameters=None):
        self.executed.append(query)

    def insert_json_each_row(self, table, rows):
        self.inserted.append((table, list(rows)))


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


if __name__ == "__main__":
    unittest.main()
