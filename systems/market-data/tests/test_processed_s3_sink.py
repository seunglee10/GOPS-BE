import json
import unittest
from collections import defaultdict

from alfaka.storage.processed_s3_sink import (
    flush_buffer,
    processed_realtime_partition_keys,
)
from alfaka.storage.s3_realtime_layout import canonical_rows_with_duplicate_count, symbol_shard


class ProcessedS3SinkV2Test(unittest.TestCase):
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
            _candle("AAPL", minute=31, source_event_id="aapl-31"),
            _candle("AAPL", minute=30, source_event_id="aapl-30"),
        ]
        partition = processed_realtime_partition_keys(_final_prefix(), rows[0], "v2")[0]
        s3 = _S3()
        first_metrics = {}
        first = flush_buffer(s3, "bucket", partition, [rows[0], rows[1], rows[0]], "jsonl", metrics=first_metrics)
        first_body = s3.objects[first]
        second = flush_buffer(s3, "bucket", partition, list(reversed(rows)), "jsonl")

        self.assertEqual(first, second)
        self.assertEqual(first_body, s3.objects[second])
        self.assertEqual(first_metrics["duplicateRows"], 1)
        reconstructed = [json.loads(line) for line in first_body.decode().splitlines()]
        self.assertEqual([row["sourceEventId"] for row in reconstructed], ["aapl-30", "aapl-31"])

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

    def put_object(self, Bucket, Key, Body, **_kwargs):
        del Bucket
        self.objects[Key] = Body
        return {"ETag": "fixture"}


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
