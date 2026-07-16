import json
import types
import unittest
from collections import defaultdict

from alfaka.storage.processed_s3_sink import (
    flush_buffer,
    processed_realtime_partition_keys,
    processed_s3_runtime_config,
    run_processed_s3_sink,
    start_processed_s3_sink,
)
from alfaka.storage.s3_realtime_layout import canonical_rows_with_duplicate_count, symbol_shard


class ProcessedS3SinkV2Test(unittest.TestCase):
    def test_entrypoint_wires_consumer_and_layout_mode_to_sink(self):
        config = processed_s3_runtime_config({
            "KAFKA_BOOTSTRAP_SERVERS": "kafka:29092",
            "KAFKA_S3_GROUP_ID": "processed-s3",
            "KAFKA_S3_ENABLE_AUTO_COMMIT": "false",
            "S3_BUCKET": "bucket",
            "S3_PROCESSED_FORMAT": "jsonl",
            "S3_REALTIME_LAYOUT_MODE": "dual",
        })
        consumer = object()
        calls = {}

        def consumer_factory(*args, **kwargs):
            calls["consumer"] = (args, kwargs)
            return consumer

        def sink_runner(*args, **kwargs):
            calls["sink"] = (args, kwargs)

        start_processed_s3_sink(
            config,
            consumer_factory=consumer_factory,
            s3_factory=lambda: "s3",
            sink_runner=sink_runner,
        )

        self.assertEqual(calls["consumer"][1], {"enable_auto_commit": False})
        self.assertEqual(calls["sink"][0][:2], (consumer, "s3"))
        self.assertEqual(calls["sink"][1]["realtime_layout_mode"], "dual")
        self.assertFalse(calls["sink"][1]["enable_auto_commit"])

    def test_crc32_shard_fixtures_are_stable(self):
        self.assertEqual({symbol: symbol_shard(symbol) for symbol in ["AAPL", "NVDA", "MSFT", "_MARKET", "BTCUSD"]}, {
            "AAPL": "28",
            "NVDA": "23",
            "MSFT": "27",
            "_MARKET": "14",
            "BTCUSD": "25",
        })

    def test_502_symbol_wave_creates_at_most_32_objects_and_no_manifests(self):
        groups = defaultdict(list)
        for index in range(502):
            row = _candle(f"S{index:04d}", source_event_id=f"event-{index}")
            keys = processed_realtime_partition_keys(_final_prefix(), row, "v2")
            self.assertEqual(len(keys), 1)
            groups[keys[0]].append(row)

        s3 = _S3()
        metrics = {}
        for partition, rows in groups.items():
            flush_buffer(s3, "bucket", partition, rows, "jsonl", manifest_prefix="manifest", metrics=metrics)

        self.assertLessEqual(len(groups), 32)
        self.assertEqual(len(s3.objects), len(groups))
        self.assertFalse(any(key.startswith("manifest/") for key in s3.objects))
        self.assertEqual(metrics["objects"], len(groups))
        self.assertEqual(metrics["rows"], 502)

    def test_identical_replay_is_deterministic_and_duplicate_rows_are_removed(self):
        rows = [
            _candle("AAPL", minute=30, source_event_id="aapl-31"),
            _candle("AAPL", minute=30, source_event_id="aapl-30"),
        ]
        partition = processed_realtime_partition_keys(_final_prefix(), rows[0], "v2")[0]
        s3 = _S3()
        first_metrics = {}
        first = flush_buffer(s3, "bucket", partition, [rows[0], rows[1], rows[0]], "jsonl", metrics=first_metrics)
        first_body = s3.objects[first]
        second_metrics = {}
        second = flush_buffer(s3, "bucket", partition, list(reversed(rows)), "jsonl", metrics=second_metrics)

        self.assertEqual(first, second)
        self.assertEqual(first_body, s3.objects[second])
        self.assertEqual(s3.put_calls, 1)
        self.assertEqual(first_metrics["duplicateRows"], 1)
        self.assertEqual(second_metrics["exactReplaySkips"], 1)
        reconstructed = [json.loads(line) for line in first_body.decode().splitlines()]
        self.assertEqual([row["sourceEventId"] for row in reconstructed], ["aapl-30", "aapl-31"])

    def test_v2_runtime_separates_adjacent_utc_minutes(self):
        consumer = _BatchThenInterruptConsumer([
            _candle("AAPL", minute=30, source_event_id="minute-30"),
            _candle("AAPL", minute=31, source_event_id="minute-31"),
        ])
        s3 = _S3()

        run_processed_s3_sink(
            consumer,
            s3,
            "bucket",
            _final_prefix(),
            "jsonl",
            flush_count=100,
            realtime_layout_mode="v2",
        )

        self.assertEqual(len(s3.objects), 2)
        object_minutes = [
            {json.loads(line)["timestamp"][11:16] for line in body.decode().splitlines()}
            for body in s3.objects.values()
        ]
        self.assertCountEqual(object_minutes, [{"13:30"}, {"13:31"}])
        self.assertEqual(consumer.commits, 1)

    def test_missing_processed_timestamp_fails_without_commit(self):
        row = _candle("AAPL")
        row.pop("timestamp")
        consumer = _BatchThenInterruptConsumer([row])

        with self.assertRaises(ValueError):
            run_processed_s3_sink(
                consumer,
                _S3(),
                "bucket",
                _final_prefix(),
                "jsonl",
                flush_count=1,
                realtime_layout_mode="v2",
            )

        self.assertEqual(consumer.commits, 0)

    def test_simulation_rows_are_not_written_to_durable_processed_storage(self):
        row = _candle("AMD")
        row["simulation"] = {"source": "gops-simulator", "runId": "sim-test"}
        consumer = _BatchThenInterruptConsumer([row])
        s3 = _S3()
        metrics = {}

        run_processed_s3_sink(
            consumer,
            s3,
            "bucket",
            _final_prefix(),
            "jsonl",
            flush_count=1,
            realtime_layout_mode="v2",
            metrics=metrics,
        )

        self.assertEqual(s3.objects, {})
        self.assertEqual(metrics["simulationRowsSkipped"], 1)
        self.assertGreaterEqual(consumer.commits, 1)

    def test_v1_v2_and_mixed_rows_reconstruct_identically(self):
        rows = [_candle("AAPL", minute=30, source_event_id="one"), _candle("AAPL", minute=31, source_event_id="two")]
        v1, _ = canonical_rows_with_duplicate_count(rows)
        v2, _ = canonical_rows_with_duplicate_count(list(reversed(rows)))
        mixed, duplicates = canonical_rows_with_duplicate_count([*v1, *v2])

        self.assertEqual(v1, v2)
        self.assertEqual(mixed, v1)
        self.assertEqual(duplicates, 2)


