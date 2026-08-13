import json
import types
import unittest
from collections import defaultdict

from market_data.storage.raw_s3_archive_sink import (
    flush_raw_buffer,
    raw_realtime_partition_keys,
    raw_s3_archive_runtime_config,
    run_raw_s3_archive_sink,
    start_raw_s3_archive_sink,
)


class RawS3ArchiveV2Test(unittest.TestCase):
    def test_default_topics_exclude_high_volume_trade_and_quote_streams(self):
        config = raw_s3_archive_runtime_config({
            "KAFKA_BOOTSTRAP_SERVERS": "kafka:29092",
            "KAFKA_RAW_S3_GROUP_ID": "raw-s3",
            "S3_BUCKET": "bucket",
        })

        self.assertEqual(config["topics"], [
            "market.input.realtime.events.v1",
            "market.input.realtime.bars.1m.v1",
            "market.input.realtime.updated-bars.1m.v1",
            "market.input.realtime.daily-bars.v1",
        ])

    def test_entrypoint_disables_auto_commit_and_wires_layout_mode(self):
        config = raw_s3_archive_runtime_config({
            "KAFKA_BOOTSTRAP_SERVERS": "kafka:29092",
            "KAFKA_RAW_S3_GROUP_ID": "raw-s3",
            "S3_BUCKET": "bucket",
            "S3_REALTIME_LAYOUT_MODE": "dual",
        })
        consumer = object()
        calls = {}

        def consumer_factory(*args, **kwargs):
            calls["consumer"] = (args, kwargs)
            return consumer

        def sink_runner(*args, **kwargs):
            calls["sink"] = (args, kwargs)

        start_raw_s3_archive_sink(
            config,
            consumer_factory=consumer_factory,
            s3_factory=lambda: "s3",
            sink_runner=sink_runner,
        )

        self.assertEqual(calls["consumer"][1], {"enable_auto_commit": False})
        self.assertEqual(calls["sink"][0], (consumer, "s3"))
        self.assertEqual(calls["sink"][1]["realtime_layout_mode"], "dual")
        self.assertFalse(calls["sink"][1]["enable_auto_commit"])

    def test_raw_auto_commit_does_not_inherit_processed_sink_setting(self):
        config = raw_s3_archive_runtime_config({
            "KAFKA_BOOTSTRAP_SERVERS": "kafka:29092",
            "KAFKA_RAW_S3_GROUP_ID": "raw-s3",
            "KAFKA_S3_ENABLE_AUTO_COMMIT": "true",
            "S3_BUCKET": "bucket",
        })

        self.assertFalse(config["enable_auto_commit"])

    def test_502_symbol_wave_creates_at_most_32_objects_and_no_manifests(self):
        groups = defaultdict(list)
        for index in range(502):
            row = _raw(f"S{index:04d}", f"raw-{index}")
            partition = raw_realtime_partition_keys(_raw_prefix(), row, "v2")[0]
            groups[partition].append(row)

        s3 = _S3()
        metrics = {}
        for partition, rows in groups.items():
            flush_raw_buffer(s3, "bucket", partition, rows, manifest_prefix="manifest", metrics=metrics)

        self.assertLessEqual(len(groups), 32)
        self.assertEqual(len(s3.objects), len(groups))
        self.assertFalse(any(key.startswith("manifest/") for key in s3.objects))
        self.assertEqual(metrics["rows"], 502)

    def test_raw_replay_uses_same_key_for_same_sorted_rows(self):
        rows = [_raw("AAPL", "a", minute=30), _raw("AAPL", "b", minute=30)]
        partition = raw_realtime_partition_keys(_raw_prefix(), rows[0], "v2")[0]
        s3 = _S3()
        first_metrics = {}
        first = flush_raw_buffer(s3, "bucket", partition, rows, metrics=first_metrics)
        body = s3.objects[first]
        second_metrics = {}
        second = flush_raw_buffer(s3, "bucket", partition, list(reversed(rows)), metrics=second_metrics)

        self.assertEqual(first, second)
        self.assertEqual(body, s3.objects[second])
        self.assertEqual(s3.put_calls, 1)
        self.assertEqual(second_metrics["exactReplaySkips"], 1)

    def test_v2_runtime_separates_adjacent_utc_minutes_and_commits_after_flush(self):
        consumer = _BatchThenInterruptConsumer([
            _envelope("AAPL", "a", minute=30),
            _envelope("AAPL", "b", minute=31),
        ])
        s3 = _S3()

        run_raw_s3_archive_sink(
            consumer,
            s3,
            s3_bucket="bucket",
            raw_prefix=_raw_prefix(),
            flush_count=100,
            realtime_layout_mode="v2",
        )

        self.assertEqual(len(s3.objects), 2)
        object_minutes = [
            {json.loads(line)["eventTime"][11:16] for line in body.decode().splitlines()}
            for body in s3.objects.values()
        ]
        self.assertCountEqual(object_minutes, [{"13:30"}, {"13:31"}])
        self.assertEqual(consumer.commits, 1)

    def test_put_failure_does_not_commit_offset(self):
        consumer = _BatchThenInterruptConsumer([_envelope("AAPL", "a", minute=30)])

        with self.assertRaises(RuntimeError):
            run_raw_s3_archive_sink(
                consumer,
                _FailingS3(),
                s3_bucket="bucket",
                raw_prefix=_raw_prefix(),
                flush_count=1,
                put_max_attempts=1,
                realtime_layout_mode="v2",
            )

        self.assertEqual(consumer.commits, 0)

    def test_simulation_rows_are_not_written_to_durable_raw_storage(self):
        row = _envelope("AMD", "sim-a", minute=30)
        row["simulation"] = {"source": "gops-simulator", "runId": "sim-test"}
        consumer = _BatchThenInterruptConsumer([row])
        s3 = _S3()
        metrics = {}

        run_raw_s3_archive_sink(
            consumer,
            s3,
            s3_bucket="bucket",
            raw_prefix=_raw_prefix(),
            flush_count=1,
            realtime_layout_mode="v2",
            metrics=metrics,
        )

        self.assertEqual(s3.objects, {})
        self.assertEqual(metrics["simulationRowsSkipped"], 1)
        self.assertGreaterEqual(consumer.commits, 1)


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


class _FailingS3:
    def head_object(self, Bucket, Key):
        del Bucket, Key
        raise KeyError()

    def put_object(self, **_kwargs):
        raise RuntimeError("put failed")


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


def _raw_prefix():
    return "market-data/rebuild-20260702-lazy-v1/raw/alpaca"


def _raw(symbol, source_event_id, minute=30):
    return {
        "source": "alpaca",
        "channel": "trades",
        "symbol": symbol,
        "eventTime": f"2026-07-08T13:{minute:02d}:00.000Z",
        "receivedAt": f"2026-07-08T13:{minute:02d}:00.100Z",
        "sourceEventId": source_event_id,
        "raw": {"S": symbol, "p": 100.5},
    }


def _envelope(symbol, source_event_id, minute=30):
    row = _raw(symbol, source_event_id, minute)
    return {
        "source": "alpaca",
        "feed": "sip",
        "channel": "trades",
        "symbol": symbol,
        "eventTime": row["eventTime"],
        "receivedAt": row["receivedAt"],
        "sourceEventId": source_event_id,
        "raw": row["raw"],
    }


if __name__ == "__main__":
    unittest.main()
