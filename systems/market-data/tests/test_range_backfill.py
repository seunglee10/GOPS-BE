from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "systems/market-data/shared"))

from alfaka.backfill.gapfill import TradingCalendar, expected_bucket_starts, to_bucket_start  # noqa: E402
from alfaka.backfill.runner import BackfillRunner, alpaca_fetch_ranges, raw_bar_to_processed_candle  # noqa: E402
from alfaka.backfill.status import BACKFILL_STATUS_SCHEMA_VERSION, RedisBackfillStore, resolve_backfill_range  # noqa: E402
from alfaka.backfill.worker import summarize_worker_result  # noqa: E402
from alfaka.storage.clickhouse_materializer import materialize_prepared_processed_rows  # noqa: E402
from alfaka.storage.processed_s3_archive import archive_processed_candles_to_s3  # noqa: E402


class FakeStore:
    def __init__(self) -> None:
        self.no_data: list[tuple[str, str, str]] = []

    def update_status(self, record, status, **fields):
        return {**record, **fields, "status": status}

    def record_no_data_before(self, symbol, interval, boundary):
        self.no_data.append((symbol, interval, boundary))
        return boundary


class FakeClickHouseClient:
    def __init__(self) -> None:
        self.inserted: list[tuple[str, list[dict]]] = []

    def ensure_market_data_schema(self):
        return None

    def insert_json_each_row(self, table, rows):
        self.inserted.append((table, rows))


class FakeS3:
    def __init__(self) -> None:
        self.objects: list[dict] = []

    def put_object(self, **kwargs):
        self.objects.append(kwargs)
        return {}


class FailingS3:
    def put_object(self, **kwargs):
        raise RuntimeError("archive unavailable")


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}

    def set(self, key, value, nx=False, ex=None):
        _ = ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])


class FakeCoverageProvider:
    def candle_coverage(self, symbol, interval):
        return {"rowCount": 0, "availableFrom": None, "availableTo": None}


class RecordingCoverageProvider(FakeCoverageProvider):
    def __init__(self) -> None:
        self.timestamp_calls: list[tuple[str, str, str, str]] = []

    def candle_timestamps(self, symbol, interval, start, end, limit=200000):
        self.timestamp_calls.append((symbol, interval, start, end))
        return []


class FullyCoveredCoverageProvider(FakeCoverageProvider):
    def candle_coverage(self, symbol, interval):
        return {
            "rowCount": 100,
            "availableFrom": "2026-01-01T00:00:00.000Z",
            "availableTo": "2026-02-01T00:00:00.000Z",
        }

    def candle_timestamps(self, symbol, interval, start, end, limit=200000):
        raise AssertionError("force backfill should not run gap detection")


class CoverageOnlyProvider:
    def __init__(self, coverage) -> None:
        self.coverage = coverage

    def candle_coverage(self, symbol, interval):
        return self.coverage


