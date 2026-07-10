import unittest

from alfaka.storage.s3_manifest import bounded_v2_processed_candle_keys, bounded_v2_raw_keys
from alfaka.storage.s3_realtime_layout import symbol_shard


class S3V2DualReaderTest(unittest.TestCase):
    def test_processed_reader_lists_only_requested_hour_and_symbol_shard(self):
        shard = symbol_shard("AAPL")
        matching = (
            "market-data/rebuild-20260702-lazy-v1/final-v2/candles/interval=1m/"
            f"date=2026-07-08/hour=13/shard={shard}/part-window-digest.parquet"
        )
        s3 = _S3([matching, matching.replace(f"shard={shard}", "shard=00")])
        metrics = {}

        keys = bounded_v2_processed_candle_keys(
            s3,
            "bucket",
            "market-data/rebuild-20260702-lazy-v1/final",
            "AAPL",
            "1m",
            "2026-07-08T13:30:00.000Z",
            "2026-07-08T14:00:00.000Z",
            metrics=metrics,
        )

        self.assertEqual(keys, [matching])
        self.assertEqual(len(s3.prefixes), 1)
        self.assertEqual(metrics, {"listCalls": 1, "objectsListed": 1})

    def test_raw_reader_lists_channel_hour_and_symbol_shard(self):
        shard = symbol_shard("NVDA")
        matching = (
            "market-data/rebuild-20260702-lazy-v1/raw-v2/alpaca/channel=bars/"
            f"date=2026-07-08/hour=13/shard={shard}/part-window-digest.jsonl"
        )
        s3 = _S3([matching])
        metrics = {}

        keys = bounded_v2_raw_keys(
            s3,
            "bucket",
            "market-data/rebuild-20260702-lazy-v1/raw/alpaca",
            "NVDA",
            ["bars"],
            "2026-07-08T13:00:00.000Z",
            "2026-07-08T14:00:00.000Z",
            metrics=metrics,
        )

        self.assertEqual(keys, [matching])
        self.assertEqual(metrics, {"listCalls": 1, "objectsListed": 1})


class _S3:
    def __init__(self, keys):
        self.keys = keys
        self.prefixes = []

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        del Bucket, ContinuationToken
        self.prefixes.append(Prefix)
        return {
            "Contents": [{"Key": key} for key in self.keys if key.startswith(Prefix)],
            "IsTruncated": False,
        }


if __name__ == "__main__":
    unittest.main()
