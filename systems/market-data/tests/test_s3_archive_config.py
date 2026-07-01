from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "systems/market-data/shared"))

from alfaka.storage.processed_s3_archive import archive_processed_candles_to_s3  # noqa: E402


class FakeS3:
    def __init__(self) -> None:
        self.objects = []

    def put_object(self, **kwargs):
        self.objects.append(kwargs)
        return {"ok": True}


class S3ArchiveConfigTests(unittest.TestCase):
    def test_post_clickhouse_archive_writes_processed_candles(self) -> None:
        s3 = FakeS3()

        result = archive_processed_candles_to_s3(
            s3,
            "bucket",
            "market-data/dev/helixho/final",
            [{
                "eventType": "CANDLE",
                "symbol": "AAPL",
                "interval": "1m",
                "timestamp": "2026-06-29T13:30:00.000Z",
                "isClosed": True,
            }],
            output_format="jsonl",
        )

        self.assertEqual(result["rowCount"], 1)
        self.assertEqual(len(s3.objects), 1)
        object_key = s3.objects[0]["Key"]
        self.assertTrue(object_key.startswith("market-data/dev/helixho/final/candles/interval=1m/symbol=AAPL/"))
        self.assertTrue(object_key.endswith(".jsonl"))

    def test_post_clickhouse_archive_skips_non_candle_rows(self) -> None:
        s3 = FakeS3()

        result = archive_processed_candles_to_s3(
            s3,
            "bucket",
            "market-data/dev/helixho/final",
            [
                {
                    "eventType": "TRADE",
                    "symbol": "AAPL",
                    "timestamp": "2026-06-29T13:30:00.000Z",
                    "price": 200,
                    "size": 10,
                },
                {
                    "eventType": "LIVE_CANDLE",
                    "symbol": "AAPL",
                    "interval": "1m",
                    "timestamp": "2026-06-29T13:30:00.000Z",
                    "isClosed": False,
                },
            ],
            output_format="jsonl",
        )

        self.assertEqual(result["rowCount"], 0)
        self.assertEqual(result["objectCount"], 0)
        self.assertEqual(result["skippedRowCount"], 2)
        self.assertEqual(s3.objects, [])


if __name__ == "__main__":
    unittest.main()