class RangeBackfillTests(unittest.TestCase):
    def test_worker_result_summary_keeps_large_ranges_out_of_stdout(self) -> None:
        summary = summarize_worker_result({
            "requestId": "backfill:AAPL:1D:test",
            "symbol": "AAPL",
            "interval": "1D",
            "status": "succeeded",
            "range": {
                "start": "2020-01-01T00:00:00.000Z",
                "end": "2020-01-10T00:00:00.000Z",
            },
            "result": {
                "source": "alpaca",
                "rawRowCount": 6,
                "processedRowCount": 6,
                "materializedRowCount": 6,
                "archiveStatus": "archived",
                "archiveObjects": ["market-data/dev/helixho/backfill/processed/part.jsonl"],
                "gapRanges": [
                    {
                        "start": "2020-01-01T00:00:00.000Z",
                        "end": "2020-01-03T00:00:00.000Z",
                        "missingCount": 2,
                    },
                    {
                        "start": "2020-01-06T00:00:00.000Z",
                        "end": "2020-01-10T00:00:00.000Z",
                        "missingCount": 4,
                    },
                ],
                "fetchRanges": [{
                    "start": "2020-01-01T00:00:00.000Z",
                    "end": "2020-01-10T00:00:00.000Z",
                }],
            },
        })

        self.assertEqual(summary["event"], "backfill.result")
        self.assertEqual(summary["gapRangeCount"], 2)
        self.assertEqual(summary["gapMissingCount"], 6)
        self.assertEqual(summary["fetchRangeCount"], 1)
        self.assertEqual(summary["archiveObjectCount"], 1)
        self.assertNotIn("gapRanges", summary)
        self.assertNotIn("fetchRanges", summary)
        self.assertNotIn("archiveObjects", summary)

    def test_resolve_backfill_range_requires_explicit_bounds(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit start and end"):
            resolve_backfill_range(interval="1m")

    def test_created_status_records_carry_schema_version(self) -> None:
        store = RedisBackfillStore(redis_client=FakeRedis(), queue_backend="list")

        record, deduplicated = store.create_request(
            "AAPL",
            "1m",
            start="2026-06-29T13:30:00.000Z",
            end="2026-06-29T14:30:00.000Z",
        )

        self.assertFalse(deduplicated)
        self.assertEqual(record["schemaVersion"], BACKFILL_STATUS_SCHEMA_VERSION)

    def test_runner_skips_alpaca_when_range_is_older_than_history_window(self) -> None:
        store = FakeStore()
        clickhouse = FakeClickHouseClient()
        runner = BackfillRunner(
            store=store,
            clickhouse_client=clickhouse,
            coverage_provider=FakeCoverageProvider(),
        )
        record = {
            "requestId": "backfill:AAPL:1D:test",
            "symbol": "AAPL",
            "interval": "1D",
            "range": {
                "start": "1990-01-01T00:00:00.000Z",
                "end": "1990-02-01T00:00:00.000Z",
            },
            "jobType": "gapfill",
            "sourcePreference": "alpaca-only",
        }

        with mock.patch.dict(os.environ, {
            "MARKET_DATA_MAX_HISTORY_YEARS": "6",
            "MARKET_DATA_HISTORY_NOW": "2026-07-01T00:00:00.000Z",
        }), mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", return_value=[]) as fetch:
            result = runner.run(record)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"]["source"], "history-window")
        self.assertEqual(result["result"]["noDataBefore"], "2020-07-01T00:00:00.000Z")
        self.assertEqual(store.no_data, [("AAPL", "1D", "2020-07-01T00:00:00.000Z")])
        self.assertEqual(clickhouse.inserted, [])
        fetch.assert_not_called()

    def test_runner_records_no_data_boundary_for_partial_alpaca_history(self) -> None:
        store = FakeStore()
        clickhouse = FakeClickHouseClient()
        runner = BackfillRunner(
            store=store,
            clickhouse_client=clickhouse,
            coverage_provider=FakeCoverageProvider(),
        )
        record = {
            "requestId": "backfill:AAPL:1D:partial",
            "symbol": "AAPL",
            "interval": "1D",
            "range": {
                "start": "2003-04-19T00:00:00.000Z",
                "end": "2023-09-01T00:00:00.000Z",
            },
            "jobType": "gapfill",
            "sourcePreference": "alpaca-only",
        }
        bars = [{
            "t": "2016-01-04T00:00:00Z",
            "o": 100,
            "h": 101,
            "l": 99,
            "c": 100.5,
            "v": 1000,
        }]

        with mock.patch.dict(os.environ, {"MARKET_DATA_MAX_HISTORY_YEARS": "0"}), mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", return_value=bars):
            result = runner.run(record)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"]["source"], "alpaca")
        self.assertTrue(result["result"]["partialHistoryBoundary"])
        self.assertEqual(result["result"]["earliestReturned"], "2016-01-04T00:00:00.000Z")
        self.assertEqual(result["result"]["noDataBefore"], "2016-01-04T00:00:00.000Z")
        self.assertGreater(result["result"]["missingBeforeCount"], 0)
        self.assertEqual(store.no_data, [("AAPL", "1D", "2016-01-04T00:00:00.000Z")])
        self.assertTrue(clickhouse.inserted)

    def test_runner_clamps_fetch_start_to_history_window(self) -> None:
        store = FakeStore()
        clickhouse = FakeClickHouseClient()
        runner = BackfillRunner(
            store=store,
            clickhouse_client=clickhouse,
            coverage_provider=FakeCoverageProvider(),
        )
        record = {
            "requestId": "backfill:AAPL:1D:history-window",
            "symbol": "AAPL",
            "interval": "1D",
            "range": {
                "start": "2015-01-01T00:00:00.000Z",
                "end": "2021-01-01T00:00:00.000Z",
            },
            "jobType": "gapfill",
            "sourcePreference": "alpaca-only",
        }
        bars = [{
            "t": "2020-07-01T00:00:00Z",
            "o": 100,
            "h": 101,
            "l": 99,
            "c": 100.5,
            "v": 1000,
        }]

        with mock.patch.dict(os.environ, {
            "MARKET_DATA_MAX_HISTORY_YEARS": "6",
            "MARKET_DATA_HISTORY_NOW": "2026-07-01T00:00:00.000Z",
        }), mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", return_value=bars) as fetch:
            result = runner.run(record)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(fetch.call_args.args[1], "2020-07-01T00:00:00.000Z")
        self.assertEqual(result["result"]["noDataBefore"], "2020-07-01T00:00:00.000Z")
        self.assertEqual(result["result"]["fetchRanges"][0]["start"], "2020-07-01T00:00:00.000Z")
        self.assertEqual(store.no_data, [("AAPL", "1D", "2020-07-01T00:00:00.000Z")])
        self.assertTrue(clickhouse.inserted)

    def test_runner_does_not_record_no_data_boundary_for_empty_middle_gap(self) -> None:
        store = FakeStore()
        runner = BackfillRunner(
            store=store,
            clickhouse_client=FakeClickHouseClient(),
            coverage_provider=FakeCoverageProvider(),
        )
        runner.detect_missing_ranges = lambda *_args, **_kwargs: [{
            "start": "2023-09-22T00:00:00.000Z",
            "end": "2023-09-23T00:00:00.000Z",
            "missingCount": 1,
        }]
        record = {
            "requestId": "backfill:AAPL:1D:middle-gap",
            "symbol": "AAPL",
            "interval": "1D",
            "range": {
                "start": "2023-01-01T00:00:00.000Z",
                "end": "2023-12-01T00:00:00.000Z",
            },
            "jobType": "gapfill",
            "sourcePreference": "coverage-first",
        }

        with mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", return_value=[]):
            result = runner.run(record)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"]["source"], "alpaca-empty")
        self.assertIsNone(result["result"]["noDataBefore"])
        self.assertIsNone(result["result"]["leadingMissingEdge"])
        self.assertEqual(store.no_data, [])

    def test_coverage_first_gapfill_materializes_alpaca_directly_to_clickhouse(self) -> None:
        clickhouse = FakeClickHouseClient()
        s3 = FakeS3()
        record = {
            "requestId": "backfill:AAPL:1m:test",
            "symbol": "AAPL",
            "interval": "1m",
            "range": {
                "start": "2026-06-29T13:30:00.000Z",
                "end": "2026-06-29T13:35:00.000Z",
            },
            "jobType": "gapfill",
            "sourcePreference": "coverage-first",
        }

        bars = [{
            "t": "2026-06-29T13:30:00Z",
            "o": 100,
            "h": 101,
            "l": 99,
            "c": 100.5,
            "v": 1000,
        }]
        env = {
            "S3_BUCKET": "test-bucket",
            "S3_BACKFILL_PROCESSED_PREFIX": "market-data/dev/helixho/backfill/processed",
            "S3_BACKFILL_PROCESSED_FORMAT": "jsonl",
            "S3_PUT_MAX_ATTEMPTS": "1",
        }
        with mock.patch.dict(os.environ, env), mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", return_value=bars):
            runner = BackfillRunner(
                store=FakeStore(),
                clickhouse_client=clickhouse,
                coverage_provider=FakeCoverageProvider(),
                s3_client=s3,
            )
            result = runner.run(record)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"]["source"], "alpaca")
        self.assertEqual(result["result"]["rawRowCount"], 1)
        self.assertEqual(result["result"]["materializedRowCount"], 1)
        self.assertEqual(result["result"]["archiveStatus"], "archived")
        self.assertEqual(result["result"]["archiveObjectCount"], 1)
        self.assertTrue(result["result"]["materializedSource"].startswith("alpaca://AAPL/1m/"))
        self.assertTrue(any(
            item["Key"].startswith("market-data/dev/helixho/backfill/processed/candles/interval=1m/symbol=AAPL/")
            for item in s3.objects
        ))
        inserted_tables = [table for table, _rows in clickhouse.inserted]
        self.assertIn("chart_candles", inserted_tables)
        self.assertIn("load_audit", inserted_tables)

    def test_force_backfill_refetches_full_range_even_when_clickhouse_is_covered(self) -> None:
        clickhouse = FakeClickHouseClient()
        record = {
            "requestId": "backfill:AAPL:1D:force",
            "symbol": "AAPL",
            "interval": "1D",
            "range": {
                "start": "2026-01-01T00:00:00.000Z",
                "end": "2026-02-01T00:00:00.000Z",
            },
            "jobType": "gapfill",
            "sourcePreference": "coverage-first",
            "force": True,
        }
        bars = [{
            "t": "2026-01-02T00:00:00Z",
            "o": 100,
            "h": 101,
            "l": 99,
            "c": 100.5,
            "v": 1000,
        }]

        with mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", return_value=bars) as fetch:
            runner = BackfillRunner(
                store=FakeStore(),
                clickhouse_client=clickhouse,
                coverage_provider=FullyCoveredCoverageProvider(),
            )
            result = runner.run(record)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"]["source"], "alpaca")
        self.assertTrue(result["result"]["force"])
        self.assertEqual(result["result"]["rawRowCount"], 1)
        self.assertEqual(result["result"]["fetchRanges"], [{
            "start": "2026-01-01T00:00:00.000Z",
            "end": "2026-02-01T00:00:00.000Z",
        }])
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[1], "2026-01-01T00:00:00.000Z")
        self.assertEqual(fetch.call_args.args[2], "2026-02-01T00:00:00.000Z")
        self.assertTrue(clickhouse.inserted)

    def test_coverage_fallback_treats_request_end_as_exclusive(self) -> None:
        cases = [
            (
                "1D",
                "2024-06-03T00:00:00.000Z",
                "2024-06-08T00:00:00.000Z",
                "2024-06-03T00:00:00.000Z",
                "2024-06-07T00:00:00.000Z",
            ),
            (
                "1m",
                "2026-06-29T13:30:00.000Z",
                "2026-06-29T13:32:00.000Z",
                "2026-06-29T13:30:00.000Z",
                "2026-06-29T13:31:00.000Z",
            ),
        ]

        for interval, start, end, available_from, available_to in cases:
            with self.subTest(interval=interval):
                record = {
                    "requestId": f"backfill:AAPL:{interval}:covered",
                    "symbol": "AAPL",
                    "interval": interval,
                    "range": {
                        "start": start,
                        "end": end,
                    },
                    "jobType": "gapfill",
                    "sourcePreference": "coverage-first",
                }
                runner = BackfillRunner(
                    store=FakeStore(),
                    clickhouse_client=FakeClickHouseClient(),
                    coverage_provider=CoverageOnlyProvider({
                        "rowCount": 100,
                        "availableFrom": available_from,
                        "availableTo": available_to,
                    }),
                )

                with mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", return_value=[]) as fetch:
                    result = runner.run(record)

                self.assertEqual(result["status"], "succeeded")
                self.assertEqual(result["result"]["source"], "clickhouse")
                self.assertTrue(result["result"]["skipped"])
                fetch.assert_not_called()

    def test_backfill_s3_archive_failure_does_not_fail_clickhouse_materialization(self) -> None:
        clickhouse = FakeClickHouseClient()
        record = {
            "requestId": "backfill:AAPL:1m:test",
            "symbol": "AAPL",
            "interval": "1m",
            "range": {
                "start": "2026-06-29T13:30:00.000Z",
                "end": "2026-06-29T13:35:00.000Z",
            },
            "jobType": "gapfill",
            "sourcePreference": "coverage-first",
        }
        bars = [{
            "t": "2026-06-29T13:30:00Z",
            "o": 100,
            "h": 101,
            "l": 99,
            "c": 100.5,
            "v": 1000,
        }]
        env = {
            "S3_BUCKET": "test-bucket",
            "S3_BACKFILL_PROCESSED_PREFIX": "market-data/dev/helixho/backfill/processed",
            "S3_BACKFILL_PROCESSED_FORMAT": "jsonl",
            "S3_PUT_MAX_ATTEMPTS": "1",
        }
        with mock.patch.dict(os.environ, env), mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", return_value=bars):
            runner = BackfillRunner(
                store=FakeStore(),
                clickhouse_client=clickhouse,
                coverage_provider=FakeCoverageProvider(),
                s3_client=FailingS3(),
            )
            result = runner.run(record)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"]["materializedRowCount"], 1)
        self.assertEqual(result["result"]["archiveStatus"], "failed")
        self.assertIn("archive unavailable", result["result"]["archiveError"])
        inserted_tables = [table for table, _rows in clickhouse.inserted]
        self.assertIn("chart_candles", inserted_tables)

    def test_backfill_s3_archive_batches_rows_into_chunk_objects(self) -> None:
        s3 = FakeS3()
        rows = [
            {
                "eventType": "CANDLE",
                "symbol": "AAPL",
                "interval": "1D",
                "timestamp": f"2026-06-{day:02d}T00:00:00.000Z",
                "open": 1,
                "high": 2,
                "low": 1,
                "close": 2,
                "volume": 100,
            }
            for day in range(1, 6)
        ]

        result = archive_processed_candles_to_s3(
            s3,
            "test-bucket",
            "market-data/dev/helixho/backfill/processed",
            rows,
            rows_per_object=2,
        )

        self.assertEqual(result["rowCount"], 5)
        self.assertEqual(result["objectCount"], 3)
        self.assertEqual(len(s3.objects), 3)
        self.assertTrue(all(
            item["Key"].startswith("market-data/dev/helixho/backfill/processed/candles/interval=1D/symbol=AAPL/archive_year=")
            for item in s3.objects
        ))

    def test_clickhouse_materializer_inserts_chart_rows_by_month_partition(self) -> None:
        clickhouse = FakeClickHouseClient()
        def month_timestamp(index: int) -> str:
            year = 2016 + index // 12
            month = index % 12 + 1
            return f"{year}-{month:02d}-01T00:00:00.000Z"

        rows = [
            {
                "eventType": "CANDLE",
                "symbol": "MSFT",
                "interval": "1D",
                "timestamp": timestamp,
                "open": 1,
                "high": 2,
                "low": 1,
                "close": 2,
                "volume": 100,
            }
            for timestamp in [month_timestamp(index) for index in range(125)]
        ]

        result = materialize_prepared_processed_rows(clickhouse, "alpaca://MSFT/1D/test", rows)

        self.assertEqual(result["rowCount"], 125)
        chart_inserts = [inserted_rows for table, inserted_rows in clickhouse.inserted if table == "chart_candles"]
        self.assertEqual(len(chart_inserts), 125)
        self.assertTrue(all(len(inserted_rows) == 1 for inserted_rows in chart_inserts))
        self.assertTrue(all(len({row["event_time"][:7] for row in inserted_rows}) == 1 for inserted_rows in chart_inserts))
        audit_rows = [inserted_rows for table, inserted_rows in clickhouse.inserted if table == "load_audit"]
        self.assertEqual(len(audit_rows), 1)
        self.assertEqual(audit_rows[0][0]["row_count"], 125)

    def test_runner_does_not_record_no_data_boundary_for_market_holiday(self) -> None:
        store = FakeStore()
        runner = BackfillRunner(
            store=store,
            clickhouse_client=FakeClickHouseClient(),
            coverage_provider=FakeCoverageProvider(),
        )
        record = {
            "requestId": "backfill:AAPL:1m:holiday",
            "symbol": "AAPL",
            "interval": "1m",
            "range": {
                "start": "2026-07-03T13:30:00.000Z",
                "end": "2026-07-03T20:00:00.000Z",
            },
            "jobType": "gapfill",
            "sourcePreference": "alpaca-only",
        }

        with mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", return_value=[]) as fetch:
            result = runner.run(record)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"]["source"], "calendar-empty")
        self.assertEqual(store.no_data, [])
        fetch.assert_not_called()

    def test_gap_detection_chunks_long_intraday_ranges(self) -> None:
        coverage = RecordingCoverageProvider()
        runner = BackfillRunner(
            store=FakeStore(),
            clickhouse_client=FakeClickHouseClient(),
            coverage_provider=coverage,
        )

        with mock.patch.dict(os.environ, {"BACKFILL_GAPFILL_MAX_DETECT_DAYS_INTRADAY": "7"}):
            ranges = runner.detect_missing_ranges(
                "AAPL",
                "1m",
                "2026-06-01T13:30:00.000Z",
                "2026-06-16T20:00:00.000Z",
                "gapfill",
            )

        self.assertGreater(len(coverage.timestamp_calls), 1)
        self.assertTrue(ranges)

    def test_daily_gap_detection_uses_daily_chunk_setting_not_intraday_limit(self) -> None:
        coverage = RecordingCoverageProvider()
        runner = BackfillRunner(
            store=FakeStore(),
            clickhouse_client=FakeClickHouseClient(),
            coverage_provider=coverage,
        )

        with mock.patch.dict(os.environ, {"BACKFILL_GAPFILL_MAX_DETECT_DAYS_INTRADAY": "7"}, clear=False):
            runner.detect_missing_ranges(
                "AAPL",
                "1D",
                "2025-01-01T00:00:00.000Z",
                "2025-12-31T00:00:00.000Z",
                "gapfill",
            )

        self.assertEqual(len(coverage.timestamp_calls), 1)

    def test_daily_alpaca_fetch_ranges_coalesce_missing_session_chunks(self) -> None:
        repair_ranges = [
            {"start": "2026-06-26T00:00:00.000Z", "end": "2026-06-27T00:00:00.000Z", "missingCount": 1},
            {"start": "2026-06-29T00:00:00.000Z", "end": "2026-06-30T00:00:00.000Z", "missingCount": 1},
            {"start": "2026-06-30T00:00:00.000Z", "end": "2026-07-01T00:00:00.000Z", "missingCount": 1},
        ]

        fetch_ranges = alpaca_fetch_ranges("1D", repair_ranges)

        self.assertEqual(fetch_ranges, [{
            "start": "2026-06-26T00:00:00.000Z",
            "end": "2026-07-01T00:00:00.000Z",
        }])

    def test_intraday_alpaca_fetch_ranges_coalesce_missing_session_chunks(self) -> None:
        repair_ranges = [
            {"start": "2026-06-26T13:30:00.000Z", "end": "2026-06-26T20:00:00.000Z", "missingCount": 390},
            {"start": "2026-06-29T13:30:00.000Z", "end": "2026-06-29T20:00:00.000Z", "missingCount": 390},
            {"start": "2026-06-30T13:30:00.000Z", "end": "2026-06-30T20:00:00.000Z", "missingCount": 390},
        ]

        fetch_ranges = alpaca_fetch_ranges("1m", repair_ranges)

        self.assertEqual(fetch_ranges, [{
            "start": "2026-06-26T13:30:00.000Z",
            "end": "2026-06-30T20:00:00.000Z",
        }])

    def test_intraday_alpaca_fetch_ranges_split_by_configured_page_window(self) -> None:
        repair_ranges = [
            {"start": "2026-06-26T13:30:00.000Z", "end": "2026-06-26T20:00:00.000Z", "missingCount": 390},
            {"start": "2026-06-29T13:30:00.000Z", "end": "2026-06-29T20:00:00.000Z", "missingCount": 390},
        ]

        with mock.patch.dict(os.environ, {"BACKFILL_INTRADAY_FETCH_MAX_DAYS": "2"}, clear=False):
            fetch_ranges = alpaca_fetch_ranges("1m", repair_ranges)

        self.assertEqual(fetch_ranges, [
            {
                "start": "2026-06-26T13:30:00.000Z",
                "end": "2026-06-26T20:00:00.000Z",
            },
            {
                "start": "2026-06-29T13:30:00.000Z",
                "end": "2026-06-29T20:00:00.000Z",
            },
        ])

    def test_configured_nyse_holiday_has_no_expected_buckets(self) -> None:
        buckets = expected_bucket_starts(
            "2026-07-03T13:30:00.000Z",
            "2026-07-03T20:00:00.000Z",
            "1m",
        )

        self.assertEqual(buckets, [])

    def test_historical_daily_candle_uses_regular_session(self) -> None:
        candle = raw_bar_to_processed_candle(
            "AAPL",
            {"t": "2026-06-29T00:00:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 100},
            interval="1D",
        )

        self.assertEqual(candle["marketSession"], "regular")

    def test_daily_gapfill_buckets_use_utc_midnight_not_new_york_midnight(self) -> None:
        calendar = TradingCalendar()

        buckets = expected_bucket_starts(
            "2026-03-09T00:00:00.000Z",
            "2026-03-10T00:00:00.000Z",
            "1D",
            calendar,
        )

        self.assertEqual([bucket.isoformat() for bucket in buckets], ["2026-03-09T00:00:00+00:00"])
        self.assertEqual(
            to_bucket_start("2026-03-09T04:00:00.000Z", "1D", calendar).isoformat(),
            "2026-03-09T00:00:00+00:00",
        )

    def test_historical_daily_candle_uses_canonical_utc_midnight_timestamp(self) -> None:
        candle = raw_bar_to_processed_candle(
            "AAPL",
            {"t": "2026-06-29T04:00:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 100},
            interval="1D",
        )

        self.assertEqual(candle["timestamp"], "2026-06-29T00:00:00.000Z")


if __name__ == "__main__":
    unittest.main()
