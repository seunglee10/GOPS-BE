from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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


class BootstrapClickHouseClient(RecordingClickHouseClient):
    def __init__(self, table_columns, existing=None):
        super().__init__()
        self.table_columns = set(table_columns)
        self.existing = set(existing or [])

    def query_json_each_row(self, query, parameters=None):
        if query.lstrip().startswith("DESCRIBE TABLE"):
            return [{"name": name} for name in sorted(self.table_columns | {"inserted_at"})]
        if "AS eventTime" in query:
            return [{"eventTime": value} for value in sorted(self.existing)]
        raise AssertionError(f"Unexpected query: {query}")


class CandleBootstrapJobTest(unittest.TestCase):
    def test_parse_intervals_normalizes_month_alias_and_deduplicates(self):
        self.assertEqual(
            MODULE.parse_intervals("1m,5m,1mo,1M"),
            ("1m", "5m", "1M"),
        )
        self.assertEqual(MODULE.parse_intervals(None), MODULE.DEFAULT_INTERVALS)

    def test_explicit_symbols_are_normalized_without_reading_universe(self):
        self.assertEqual(MODULE.parse_symbols("aapl,NVDA,AAPL"), ("AAPL", "NVDA"))

    def test_default_range_uses_last_completed_extended_session(self):
        now = datetime(2026, 7, 11, 3, 0, tzinfo=timezone.utc)

        start, end = MODULE.default_bootstrap_range(now, lookback_days=365)

        self.assertEqual(end, "2026-07-11T00:00:00.000Z")
        self.assertEqual(start, "2025-07-11T00:00:00.000Z")

    def test_parse_symbols_reads_repository_shaped_universe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "universe.json"
            path.write_text(json.dumps({"symbols": ["aapl", "NVDA", "AAPL"]}), encoding="utf-8")

            symbols = MODULE.parse_symbols(None, universe_path=path)

        self.assertEqual(symbols, ("AAPL", "NVDA"))

    def test_resolve_bootstrap_range_validates_explicit_bounds(self):
        self.assertEqual(
            MODULE.resolve_bootstrap_range(
                start="2025-07-11T00:00:00Z",
                end="2026-07-11T00:00:00Z",
                lookback_days=365,
            ),
            ("2025-07-11T00:00:00.000Z", "2026-07-11T00:00:00.000Z"),
        )
        with self.assertRaisesRegex(ValueError, "provided together"):
            MODULE.resolve_bootstrap_range(start="2025-07-11T00:00:00Z", end=None, lookback_days=365)
        with self.assertRaisesRegex(ValueError, "earlier"):
            MODULE.resolve_bootstrap_range(
                start="2026-07-11T00:00:00Z",
                end="2025-07-11T00:00:00Z",
                lookback_days=365,
            )

    def test_moving_average_warmup_start_extends_every_interval(self):
        target_start = "2025-07-11T00:00:00.000Z"
        target_time = MODULE.parse_time(target_start)

        for interval in MODULE.DEFAULT_INTERVALS:
            with self.subTest(interval=interval):
                warmup_time = MODULE.parse_time(
                    MODULE.moving_average_warmup_start(interval, target_start)
                )
                self.assertLess(warmup_time, target_time)

        monthly_warmup = MODULE.parse_time(
            MODULE.moving_average_warmup_start("1M", target_start)
        )
        self.assertLessEqual(monthly_warmup, target_time - timedelta(days=59 * 31))

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

    def test_insert_rows_flushes_multiple_batches(self):
        client = RecordingClickHouseClient()
        rows = [
            {"event_time": f"2026-07-10 13:{minute:02d}:00.000", "symbol": "AAPL", "interval": "1m"}
            for minute in range(5)
        ]

        inserted = MODULE.insert_missing_rows(
            client,
            rows,
            existing_timestamps=set(),
            batch_size=2,
            token_prefix="bootstrap:AAPL:1m",
        )

        self.assertEqual(inserted, 5)
        self.assertEqual([len(item[1]) for item in client.inserts], [2, 2, 1])

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

    def test_bootstrap_dry_run_does_not_fetch_or_insert(self):
        table_columns = set(MODULE.CORE_CLICKHOUSE_COLUMNS)
        client = BootstrapClickHouseClient(table_columns)

        def fail_fetch(*args, **kwargs):
            raise AssertionError("dry-run must not call Alpaca")

        summary = MODULE.bootstrap(
            symbols=("AAPL",),
            intervals=("1M",),
            start="2025-07-11T00:00:00.000Z",
            end="2026-07-11T00:00:00.000Z",
            feed="sip",
            apply=False,
            insert_batch_size=100,
            timestamp_limit=500_000,
            continue_on_error=False,
            client=client,
            fetcher=fail_fetch,
        )

        self.assertEqual(summary["mode"], "dry-run")
        self.assertEqual(summary["insertedRows"], 0)
        self.assertEqual(client.inserts, [])

    def test_bootstrap_apply_inserts_only_missing_canonical_rows(self):
        table_columns = {
            "event_time", "symbol", "interval", "open", "high", "low", "close", "volume",
            "trade_count", "vwap", "ma5", "ma20", "ma60", "is_closed", "correction_type",
            "source", "feed", "feed_profile", "market_session", "price_adjustment",
            "canonical_version", "source_event_id", "created_at",
        }
        client = BootstrapClickHouseClient(
            table_columns,
            existing={"2026-07-10 13:30:00.000"},
        )

        def fetch(symbol, start, end, feed, timeframe):
            self.assertEqual((symbol, feed, timeframe), ("AAPL", "sip", "1Min"))
            return [
                {"t": "2026-07-10T13:30:00Z", "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 1000},
                {"t": "2026-07-10T13:31:00Z", "o": 100.5, "h": 102, "l": 100, "c": 101, "v": 2000},
            ]

        summary = MODULE.bootstrap(
            symbols=("AAPL",),
            intervals=("1m",),
            start="2025-07-11T00:00:00.000Z",
            end="2026-07-11T00:00:00.000Z",
            feed="sip",
            apply=True,
            insert_batch_size=100,
            timestamp_limit=500_000,
            continue_on_error=False,
            client=client,
            fetcher=fetch,
        )

        self.assertEqual(summary["fetchedRows"], 2)
        self.assertEqual(summary["insertedRows"], 1)
        self.assertEqual(summary["skippedExistingRows"], 1)
        self.assertEqual(client.inserts[0][1][0]["event_time"], "2026-07-10 13:31:00.000")

    def test_bootstrap_uses_warmup_bars_but_stores_only_target_range(self):
        table_columns = {
            "event_time", "symbol", "interval", "open", "high", "low", "close", "volume",
            "trade_count", "vwap", "ma5", "ma20", "ma60", "is_closed", "correction_type",
            "source", "feed", "feed_profile", "market_session", "price_adjustment",
            "canonical_version", "source_event_id", "created_at",
        }
        client = BootstrapClickHouseClient(table_columns)
        first_bar_time = datetime(2026, 7, 10, 13, 30, tzinfo=timezone.utc)
        target_start_time = first_bar_time + timedelta(minutes=59)
        target_start = target_start_time.isoformat().replace("+00:00", "Z")
        target_end = (target_start_time + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        raw_bars = [
            {
                "t": (first_bar_time + timedelta(minutes=index)).isoformat().replace("+00:00", "Z"),
                "o": index + 1,
                "h": index + 2,
                "l": index + 0.5,
                "c": index + 1,
                "v": 1000,
            }
            for index in range(60)
        ]
        requested_ranges = []

        def fetch(symbol, start, end, feed, timeframe):
            requested_ranges.append((start, end))
            return raw_bars

        summary = MODULE.bootstrap(
            symbols=("AAPL",),
            intervals=("1m",),
            start=target_start,
            end=target_end,
            feed="sip",
            apply=True,
            insert_batch_size=100,
            timestamp_limit=500_000,
            continue_on_error=False,
            client=client,
            fetcher=fetch,
        )

        self.assertLess(MODULE.parse_time(requested_ranges[0][0]), MODULE.parse_time(target_start))
        self.assertEqual(requested_ranges[0][1], target_end)
        self.assertEqual(summary["fetchedRows"], 1)
        self.assertEqual(summary["insertedRows"], 1)
        inserted_rows = client.inserts[0][1]
        self.assertEqual(len(inserted_rows), 1)
        self.assertEqual(inserted_rows[0]["event_time"], "2026-07-10 14:29:00.000")
        self.assertEqual(inserted_rows[0]["ma5"], 58.0)
        self.assertEqual(inserted_rows[0]["ma20"], 50.5)
        self.assertEqual(inserted_rows[0]["ma60"], 30.5)

    def test_bootstrap_can_continue_after_one_symbol_fails(self):
        client = BootstrapClickHouseClient(set(MODULE.CORE_CLICKHOUSE_COLUMNS))

        def fetch(symbol, start, end, feed, timeframe):
            if symbol == "AAPL":
                raise RuntimeError("provider unavailable")
            return []

        summary = MODULE.bootstrap(
            symbols=("AAPL", "NVDA"),
            intervals=("1D",),
            start="2025-07-11T00:00:00.000Z",
            end="2026-07-11T00:00:00.000Z",
            feed="sip",
            apply=True,
            insert_batch_size=100,
            timestamp_limit=500_000,
            continue_on_error=True,
            client=client,
            fetcher=fetch,
        )

        self.assertEqual(len(summary["failures"]), 1)
        self.assertEqual(summary["failures"][0]["symbol"], "AAPL")

    def test_parser_requires_apply_for_writes(self):
        parser = MODULE.build_parser()

        self.assertFalse(parser.parse_args([]).apply)
        self.assertTrue(parser.parse_args(["--apply"]).apply)


if __name__ == "__main__":
    unittest.main()
