from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "systems/market-data/shared"))

from alfaka.tools.repair_daily_candle_timestamps import (  # noqa: E402
    canonical_insert_sql,
    delete_non_midnight_sql,
    summary_sql,
)


class DailyTimestampRepairToolTests(unittest.TestCase):
    def test_summary_targets_only_non_midnight_daily_rows(self) -> None:
        sql = summary_sql("market_data.chart_candles", ["AAPL"])

        self.assertIn("interval IN ('1D', '1d')", sql)
        self.assertIn("formatDateTime(event_time, '%H:%i:%S', 'UTC') != '00:00:00'", sql)
        self.assertIn("symbol IN {symbols:Array(String)}", sql)

    def test_insert_rewrites_daily_rows_to_utc_midnight_bucket(self) -> None:
        sql = canonical_insert_sql("market_data.chart_candles", [])

        self.assertIn("INSERT INTO market_data.chart_candles", sql)
        self.assertIn("concat(toString(bucket_date), 'T00:00:00.000Z')", sql)
        self.assertIn("argMax(", sql)
        self.assertNotIn("symbol IN {symbols:Array(String)}", sql)

    def test_delete_removes_only_legacy_non_midnight_daily_rows(self) -> None:
        sql = delete_non_midnight_sql("market_data.chart_candles", ["AAPL", "MSFT"])

        self.assertIn("ALTER TABLE market_data.chart_candles", sql)
        self.assertIn("DELETE WHERE interval IN ('1D', '1d')", sql)
        self.assertIn("formatDateTime(event_time, '%H:%i:%S', 'UTC') != '00:00:00'", sql)
        self.assertIn("symbol IN {symbols:Array(String)}", sql)


if __name__ == "__main__":
    unittest.main()
