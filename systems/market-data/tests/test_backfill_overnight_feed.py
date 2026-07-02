import io
import os
import unittest
from unittest import mock

from alfaka.backfill.runner import BackfillRunner


def alpaca_raw_bar(timestamp, open_price=10, index=0):
    close_price = round(open_price + 0.5, 4)
    return {
        "t": timestamp,
        "o": open_price,
        "h": round(close_price + 0.5, 4),
        "l": round(open_price - 0.5, 4),
        "c": close_price,
        "v": 1000 + index,
        "n": 10 + index,
        "vw": round((open_price + close_price) / 2, 4),
    }


class RecordingClickHouseClient:
    def __init__(self):
        self.inserts = []

    def insert_json_each_row(self, table, rows):
        self.inserts.append((table, list(rows)))


class S3ObjectStore:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})

    def get_paginator(self, name):
        if name != "list_objects_v2":
            raise ValueError(name)
        return self

    def paginate(self, Bucket, Prefix):
        return [{"Contents": [{"Key": key} for key in sorted(self.objects) if key.startswith(Prefix)]}]

    def get_object(self, Bucket, Key):
        payload = self.objects[Key]
        body = payload["Body"] if isinstance(payload, dict) else payload
        content_type = payload.get("ContentType", "application/x-ndjson") if isinstance(payload, dict) else "application/x-ndjson"
        if isinstance(body, str):
            body = body.encode("utf-8")
        return {"Body": io.BytesIO(body), "ContentType": content_type}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = {"Body": Body, "ContentType": ContentType}


class TimestampCoverageProvider:
    def __init__(self, timestamps=None):
        self.timestamps = list(timestamps or [])

    def candle_coverage(self, symbol, interval):
        return {"rowCount": len(self.timestamps), "availableFrom": None, "availableTo": None}

    def candle_timestamps(self, symbol, interval, from_time, to_time, limit=200000):
        return list(self.timestamps)


class BackfillOvernightFeedTests(unittest.TestCase):
    def test_backfill_runner_routes_overnight_gapfill_to_boats_feed(self):
        record = {
            "requestId": "backfill:AAPL:1m:overnight",
            "symbol": "AAPL",
            "interval": "1m",
            "range": {"start": "2026-07-02T00:00:00.000Z", "end": "2026-07-02T00:03:00.000Z"},
            "jobType": "gapfill",
            "sourcePreference": "coverage-first",
        }
        calls = []

        def fake_fetch(symbol, start, end, feed, timeframe):
            calls.append({
                "symbol": symbol,
                "start": start,
                "end": end,
                "feed": feed,
                "timeframe": timeframe,
            })
            return [alpaca_raw_bar(start, open_price=10)]

        client = RecordingClickHouseClient()
        runner = BackfillRunner(
            s3=S3ObjectStore(),
            clickhouse_client=client,
            coverage_provider=TimestampCoverageProvider(),
        )

        with mock.patch.dict(os.environ, {
            "S3_BUCKET": "bucket",
            "S3_FINAL_PREFIX": "market-data/final",
            "S3_PROCESSED_FORMAT": "jsonl",
            "HISTORICAL_FEED": "sip",
            "HISTORICAL_OVERNIGHT_FEED": "boats",
        }):
            with mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", side_effect=fake_fetch):
                result = runner._run(record)

        self.assertEqual(calls, [{
            "symbol": "AAPL",
            "start": "2026-07-02T00:00:00.000Z",
            "end": "2026-07-02T00:03:00.000Z",
            "feed": "boats",
            "timeframe": "1Min",
        }])
        self.assertEqual(result["source"], "alpaca")
        self.assertEqual(client.inserts[0][1][0]["feed_profile"], "boats")
        self.assertEqual(client.inserts[0][1][0]["market_session"], "overnight")
        self.assertEqual(client.inserts[0][1][0]["canonical_version"], "v2")

    def test_backfill_runner_splits_gapfill_at_overnight_daytime_boundary(self):
        record = {
            "requestId": "backfill:AAPL:1m:overnight-boundary",
            "symbol": "AAPL",
            "interval": "1m",
            "range": {"start": "2026-07-02T07:58:00.000Z", "end": "2026-07-02T08:02:00.000Z"},
            "jobType": "gapfill",
            "sourcePreference": "coverage-first",
        }
        calls = []

        def fake_fetch(symbol, start, end, feed, timeframe):
            calls.append({"start": start, "end": end, "feed": feed})
            return [alpaca_raw_bar(start, open_price=10)]

        runner = BackfillRunner(
            s3=S3ObjectStore(),
            clickhouse_client=RecordingClickHouseClient(),
            coverage_provider=TimestampCoverageProvider(),
        )

        with mock.patch.dict(os.environ, {
            "S3_BUCKET": "bucket",
            "S3_FINAL_PREFIX": "market-data/final",
            "S3_PROCESSED_FORMAT": "jsonl",
            "HISTORICAL_FEED": "sip",
            "HISTORICAL_OVERNIGHT_FEED": "boats",
        }):
            with mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", side_effect=fake_fetch):
                runner._run(record)

        self.assertEqual(calls, [
            {"start": "2026-07-02T07:58:00.000Z", "end": "2026-07-02T08:00:00.000Z", "feed": "boats"},
            {"start": "2026-07-02T08:00:00.000Z", "end": "2026-07-02T08:02:00.000Z", "feed": "sip"},
        ])


if __name__ == "__main__":
    unittest.main()
