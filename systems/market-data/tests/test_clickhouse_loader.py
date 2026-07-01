from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "systems/market-data/shared"))

from alfaka.storage.clickhouse_loader import PostClickHouseCandleArchive, candle_to_clickhouse_row, canonicalize_candle_payload, load_payload, summarize_archive_rows  # noqa: E402
from alfaka.storage.clickhouse_materializer import prepare_processed_candle_rows  # noqa: E402


def daily_payload(timestamp: str, close: float = 2.0) -> dict:
    return {
        "eventType": "CANDLE",
        "symbol": "AAPL",
        "interval": "1D",
        "timestamp": timestamp,
        "open": 1,
        "high": 3,
        "low": 1,
        "close": close,
        "volume": 100,
        "isClosed": True,
        "source": "alpaca.dailyBars",
        "feed": "sip",
        "feedProfile": "sip",
        "sourceEventId": f"daily/AAPL/{timestamp}",
    }


class ClickHouseLoaderTests(unittest.TestCase):
    def test_trade_payload_is_realtime_only_and_not_inserted(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.inserts = []

            def insert_json_each_row(self, table, rows):
                self.inserts.append((table, rows))

        client = FakeClient()

        load_payload(client, {
            "eventType": "TRADE",
            "symbol": "AAPL",
            "price": 100.0,
            "size": 1,
            "timestamp": "2026-06-30T13:30:00.000Z",
        })

        self.assertEqual(client.inserts, [])

    def test_daily_candle_payload_is_canonicalized_before_insert(self) -> None:
        payload = daily_payload("2026-06-30T04:00:00.000Z")

        normalized = canonicalize_candle_payload(payload)
        row = candle_to_clickhouse_row(payload)

        self.assertEqual(normalized["timestamp"], "2026-06-30T00:00:00.000Z")
        self.assertEqual(row["event_time"], "2026-06-30 00:00:00.000")
        self.assertEqual(row["market_session"], "regular")

    def test_materializer_dedupes_daily_rows_by_canonical_timestamp(self) -> None:
        prepared = prepare_processed_candle_rows([
            daily_payload("2026-06-30T00:00:00.000Z", close=2.0),
            daily_payload("2026-06-30T04:00:00.000Z", close=3.0),
        ])

        self.assertEqual(len(prepared["rows"]), 1)
        self.assertEqual(prepared["rows"][0]["timestamp"], "2026-06-30T00:00:00.000Z")
        self.assertEqual(prepared["rows"][0]["marketSession"], "regular")
        self.assertEqual(prepared["rows"][0]["close"], 3.0)

    def test_s3_archive_runs_only_after_clickhouse_candle_insert_succeeds(self) -> None:
        events = []

        class FakeClient:
            def insert_json_each_row(self, table, rows):
                events.append(("insert", table, rows[0]["event_time"]))

        class FakeArchive:
            def archive_candle_payload(self, payload):
                events.append(("archive", payload["symbol"], payload["timestamp"]))

        load_payload(
            FakeClient(),
            daily_payload("2026-06-30T04:00:00.000Z"),
            archive=FakeArchive(),
        )

        self.assertEqual(events, [
            ("insert", "chart_candles", "2026-06-30 00:00:00.000"),
            ("archive", "AAPL", "2026-06-30T00:00:00.000Z"),
        ])

    def test_s3_archive_is_not_called_when_clickhouse_insert_fails(self) -> None:
        class FailingClient:
            def insert_json_each_row(self, table, rows):
                raise RuntimeError("clickhouse unavailable")

        class FakeArchive:
            def __init__(self) -> None:
                self.calls = []

            def archive_candle_payload(self, payload):
                self.calls.append(payload)

        archive = FakeArchive()

        with self.assertRaises(RuntimeError):
            load_payload(FailingClient(), daily_payload("2026-06-30T04:00:00.000Z"), archive=archive)

        self.assertEqual(archive.calls, [])

    def test_s3_archive_failure_does_not_fail_clickhouse_insert(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.inserts = []

            def insert_json_each_row(self, table, rows):
                self.inserts.append((table, rows))

        class FailingArchive:
            def archive_candle_payload(self, payload):
                raise RuntimeError("archive unavailable")

        client = FakeClient()

        load_payload(client, daily_payload("2026-06-30T04:00:00.000Z"), archive=FailingArchive())

        self.assertEqual(len(client.inserts), 1)

    def test_post_clickhouse_archive_uses_final_prefix_config(self) -> None:
        archive = PostClickHouseCandleArchive.from_env({
            "S3_BUCKET": "test-bucket",
            "S3_ARCHIVE_ROOT_PREFIX": "market-data/dev/helixho",
            "S3_FINAL_PREFIX": "market-data/dev/helixho/final",
            "S3_MANIFEST_PREFIX": "market-data/dev/helixho/manifest",
            "S3_PROCESSED_FORMAT": "jsonl",
            "S3_CLICKHOUSE_ARCHIVE_FLUSH_ROWS": "25",
            "S3_CLICKHOUSE_ARCHIVE_FLUSH_SECONDS": "30",
        })

        self.assertIsNotNone(archive)
        self.assertEqual(archive.bucket, "test-bucket")
        self.assertEqual(archive.prefix, "market-data/dev/helixho/final")
        self.assertEqual(archive.manifest_prefix, "market-data/dev/helixho/manifest")
        self.assertEqual(archive.output_format, "jsonl")
        self.assertEqual(archive.flush_rows, 25)
        self.assertEqual(archive.flush_interval_seconds, 30)

    def test_post_clickhouse_archive_can_be_disabled_by_env(self) -> None:
        archive = PostClickHouseCandleArchive.from_env({
            "S3_BUCKET": "test-bucket",
            "CLICKHOUSE_LOADER_S3_ARCHIVE_ENABLED": "false",
        })

        self.assertIsNone(archive)

    def test_post_clickhouse_archive_batches_inserted_candles(self) -> None:
        class FakeS3:
            def __init__(self) -> None:
                self.objects = []

            def put_object(self, **kwargs):
                self.objects.append(kwargs)

        s3 = FakeS3()
        archive = PostClickHouseCandleArchive(
            s3=s3,
            bucket="test-bucket",
            prefix="market-data/dev/helixho/final",
            output_format="jsonl",
            manifest_prefix=None,
            manifest_layout="compact",
            max_attempts=1,
            retry_sleep_seconds=0,
            rows_per_object=1000,
            flush_rows=2,
            flush_interval_seconds=60,
        )

        self.assertEqual(archive.archive_candle_payload(daily_payload("2026-06-30T00:00:00.000Z"))["archiveStatus"], "buffered")
        self.assertEqual(s3.objects, [])

        archive.archive_candle_payload(daily_payload("2026-07-01T00:00:00.000Z"))

        self.assertEqual(len(s3.objects), 1)
        self.assertTrue(s3.objects[0]["Key"].startswith("market-data/dev/helixho/final/candles/interval=1D/symbol=AAPL/"))
        self.assertEqual(s3.objects[0]["Body"].count(b"\n"), 2)

    def test_archive_log_summary_is_bounded(self) -> None:
        rows = [{"symbol": f"T{i}", "interval": "1m"} for i in range(10)]

        summary = summarize_archive_rows(rows, max_groups=3)

        self.assertEqual(summary, "T0:1m:1,T1:1m:1,T2:1m:1,+7 groups")


if __name__ == "__main__":
    unittest.main()