class _S3:
    def __init__(self):
        self.objects = {}
        self.put_calls = 0

    def put_object(self, Bucket, Key, Body, **_kwargs):
        del Bucket
        self.put_calls += 1
        self.objects[Key] = Body
        return {"ETag": "fixture"}

    def head_object(self, Bucket, Key):
        del Bucket
        if Key not in self.objects:
            raise KeyError(Key)
        return {"ETag": "fixture"}


class _BatchThenInterruptConsumer:
    def __init__(self, rows):
        self.rows = rows
        self.polls = 0
        self.commits = 0

    def poll(self, timeout_ms=1000):
        del timeout_ms
        self.polls += 1
        if self.polls > 1:
            raise KeyboardInterrupt()
        return {None: [types.SimpleNamespace(value=row) for row in self.rows]}

    def commit(self):
        self.commits += 1


def _final_prefix():
    return "market-data/rebuild-20260702-lazy-v1/final"


def _candle(symbol, minute=30, source_event_id="event"):
    return {
        "eventType": "CANDLE",
        "symbol": symbol,
        "interval": "1m",
        "timestamp": f"2026-07-08T13:{minute:02d}:00.000Z",
        "open": 100,
        "high": 101,
        "low": 99,
        "close": 100.5,
        "volume": 10,
        "isClosed": True,
        "sourceEventId": source_event_id,
        "priceAdjustment": "split",
        "canonicalVersion": "v2",
    }


if __name__ == "__main__":
    unittest.main()
