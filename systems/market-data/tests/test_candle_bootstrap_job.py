from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SHARED_ROOT = REPO_ROOT / "systems" / "market-data" / "shared"
JOB_PATH = REPO_ROOT / "systems" / "market-data" / "jobs" / "candle-bootstrap" / "main.py"

sys.path.insert(0, str(SHARED_ROOT))

SPEC = importlib.util.spec_from_file_location("candle_bootstrap_job", JOB_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class RecordingClickHouseClient:
    database = "market_data"

    def __init__(self):
        self.inserts: list[tuple[str, list[dict[str, object]], str | None]] = []

    def insert_json_each_row(self, table, rows, deduplication_token=None):
        self.inserts.append((table, list(rows), deduplication_token))


class CandleBootstrapJobTest(unittest.TestCase):
    def test_parse_intervals_normalizes_month_alias_and_deduplicates(self):
        self.assertEqual(
            MODULE.parse_intervals("1m,5m,1mo,1M"),
            ("1m", "5m", "1M"),
        )

    def test_default_range_uses_last_completed_extended_session(self):
        now = datetime(2026, 7, 11, 3, 0, tzinfo=timezone.utc)

        start, end = MODULE.default_bootstrap_range(now, lookback_days=365)

        self.assertEqual(end, "2026-07-11T00:00:00.000Z")
        self.assertEqual(start, "2025-07-11T00:00:00.000Z")

    def test_prepare_rows_reuses_canonical_clickhouse_shape(self):
        raw_bars = [{
            "t": "2026-07-10T13:30:00Z",
            "o": 100.0,
            "h": 102.0,
            "l": 99.5,
            "c": 101.5,
            "v": 12345,
            "n": 321,
            "vw": 100.9,
        }]
        table_columns = {
            "event_time", "symbol", "interval", "open", "high", "low", "close", "volume",
            "trade_count", "vwap", "ma5", "ma20", "ma60", "is_closed", "correction_type",
            "source", "feed", "feed_profile", "market_session", "price_adjustment",
            "canonical_version", "source_event_id", "created_at",
        }

        rows = MODULE.prepare_clickhouse_rows(
            "AAPL",
            "1m",
            raw_bars,
            feed="sip",
            table_columns=table_columns,
            received_at="2026-07-11T00:00:00.000Z",
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(set(row), table_columns)
        self.assertEqual(row["symbol"], "AAPL")
        self.assertEqual(row["interval"], "1m")
        self.assertEqual(row["volume"], 12345.0)
        self.assertEqual(row["trade_count"], 321)
        self.assertEqual(row["vwap"], 100.9)
        self.assertEqual(row["market_session"], "regular")
        self.assertEqual(row["price_adjustment"], "split")
        self.assertEqual(row["canonical_version"], "v2")
        self.assertNotIn("simulation_run_id", row)

    def test_insert_rows_filters_existing_timestamps_and_uses_deduplication_token(self):
        client = RecordingClickHouseClient()
        rows = [
            {"event_time": "2026-07-10 13:30:00.000", "symbol": "AAPL", "interval": "1m"},
            {"event_time": "2026-07-10 13:31:00.000", "symbol": "AAPL", "interval": "1m"},
        ]

        inserted = MODULE.insert_missing_rows(
            client,
            rows,
            existing_timestamps={"2026-07-10 13:30:00.000"},
            batch_size=100,
            token_prefix="bootstrap:AAPL:1m",
        )

        self.assertEqual(inserted, 1)
        self.assertEqual(len(client.inserts), 1)
        table, inserted_rows, token = client.inserts[0]
        self.assertEqual(table, "chart_candles")
        self.assertEqual(inserted_rows, [rows[1]])
        self.assertTrue(token.startswith("bootstrap:AAPL:1m:"))

    def test_monthly_rows_are_marked_as_regular_session(self):
        rows = MODULE.prepare_clickhouse_rows(
            "AAPL",
            "1M",
            [{"t": "2026-06-01T04:00:00Z", "o": 100, "h": 110, "l": 95, "c": 108, "v": 5000}],
            feed="sip",
            table_columns={"event_time", "symbol", "interval", "market_session", "volume"},
            received_at="2026-07-11T00:00:00.000Z",
        )

        self.assertEqual(rows[0]["interval"], "1M")
        self.assertEqual(rows[0]["market_session"], "regular")


if __name__ == "__main__":
    unittest.main()
