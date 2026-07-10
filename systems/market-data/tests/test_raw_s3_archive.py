import unittest
from collections import defaultdict

from alfaka.storage.raw_s3_archive_sink import flush_raw_buffer, raw_realtime_partition_keys


class RawS3ArchiveV2Test(unittest.TestCase):
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
        rows = [_raw("AAPL", "a", minute=30), _raw("AAPL", "b", minute=31)]
        partition = raw_realtime_partition_keys(_raw_prefix(), rows[0], "v2")[0]
        s3 = _S3()
        first = flush_raw_buffer(s3, "bucket", partition, rows)
        body = s3.objects[first]
        second = flush_raw_buffer(s3, "bucket", partition, list(reversed(rows)))

        self.assertEqual(first, second)
        self.assertEqual(body, s3.objects[second])


class _S3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **_kwargs):
        del Bucket
        self.objects[Key] = Body
        return {"ETag": "fixture"}


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


if __name__ == "__main__":
    unittest.main()
