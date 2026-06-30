import json
import io
import os
import sys
import types
import importlib.util
import unittest
from unittest import mock
from pathlib import Path
from datetime import datetime, timedelta, time, timezone

sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda *args, **kwargs: None))
sys.modules.setdefault("botocore", types.SimpleNamespace())
sys.modules.setdefault("botocore.config", types.SimpleNamespace(Config=lambda **kwargs: kwargs))
sys.modules.setdefault("redis", types.SimpleNamespace(from_url=lambda *args, **kwargs: None))

REPO_ROOT = Path(__file__).resolve().parents[3]

from alfaka.alpaca.subscription import (
    configured_collection_symbols,
    configured_seed_symbols,
    configured_universe_symbols,
    load_request_config,
    load_symbols_and_channels,
    resolve_request_config_path,
)
from alfaka.alpaca.feed_profiles import market_session_for_timestamp, resolve_feed_profile
from alfaka.alpaca.trade_tiers import resolve_trade_subscription_plan
from alfaka.alpaca.websocket_collector import read_trade_subscription_symbols
from alfaka.alpaca.assets import asset_to_symbol_metadata
from alfaka.alpaca.news import build_news_events
from alfaka.common.kafka_io import create_json_consumer
from alfaka.common.market_messages import build_raw_envelope, raw_topic_name, source_event_id
from alfaka.common.redis_keys import RedisKeyBuilder
from alfaka.common.runtime_health import read_component_health, write_component_health
from alfaka.common.runtime_config import has_placeholder_value, validate_required_values
from alfaka.common.secrets import load_alpaca_credentials, resolve_alpaca_credential_source
from alfaka.backfill.runner import BackfillRunner, BackfillUnavailable, fetch_alpaca_bars, raw_bar_to_processed_candle, raw_bars_to_processed_candles
from alfaka.backfill.gapfill import TradingCalendar, detect_gapfill_ranges
from alfaka.backfill.status import RedisBackfillStore, default_backfill_range
from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider, clickhouse_param_value
from alfaka.serving.cursors import timestamp_from_cursor
from alfaka.serving.dto import cursor_for, market_status_event, snapshot, websocket_event
from alfaka.serving.hot_symbols import build_hot_symbols_payload, dollar_volume_from_candle
from alfaka.serving.intervals import candle_count_for_1y, candle_count_for_24h, historical_target_bars, redis_closed_candle_cap, resolve_candle_limit
from alfaka.serving.provider import MarketDataProvider, has_more_before_target
from alfaka.serving.redis_provider import RedisMarketDataProvider
from alfaka.serving.symbol_registry import SymbolRegistry
from alfaka.storage.clickhouse_loader import candle_to_clickhouse_row, load_payload, news_to_clickhouse_row, status_to_clickhouse_row, symbol_to_clickhouse_row, trade_to_clickhouse_row
from alfaka.storage.s3_materializer import (
    detect_s3_object_format,
    list_s3_objects,
    materialize_keys_from_env,
    materialize_processed_rows,
    materialize_s3_processed_objects,
    normalize_processed_candle_row,
    read_s3_rows,
)
from alfaka.storage.processed_s3_sink import flush_buffer, flush_due_buffers, normalize_storage_row, s3_partition_key
from alfaka.storage.raw_s3_archive_sink import flush_raw_buffer, raw_archive_row, raw_envelope_partition_key, raw_s3_archive_runtime_config, run_raw_s3_archive_sink
from alfaka.storage.raw_s3_archive import raw_chunk_partition_key, raw_object_suffix, raw_partition_key, upload_raw_page_to_s3
from alfaka.storage.s3_manifest import processed_candle_keys_from_manifest, raw_keys_from_manifest
from alfaka.streaming.processor import ProcessorState, process_raw_envelope, processor_runtime_config, recover_processor_state_from_clickhouse, recover_processor_state_from_redis, write_closed_candle_to_redis
from alfaka.streaming.transforms import (
    CandleAggregator,
    VolumeProfileBinBuilder,
    normalize_bar,
    normalize_status,
    normalize_trade,
)
from alfaka.tools import live_path_trace


class FakeRedisProvider:
    def __init__(self, candles=None, symbol_metadata=None, profile_bins=None, live_candle=None):
        self._candles = candles or []
        self._symbol_metadata = symbol_metadata or {}
        self._profile_bins = profile_bins or []
        self._live_candle = live_candle

    def recent_candles(self, symbol, interval, limit):
        return list(self._candles)[-limit:]

    def live_candle(self, symbol, interval="1m"):
        return self._live_candle

    def symbol_metadata(self, symbol):
        return self._symbol_metadata.get(symbol)

    def volume_profile_bins(self, symbol):
        return list(self._profile_bins)


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


class FakeClickHouseProvider:
    def __init__(self, candles=None, symbols=None):
        self._candles = candles or []
        self._symbols = symbols or {}

    def candles(self, symbol, interval, limit, before=None, from_time=None, to_time=None):
        return list(self._candles)[-limit:]

    def candles_since(self, symbol, interval, timestamp, limit=500):
        return list(self._candles)[:limit]

    def candle_coverage(self, symbol, interval):
        if not self._candles:
            return {"rowCount": 0, "availableFrom": None, "availableTo": None}
        return {
            "rowCount": len(self._candles),
            "availableFrom": self._candles[0].get("timestamp"),
            "availableTo": self._candles[-1].get("timestamp"),
        }

    def search_symbols(self, query, limit):
        return []

    def symbol(self, symbol):
        return self._symbols.get(symbol)


class RecordingRangeClickHouseProvider(FakeClickHouseProvider):
    def __init__(self, candles=None, symbols=None):
        super().__init__(candles=candles, symbols=symbols)
        self.calls = []

    def candles(self, symbol, interval, limit, before=None, from_time=None, to_time=None):
        self.calls.append({
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
            "before": before,
            "from_time": from_time,
            "to_time": to_time,
        })
        candles = list(self._candles)
        if from_time:
            candles = [candle for candle in candles if candle.get("timestamp", "") >= from_time]
        if before:
            candles = [candle for candle in candles if candle.get("timestamp", "") < before]
        if to_time:
            candles = [candle for candle in candles if candle.get("timestamp", "") <= to_time]
        return candles[-limit:]


class FailingClickHouseProvider(FakeClickHouseProvider):
    def candles_since(self, symbol, interval, timestamp, limit=500, include_from=False):
        raise RuntimeError("clickhouse down")

    def volume_profile_bins(self, symbol, from_time, to_time, price_bin_size=None):
        raise RuntimeError("clickhouse down")

    def candle_coverage(self, symbol, interval):
        raise RuntimeError("clickhouse down")


class RecordingClickHouseProviderForAggregation(ClickHouseMarketDataProvider):
    def __init__(self, rows):
        super().__init__(url="http://clickhouse.local:8123", database="market_data", user="u", password="p")
        self.rows = rows
        self.queries = []

    def query_json_each_row(self, query, params=None):
        self.queries.append((query, params or {}))
        return list(self.rows)


class FakeClickHouseRecoveryProvider:
    def __init__(self):
        self.calls = []

    def candles(self, symbol, interval, limit):
        self.calls.append((symbol, interval, limit))
        if symbol != "AAPL":
            return []
        if interval == "1m":
            return [{
                "timestamp": "2026-06-25T10:15:00.000Z",
                "open": 190,
                "high": 191,
                "low": 189,
                "close": 190.5,
                "volume": 100,
                "isClosed": True,
                "source": "clickhouse.fixture",
                "feed": "sip",
                "sourceEventId": "clickhouse-1m",
            }]
        if interval == "1D":
            return [{
                "timestamp": "2026-06-24T00:00:00.000Z",
                "open": 180,
                "high": 186,
                "low": 179,
                "close": 185,
                "volume": 1000,
                "isClosed": True,
                "source": "clickhouse.fixture",
                "feed": "sip",
                "sourceEventId": "clickhouse-1d",
            }]
        return []


class RecordingClickHouseClient:
    def __init__(self):
        self.inserts = []

    def insert_json_each_row(self, table, rows):
        self.inserts.append((table, list(rows)))


class FailingAuditClickHouseClient(RecordingClickHouseClient):
    def __init__(self):
        super().__init__()
        self.fail_next_audit = True

    def insert_json_each_row(self, table, rows):
        if table == "load_audit" and self.fail_next_audit:
            self.fail_next_audit = False
            raise RuntimeError("load audit unavailable")
        super().insert_json_each_row(table, rows)


def load_initial_load_job_module():
    module_path = REPO_ROOT / "systems/market-data/jobs/initial-load/main.py"
    spec = importlib.util.spec_from_file_location("initial_load_job", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.hashes = {}
        self.queues = {}
        self.streams = {}
        self.stream_seq = {}
        self.stream_groups = {}
        self.zsets = {}
        self.sets = {}
        self.published = []

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def exists(self, key):
        return key in self.values or key in self.hashes or key in self.zsets or key in self.sets

    def hset(self, key, mapping):
        self.hashes[key] = dict(mapping)
        return len(mapping)

    def lpush(self, key, value):
        self.queues.setdefault(key, []).insert(0, value)
        return len(self.queues[key])

    def brpop(self, key, timeout=0):
        values = self.queues.get(key) or []
        if not values:
            return None
        return key, values.pop()

    def llen(self, key):
        return len(self.queues.get(key, []))

    def xgroup_create(self, key, group, id="0", mkstream=False):
        group_key = (key, group)
        if group_key in self.stream_groups:
            raise Exception("BUSYGROUP Consumer Group name already exists")
        if mkstream:
            self.streams.setdefault(key, [])
        self.stream_groups[group_key] = {"last_id": id, "pending": {}}
        return True

    def xadd(self, key, fields, maxlen=None, approximate=True):
        seq = self.stream_seq.get(key, 0) + 1
        self.stream_seq[key] = seq
        stream_id = f"{seq}-0"
        self.streams.setdefault(key, []).append((stream_id, dict(fields)))
        if maxlen and len(self.streams[key]) > maxlen:
            self.streams[key] = self.streams[key][-maxlen:]
        return stream_id

    def xreadgroup(self, group, consumer, streams, count=1, block=0):
        key, requested_id = next(iter(streams.items()))
        group_state = self.stream_groups.setdefault((key, group), {"last_id": "0", "pending": {}})
        if requested_id != ">":
            return []
        messages = []
        for stream_id, fields in self.streams.get(key, []):
            if not self._stream_id_gt(stream_id, group_state["last_id"]):
                continue
            group_state["last_id"] = stream_id
            pending = group_state["pending"].setdefault(
                stream_id,
                {"fields": dict(fields), "consumer": consumer, "idle": 0, "deliveries": 0},
            )
            pending["consumer"] = consumer
            pending["deliveries"] += 1
            response_fields = {**pending["fields"], "deliveryCount": pending["deliveries"]}
            messages.append((stream_id, response_fields))
            if len(messages) >= count:
                break
        return [(key, messages)] if messages else []

    def xpending_range(self, key, group, min="-", max="+", count=10):
        group_state = self.stream_groups.get((key, group), {"pending": {}})
        entries = []
        for stream_id, pending in list(group_state.get("pending", {}).items())[:count]:
            entries.append({
                "message_id": stream_id,
                "consumer": pending["consumer"],
                "time_since_delivered": pending["idle"],
                "times_delivered": pending["deliveries"],
                "requestId": pending["fields"].get("requestId"),
            })
        return entries

    def xinfo_groups(self, key):
        groups = []
        for (stream_key, group_name), group_state in self.stream_groups.items():
            if stream_key != key:
                continue
            consumers = {entry["consumer"] for entry in group_state.get("pending", {}).values()}
            lag = sum(1 for stream_id, _fields in self.streams.get(key, []) if self._stream_id_gt(stream_id, group_state["last_id"]))
            groups.append({
                "name": group_name,
                "consumers": len(consumers),
                "pending": len(group_state.get("pending", {})),
                "last-delivered-id": group_state["last_id"],
                "lag": lag,
            })
        return groups

    def xclaim(self, key, group, consumer, min_idle_time, message_ids):
        group_state = self.stream_groups.get((key, group), {"pending": {}})
        messages = []
        for stream_id in message_ids:
            pending = group_state.get("pending", {}).get(stream_id)
            if not pending or pending["idle"] < min_idle_time:
                continue
            pending["consumer"] = consumer
            pending["deliveries"] += 1
            response_fields = {**pending["fields"], "deliveryCount": pending["deliveries"]}
            messages.append((stream_id, response_fields))
        return messages

    def xack(self, key, group, *ids):
        group_state = self.stream_groups.get((key, group), {"pending": {}})
        removed = 0
        for stream_id in ids:
            if stream_id in group_state.get("pending", {}):
                group_state["pending"].pop(stream_id, None)
                removed += 1
        return removed

    def xlen(self, key):
        return len(self.streams.get(key, []))

    def xrange(self, key, min="-", max="+", count=None):
        rows = [(stream_id, dict(fields)) for stream_id, fields in self.streams.get(key, []) if stream_id == min or min == "-"]
        return rows[:count] if count else rows

    @staticmethod
    def _stream_id_gt(left, right):
        def parse(value):
            parts = str(value).split("-", 1)
            if len(parts) == 1:
                return int(parts[0]), 0
            return int(parts[0]), int(parts[1])

        left_major, left_minor = parse(left)
        right_major, right_minor = parse(right)
        return (left_major, left_minor) > (right_major, right_minor)

    def zremrangebyscore(self, key, start, end):
        values = self.zsets.get(key, {})
        removed = [member for member, score in values.items() if start <= score <= end]
        for member in removed:
            values.pop(member, None)
        return len(removed)

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    def zrange(self, key, start, end):
        ordered = [member for member, _score in sorted(self.zsets.get(key, {}).items(), key=lambda item: (item[1], item[0]))]
        length = len(ordered)
        if start < 0:
            start = length + start
        if end < 0:
            end = length + end
        start = max(0, start)
        end = min(length - 1, end)
        if start > end or length == 0:
            return []
        return ordered[start:end + 1]

    def zremrangebyrank(self, key, start, end):
        values = self.zsets.get(key, {})
        ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
        length = len(ordered)
        if start < 0:
            start = length + start
        if end < 0:
            end = length + end
        start = max(0, start)
        end = min(length - 1, end)
        if start > end or length == 0:
            return 0
        removed = ordered[start:end + 1]
        for member, _score in removed:
            values.pop(member, None)
        return len(removed)

    def sadd(self, key, *values):
        self.sets.setdefault(key, set()).update(values)
        return len(values)

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def expire(self, key, seconds):
        return True

    def publish(self, channel, value):
        self.published.append((channel, value))
        return 1


class FakeS3WithPaginator:
    def get_paginator(self, name):
        if name != "list_objects_v2":
            raise ValueError(name)
        return self

    def paginate(self, Bucket, Prefix):
        return [
            {"Contents": [{"Key": f"{Prefix}/part-1.jsonl"}]},
            {"Contents": [{"Key": f"{Prefix}/part-2.parquet"}]},
        ]


class RecordingS3:
    def __init__(self):
        self.objects = []

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects.append({"Bucket": Bucket, "Key": Key, "Body": Body, "ContentType": ContentType})


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


class FlakyS3ObjectStore(S3ObjectStore):
    def __init__(self, failures_before_success=1):
        super().__init__()
        self.failures_before_success = failures_before_success
        self.put_attempts = 0

    def put_object(self, Bucket, Key, Body, ContentType):
        self.put_attempts += 1
        if self.put_attempts <= self.failures_before_success:
            raise RuntimeError("temporary s3 outage")
        return super().put_object(Bucket, Key, Body, ContentType)


class FailOnPutNumbersS3ObjectStore(S3ObjectStore):
    def __init__(self, fail_on):
        super().__init__()
        self.fail_on = set(fail_on)
        self.put_attempts = 0

    def put_object(self, Bucket, Key, Body, ContentType):
        self.put_attempts += 1
        if self.put_attempts in self.fail_on:
            raise RuntimeError("temporary s3 outage")
        return super().put_object(Bucket, Key, Body, ContentType)


class FakeHttpResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._payload


class OneMessageThenInterruptConsumer:
    def __init__(self, value):
        self.value = value
        self.poll_count = 0

    def poll(self, timeout_ms=1000):
        self.poll_count += 1
        if self.poll_count == 1:
            return {"topic-partition": [types.SimpleNamespace(value=self.value)]}
        raise KeyboardInterrupt


class StaticCoverageProvider:
    def __init__(self, coverage):
        self.coverage = coverage

    def candle_coverage(self, symbol, interval):
        return dict(self.coverage)


class TimestampCoverageProvider(StaticCoverageProvider):
    def __init__(self, coverage, timestamps):
        super().__init__(coverage)
        self.timestamps = timestamps

    def candle_timestamps(self, symbol, interval, from_time, to_time, limit=200000):
        return list(self.timestamps)


class RecordingProducer:
    def __init__(self):
        self.sent = []

    def send(self, topic, key, value):
        self.sent.append({"topic": topic, "key": key, "value": value})


class RecordingKafkaConsumer:
    calls = []

    def __init__(self, *topics, **kwargs):
        self.topics = topics
        self.kwargs = kwargs
        RecordingKafkaConsumer.calls.append({"topics": topics, "kwargs": kwargs})


class QueueMetricsUnavailableStore:
    def queue_metrics(self):
        raise RuntimeError("redis unavailable")


class MarketDataHardeningContractTest(unittest.TestCase):
    def setUp(self):
        self._saved_market_env = {
            key: os.environ.get(key)
            for key in (
                "ALFAKA_REQUEST_CONFIG",
                "ALPACA_UNIVERSE",
                "ALPACA_UNIVERSE_REGISTRY_PATH",
                "ALPACA_COLLECTION_SYMBOL_SOURCE",
            )
        }
        os.environ["ALFAKA_REQUEST_CONFIG"] = "systems/market-data/config/market-data-request.json"
        os.environ["ALPACA_UNIVERSE"] = "sp500"
        os.environ["ALPACA_UNIVERSE_REGISTRY_PATH"] = "systems/market-data/config/sp500-universe.json"
        os.environ["ALPACA_COLLECTION_SYMBOL_SOURCE"] = "universe"

    def tearDown(self):
        for key, value in self._saved_market_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_raw_envelope_has_source_event_id_and_topic(self):
        message = {"T": "t", "S": "AAPL", "i": 123, "p": 195.2, "s": 10, "t": "2026-06-25T10:15:20.100Z"}
        envelope = build_raw_envelope(message, "sip")

        self.assertEqual(envelope["channel"], "trades")
        self.assertEqual(envelope["symbol"], "AAPL")
        self.assertEqual(raw_topic_name("market.raw", "t"), "market.raw.trades")
        self.assertIn("sourceEventId", envelope)
        self.assertEqual(envelope["sourceEventId"], "alpaca/sip/trades/AAPL/123/2026-06-25T10:15:20.100Z")

    def test_feed_profiles_and_market_session_contract(self):
        self.assertEqual(resolve_feed_profile({"ALPACA_FEED": "iex"}).profile_id, "iex")
        boats = resolve_feed_profile({"ALPACA_FEED_PROFILE": "overnight"})
        self.assertEqual(boats.feed, "boats")
        self.assertIn("overnight", boats.sessions)
        self.assertEqual(market_session_for_timestamp("2026-06-29T08:30:00.000Z"), "pre")
        self.assertEqual(market_session_for_timestamp("2026-06-29T14:00:00.000Z"), "regular")
        self.assertEqual(market_session_for_timestamp("2026-06-29T21:00:00.000Z"), "after")
        self.assertEqual(market_session_for_timestamp("2026-06-30T02:00:00.000Z"), "overnight")
        self.assertEqual(market_session_for_timestamp("2026-06-28T14:00:00.000Z"), "closed")

    def test_raw_envelope_and_rows_preserve_feed_profile_and_session(self):
        payload = {"T": "t", "S": "AAPL", "t": "2026-06-30T02:00:00.000Z", "p": 200.5, "s": 10, "i": 42}
        envelope = build_raw_envelope(payload, "boats", feed_profile="boats")
        self.assertEqual(envelope["feed"], "boats")
        self.assertEqual(envelope["feedProfile"], "boats")
        self.assertEqual(envelope["marketSession"], "overnight")

        trade = normalize_trade(envelope)
        trade_row = trade_to_clickhouse_row(trade)
        self.assertEqual(trade["feedProfile"], "boats")
        self.assertEqual(trade["marketSession"], "overnight")
        self.assertEqual(trade_row["feed_profile"], "boats")
        self.assertEqual(trade_row["market_session"], "overnight")

        bar_envelope = build_raw_envelope(
            {"T": "b", "S": "AAPL", "t": "2026-06-29T21:00:00.000Z", "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1000},
            "sip",
            feed_profile="sip",
        )
        candle = normalize_bar(bar_envelope)
        candle_row = candle_to_clickhouse_row(candle)
        self.assertEqual(candle["marketSession"], "after")
        self.assertEqual(candle_row["feed_profile"], "sip")
        self.assertEqual(candle_row["market_session"], "after")

    def test_raw_topic_mapping_covers_required_channels(self):
        expected_topics = {
            "b": "market.raw.bars",
            "u": "market.raw.updated-bars",
            "t": "market.raw.trades",
            "q": "market.raw.quotes",
            "d": "market.raw.daily-bars",
            "s": "market.raw.statuses",
            "c": "market.raw.corrections",
            "x": "market.raw.cancel-errors",
        }

        for message_type, topic in expected_topics.items():
            with self.subTest(message_type=message_type):
                self.assertEqual(raw_topic_name("market.raw", message_type), topic)

    def test_processor_runtime_config_prefers_processor_group_id(self):
        config = processor_runtime_config({
            "KAFKA_BOOTSTRAP_SERVERS": "kafka:29092",
            "KAFKA_PROCESSOR_GROUP_ID": "alfaka-market-processor",
            "KAFKA_FLINK_GROUP_ID": "legacy-flink-name",
            "KAFKA_RAW_TOPIC_PREFIX": "market.raw",
            "REDIS_URL": "redis://redis:6379/0",
            "PROCESSOR_RECOVERY_SYMBOLS": "AAPL,MSFT",
            "PROCESSOR_RECOVERY_CLICKHOUSE_ENABLED": "true",
        })

        self.assertEqual(config["group_id"], "alfaka-market-processor")
        self.assertEqual(config["raw_topics"][0], "market.raw.bars")
        self.assertIn("market.raw.cancel-errors", config["raw_topics"])
        self.assertEqual(config["recovery_symbols"], ["AAPL", "MSFT"])
        self.assertTrue(config["clickhouse_recovery_enabled"])

    def test_processor_runtime_config_rejects_placeholders(self):
        with self.assertRaisesRegex(RuntimeError, "placeholder"):
            processor_runtime_config({
                "KAFKA_BOOTSTRAP_SERVERS": "YOUR_MSK_BOOTSTRAP_SERVERS",
                "KAFKA_PROCESSOR_GROUP_ID": "alfaka-market-processor",
                "KAFKA_RAW_TOPIC_PREFIX": "market.raw",
                "REDIS_URL": "redis://redis:6379/0",
            })
        with self.assertRaisesRegex(RuntimeError, "redis_url contains placeholder"):
            processor_runtime_config({
                "KAFKA_BOOTSTRAP_SERVERS": "kafka:29092",
                "KAFKA_PROCESSOR_GROUP_ID": "alfaka-market-processor",
                "KAFKA_RAW_TOPIC_PREFIX": "market.raw",
                "REDIS_URL": "redis://YOUR_REDIS_ENDPOINT:6379/0",
            })

    def test_runtime_config_validation_rejects_empty_and_embedded_placeholders(self):
        self.assertTrue(has_placeholder_value("http://YOUR_CLICKHOUSE_ENDPOINT:8123"))
        self.assertTrue(has_placeholder_value(["market.candles.closed.v1", "REPLACE_TOPIC"]))
        with self.assertRaisesRegex(RuntimeError, "Invalid test component runtime config"):
            validate_required_values("test component", {
                "kafka_servers": "kafka:29092",
                "topics": ["market.candles.closed.v1", ""],
                "redis_url": "redis://redis:6379/0",
            })
        with self.assertRaisesRegex(RuntimeError, "placeholder"):
            validate_required_values("test component", {
                "kafka_servers": "kafka:29092",
                "clickhouse_url": "http://YOUR_CLICKHOUSE_ENDPOINT:8123",
            })

    def test_alpaca_credential_source_can_force_aws_secrets_manager(self):
        class FakeSecretsManager:
            def get_secret_value(self, SecretId):
                self.secret_id = SecretId
                return {"SecretString": json.dumps({
                    "APCA_API_KEY_ID": "aws-key",
                    "APCA_API_SECRET_KEY": "aws-secret",
                })}

        client = FakeSecretsManager()
        with mock.patch.dict(os.environ, {
            "ALPACA_CREDENTIAL_SOURCE": "aws-secrets-manager",
            "ALPACA_SECRET_NAME": "dev/alpaca",
            "AWS_REGION": "ap-northeast-2",
            "APCA_API_KEY_ID": "local-key",
            "APCA_API_SECRET_KEY": "local-secret",
        }):
            with mock.patch("alfaka.common.secrets.boto3.client", return_value=client) as boto_client:
                key, secret = load_alpaca_credentials()

        self.assertEqual((key, secret), ("aws-key", "aws-secret"))
        boto_client.assert_called_once_with("secretsmanager", region_name="ap-northeast-2")
        self.assertEqual(client.secret_id, "dev/alpaca")

    def test_alpaca_credential_source_can_force_local_env(self):
        with mock.patch.dict(os.environ, {
            "ALPACA_CREDENTIAL_SOURCE": "local-env",
            "ALPACA_SECRET_NAME": "dev/alpaca",
            "APCA_API_KEY_ID": "local-key",
            "APCA_API_SECRET_KEY": "local-secret",
        }):
            with mock.patch("alfaka.common.secrets.boto3.client") as boto_client:
                key, secret = load_alpaca_credentials()

        self.assertEqual((key, secret), ("local-key", "local-secret"))
        boto_client.assert_not_called()
        self.assertEqual(resolve_alpaca_credential_source({"ALPACA_CREDENTIAL_SOURCE": "aws"}), "aws-secrets-manager")

    def test_component_health_round_trips_through_redis(self):
        redis_client = MemoryRedis()
        keys = RedisKeyBuilder()

        payload = write_component_health(
            redis_client,
            keys,
            "market-processor",
            lastChannel="bars",
            lastSymbol="AAPL",
        )

        self.assertEqual(payload["component"], "market-processor")
        self.assertEqual(read_component_health(redis_client, keys, "market-processor")["lastSymbol"], "AAPL")

    def test_kafka_consumer_allows_manual_commit_mode(self):
        RecordingKafkaConsumer.calls = []
        kafka_module = types.SimpleNamespace(KafkaConsumer=RecordingKafkaConsumer)

        with mock.patch.dict(sys.modules, {"kafka": kafka_module}):
            consumer = create_json_consumer(
                ["market.candles.closed.v1"],
                "kafka:29092",
                "alfaka-clickhouse-loader",
                "alfaka-clickhouse-consumer",
                enable_auto_commit=False,
            )

        self.assertIsInstance(consumer, RecordingKafkaConsumer)
        self.assertEqual(consumer.topics, ("market.candles.closed.v1",))
        self.assertFalse(consumer.kwargs["enable_auto_commit"])
        self.assertEqual(consumer.kwargs["group_id"], "alfaka-clickhouse-loader")

    def test_live_path_trace_builds_read_only_contract(self):
        with mock.patch.dict(os.environ, {
            "KAFKA_RAW_TOPIC_PREFIX": "market.raw",
            "KAFKA_PROCESSOR_GROUP_ID": "alfaka-market-processor",
            "KAFKA_TICKS_TOPIC": "market.ticks.v1",
            "KAFKA_LIVE_CANDLE_TOPIC": "market.candles.live.1m.v1",
            "KAFKA_CLOSED_CANDLE_TOPIC": "market.candles.closed.v1",
            "KAFKA_STATUS_TOPIC": "market.status.v1",
            "KAFKA_VOLUME_PROFILE_BINS_TOPIC": "market.volume-profile-bins.1m.v1",
        }):
            with mock.patch.object(live_path_trace, "check_api", return_value=live_path_trace.trace_check("api", "ok")):
                with mock.patch.object(live_path_trace, "check_redis", return_value=live_path_trace.trace_check("redis", "warn")):
                    with mock.patch.object(live_path_trace, "check_kafka", return_value=live_path_trace.trace_check("kafka", "ok")):
                        trace = live_path_trace.collect_trace(symbol="nvda", interval="1m")

        self.assertEqual(trace["symbol"], "NVDA")
        self.assertEqual(trace["status"], "warn")
        self.assertEqual(trace["config"]["processorGroupId"], "alfaka-market-processor")
        self.assertIn("Alpaca -> raw Kafka -> Python processor", trace["path"])
        self.assertEqual(live_path_trace.expected_raw_topics("market.raw")[0], "market.raw.bars")
        self.assertEqual(live_path_trace.expected_processed_topics()[0], "market.ticks.v1")
        self.assertEqual(live_path_trace.overall_status([live_path_trace.trace_check("x", "ok")]), "ok")
        self.assertEqual(live_path_trace.overall_status([live_path_trace.trace_check("x", "fail")]), "fail")
        self.assertEqual(live_path_trace.redact_url("redis://user:pass@redis:6379/0"), "redis://***@redis:6379/0")

    def test_processor_smoke_processes_trade_to_topics_and_redis(self):
        producer = RecordingProducer()
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        topics = {
            "ticks": "market.ticks.v1",
            "live_candle": "market.candles.live.1m.v1",
            "closed_candle": "market.candles.closed.v1",
            "status": "market.status.v1",
            "profile": "market.volume-profile-bins.1m.v1",
        }
        envelope = build_raw_envelope(
            {"T": "t", "S": "AAPL", "i": 123, "p": 195.2, "s": 10, "t": "2026-06-25T10:15:20.100Z"},
            "sip",
        )

        result = process_raw_envelope(envelope, producer, redis, keys, ProcessorState(), topics)

        self.assertEqual(result, "trades")
        self.assertEqual([sent["topic"] for sent in producer.sent], [
            "market.ticks.v1",
            "market.candles.live.1m.v1",
            "market.volume-profile-bins.1m.v1",
        ])
        self.assertEqual(redis.hashes[keys.price_latest("AAPL")]["price"], 195.2)
        live_candle = json.loads(redis.values[keys.live_candle("AAPL")])
        self.assertEqual(live_candle["eventType"], "LIVE_CANDLE")
        self.assertFalse(live_candle["isClosed"])
        published_events = [json.loads(value) for _, value in redis.published]
        self.assertTrue(any(event["type"] == "LIVE_CANDLE_UPDATE" and event["symbol"] == "AAPL" for event in published_events))
        health = read_component_health(redis, keys, "market-processor")
        self.assertEqual(health["lastResult"], "trades")
        self.assertEqual(health["lastChannel"], "trades")
        self.assertEqual(health["lastSymbol"], "AAPL")
        self.assertEqual(health["lastSourceEventId"], envelope["sourceEventId"])

    def test_processor_emits_provisional_live_candles_for_all_chart_intervals(self):
        producer = RecordingProducer()
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        topics = {
            "ticks": "market.ticks.v1",
            "live_candle": "market.candles.live.1m.v1",
            "closed_candle": "market.candles.closed.v1",
            "status": "market.status.v1",
            "profile": "market.volume-profile-bins.1m.v1",
        }
        state = ProcessorState()

        for message in (
            {"T": "d", "S": "AAPL", "t": "2026-06-24T00:00:00.000Z", "o": 180, "h": 186, "l": 179, "c": 185, "v": 1000},
            {"T": "b", "S": "AAPL", "t": "2026-06-25T10:15:00.000Z", "o": 190, "h": 191, "l": 189, "c": 190.5, "v": 100},
            {"T": "t", "S": "AAPL", "i": 124, "p": 195.2, "s": 10, "t": "2026-06-25T10:17:20.100Z"},
        ):
            process_raw_envelope(build_raw_envelope(message, "sip"), producer, redis, keys, state, topics)

        live_5m = json.loads(redis.values[keys.live_candle("AAPL", "5m")])
        live_10m = json.loads(redis.values[keys.live_candle("AAPL", "10m")])
        live_1d = json.loads(redis.values[keys.live_candle("AAPL", "1D")])
        live_1w = json.loads(redis.values[keys.live_candle("AAPL", "1W")])
        live_1month = json.loads(redis.values[keys.live_candle("AAPL", "1M")])

        self.assertEqual(live_5m["timestamp"], "2026-06-25T10:15:00.000Z")
        self.assertEqual(live_5m["open"], 190)
        self.assertEqual(live_5m["high"], 195.2)
        self.assertEqual(live_5m["low"], 189)
        self.assertEqual(live_5m["close"], 195.2)
        self.assertEqual(live_5m["volume"], 110)
        self.assertEqual(live_5m["sourceInterval"], "1m")
        self.assertFalse(live_5m["isClosed"])
        self.assertEqual(live_10m["timestamp"], "2026-06-25T10:10:00.000Z")
        self.assertEqual(live_1d["timestamp"], "2026-06-25T00:00:00.000Z")
        self.assertEqual(live_1d["sourceInterval"], "1m")
        self.assertEqual(live_1w["timestamp"], "2026-06-22T00:00:00.000Z")
        self.assertEqual(live_1w["sourceInterval"], "1D")
        self.assertEqual(live_1month["timestamp"], "2026-06-01T00:00:00.000Z")
        self.assertEqual(live_1month["sourceInterval"], "1D")

        live_events = [
            json.loads(value)
            for _, value in redis.published
            if json.loads(value).get("type") == "LIVE_CANDLE_UPDATE"
        ]
        intervals = {event["interval"] for event in live_events}
        self.assertTrue({"1m", "5m", "10m", "1D", "1W", "1M"}.issubset(intervals))
        latest_5m_event = [event for event in live_events if event["interval"] == "5m"][-1]
        self.assertEqual(latest_5m_event["source"], "derived.live")
        self.assertEqual(latest_5m_event["sourceInterval"], "1m")
        self.assertEqual(latest_5m_event["data"]["sourceInterval"], "1m")
        self.assertIn("updatedAt", latest_5m_event["data"])

    def test_processor_recovers_provisional_state_from_redis_before_next_trade(self):
        producer = RecordingProducer()
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        topics = {
            "ticks": "market.ticks.v1",
            "live_candle": "market.candles.live.1m.v1",
            "closed_candle": "market.candles.closed.v1",
            "status": "market.status.v1",
            "profile": "market.volume-profile-bins.1m.v1",
        }
        write_closed_candle_to_redis(redis, keys, {
            "eventType": "CANDLE",
            "symbol": "AAPL",
            "interval": "1m",
            "timestamp": "2026-06-25T10:15:00.000Z",
            "open": 190,
            "high": 191,
            "low": 189,
            "close": 190.5,
            "volume": 100,
            "isClosed": True,
            "source": "alpaca.bars",
            "feed": "sip",
            "sourceEventId": "closed-1m",
            "createdAt": "2026-06-25T10:16:00.000Z",
        })
        write_closed_candle_to_redis(redis, keys, {
            "eventType": "CANDLE",
            "symbol": "AAPL",
            "interval": "1D",
            "timestamp": "2026-06-24T00:00:00.000Z",
            "open": 180,
            "high": 186,
            "low": 179,
            "close": 185,
            "volume": 1000,
            "isClosed": True,
            "source": "alpaca.dailyBars",
            "feed": "sip",
            "sourceEventId": "closed-1d",
            "createdAt": "2026-06-24T21:00:00.000Z",
        })
        state = ProcessorState()

        recovered = recover_processor_state_from_redis(redis, keys, state, ["AAPL"])
        process_raw_envelope(
            build_raw_envelope({"T": "t", "S": "AAPL", "i": 125, "p": 195.2, "s": 10, "t": "2026-06-25T10:17:20.100Z"}, "sip"),
            producer,
            redis,
            keys,
            state,
            topics,
        )

        live_5m = json.loads(redis.values[keys.live_candle("AAPL", "5m")])
        live_1w = json.loads(redis.values[keys.live_candle("AAPL", "1W")])
        self.assertEqual(recovered["symbols"], 1)
        self.assertEqual(recovered["closed"]["1m"], 1)
        self.assertEqual(recovered["closed"]["1D"], 1)
        self.assertEqual(live_5m["open"], 190)
        self.assertEqual(live_5m["volume"], 110)
        self.assertEqual(live_1w["open"], 180)
        self.assertEqual(live_1w["close"], 195.2)

    def test_processor_recovers_provisional_state_from_clickhouse_when_enabled(self):
        producer = RecordingProducer()
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        topics = {
            "ticks": "market.ticks.v1",
            "live_candle": "market.candles.live.1m.v1",
            "closed_candle": "market.candles.closed.v1",
            "status": "market.status.v1",
            "profile": "market.volume-profile-bins.1m.v1",
        }
        state = ProcessorState()
        provider = FakeClickHouseRecoveryProvider()

        recovered = recover_processor_state_from_clickhouse(state, ["AAPL"], provider=provider)
        process_raw_envelope(
            build_raw_envelope({"T": "t", "S": "AAPL", "i": 126, "p": 195.2, "s": 10, "t": "2026-06-25T10:17:20.100Z"}, "sip"),
            producer,
            redis,
            keys,
            state,
            topics,
        )

        live_5m = json.loads(redis.values[keys.live_candle("AAPL", "5m")])
        live_1w = json.loads(redis.values[keys.live_candle("AAPL", "1W")])
        self.assertEqual(recovered["symbols"], 1)
        self.assertEqual(recovered["closed"]["1m"], 1)
        self.assertEqual(recovered["closed"]["1D"], 1)
        self.assertEqual(provider.calls[0], ("AAPL", "1m", redis_closed_candle_cap("1m")))
        self.assertEqual(live_5m["open"], 190)
        self.assertEqual(live_5m["volume"], 110)
        self.assertEqual(live_1w["open"], 180)
        self.assertEqual(live_1w["close"], 195.2)

    def test_official_bar_event_replaces_matching_trade_live_bucket_contract(self):
        producer = RecordingProducer()
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        topics = {
            "ticks": "market.ticks.v1",
            "live_candle": "market.candles.live.1m.v1",
            "closed_candle": "market.candles.closed.v1",
            "status": "market.status.v1",
            "profile": "market.volume-profile-bins.1m.v1",
        }
        state = ProcessorState()

        process_raw_envelope(
            build_raw_envelope({"T": "t", "S": "AAPL", "i": 123, "p": 195.2, "s": 10, "t": "2026-06-25T10:15:20.100Z"}, "sip"),
            producer,
            redis,
            keys,
            state,
            topics,
        )
        process_raw_envelope(
            build_raw_envelope({"T": "b", "S": "AAPL", "t": "2026-06-25T10:15:00.000Z", "o": 195, "h": 196, "l": 194, "c": 195.5, "v": 100}, "sip"),
            producer,
            redis,
            keys,
            state,
            topics,
        )

        events = [json.loads(value) for _, value in redis.published]
        one_minute_events = [event for event in events if event.get("symbol") == "AAPL" and event.get("interval") == "1m"]
        self.assertEqual(one_minute_events[0]["type"], "LIVE_CANDLE_UPDATE")
        self.assertFalse(one_minute_events[0]["data"]["isClosed"])
        self.assertEqual(one_minute_events[-1]["type"], "CANDLE_CLOSED")
        self.assertTrue(one_minute_events[-1]["data"]["isClosed"])
        self.assertEqual(one_minute_events[-1]["data"]["timestamp"], "2026-06-25T10:15:00.000Z")

    def test_processor_smoke_processes_bar_to_closed_candle_and_pubsub(self):
        producer = RecordingProducer()
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        topics = {
            "ticks": "market.ticks.v1",
            "live_candle": "market.candles.live.1m.v1",
            "closed_candle": "market.candles.closed.v1",
            "status": "market.status.v1",
            "profile": "market.volume-profile-bins.1m.v1",
        }
        envelope = build_raw_envelope(
            {"T": "b", "S": "AAPL", "t": "2026-06-25T10:15:00.000Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 100},
            "sip",
        )

        result = process_raw_envelope(envelope, producer, redis, keys, ProcessorState(), topics)

        self.assertEqual(result, "bars")
        self.assertEqual(producer.sent[0]["topic"], "market.candles.closed.v1")
        self.assertEqual(producer.sent[0]["value"]["interval"], "1m")
        self.assertTrue(producer.sent[0]["value"]["isClosed"])
        self.assertIn(keys.latest_candle("AAPL", "1m"), redis.values)
        self.assertIn(keys.recent_candles("AAPL", "1m"), redis.zsets)
        published_events = [json.loads(value) for _, value in redis.published]
        self.assertTrue(any(event["type"] == "CANDLE_CLOSED" and event["interval"] == "1m" for event in published_events))

    def test_closed_candle_redis_series_is_trimmed_to_interval_cap(self):
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        cap = redis_closed_candle_cap("1m")
        start = datetime(2026, 6, 25, 9, 30, tzinfo=timezone.utc)

        for index in range(cap + 3):
            timestamp = (start + timedelta(minutes=index)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            write_closed_candle_to_redis(redis, keys, {
                "eventType": "CANDLE",
                "symbol": "AAPL",
                "interval": "1m",
                "timestamp": timestamp,
                "open": index,
                "high": index,
                "low": index,
                "close": index,
                "volume": index,
                "isClosed": True,
            })

        series = redis.zsets[keys.recent_candles("AAPL", "1m")]
        remaining = [
            json.loads(member)["timestamp"]
            for member, _score in sorted(series.items(), key=lambda item: item[1])
        ]
        self.assertEqual(len(remaining), cap)
        self.assertEqual(remaining[0], (start + timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
        self.assertEqual(remaining[-1], (start + timedelta(minutes=cap + 2)).strftime("%Y-%m-%dT%H:%M:%S.000Z"))

    def test_kubernetes_base_includes_market_processor_runtime_unit(self):
        base_kustomization = (REPO_ROOT / "infra/k8s/base/kustomization.yaml").read_text(encoding="utf-8")
        deployment = (REPO_ROOT / "infra/k8s/base/deployment-market-processor.yaml").read_text(encoding="utf-8")
        raw_archive_deployment = (REPO_ROOT / "infra/k8s/base/deployment-raw-s3-archive.yaml").read_text(encoding="utf-8")
        configmap = (REPO_ROOT / "infra/k8s/base/configmap.yaml").read_text(encoding="utf-8")
        aws_overlay = (REPO_ROOT / "infra/k8s/overlays/aws/kustomization.yaml").read_text(encoding="utf-8")

        self.assertIn("deployment-market-processor.yaml", base_kustomization)
        self.assertIn("deployment-raw-s3-archive.yaml", base_kustomization)
        self.assertIn("name: alfaka-market-processor", deployment)
        self.assertIn("app: alfaka-market-processor", deployment)
        self.assertIn("gops-market-processor:latest", deployment)
        self.assertIn("systems/market-data/pods/market-processor/local_main.py", deployment)
        self.assertIn("name: alfaka-raw-s3-archive", raw_archive_deployment)
        self.assertIn("systems/market-data/pods/s3-sink/raw_archive_sink.py", raw_archive_deployment)
        self.assertIn("gops-market-storage:latest", raw_archive_deployment)
        self.assertIn("KAFKA_PROCESSOR_GROUP_ID: alfaka-market-processor", configmap)
        self.assertIn("KAFKA_RAW_S3_GROUP_ID: alfaka-raw-s3-archive", configmap)
        self.assertIn('S3_RAW_FLUSH_INTERVAL_SECONDS: "60"', configmap)
        self.assertIn('KAFKA_CLICKHOUSE_ENABLE_AUTO_COMMIT: "false"', configmap)
        self.assertIn("Python market-processor pod", aws_overlay)
        self.assertNotIn("local-stream-processor는 운영 Flink가 아니므로 포함하지 않습니다", aws_overlay)

    def test_initial_load_compose_uses_sp500_universe_contract(self):
        compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn('ALPACA_UNIVERSE: "sp500"', compose)
        self.assertIn('ALPACA_UNIVERSE_REGISTRY_PATH: "systems/market-data/config/sp500-universe.json"', compose)
        self.assertIn('INITIAL_LOAD_INTERVALS: "${INITIAL_LOAD_INTERVALS:-1D}"', compose)

    def test_raw_archive_batches_historical_bars_by_day(self):
        s3 = RecordingS3()
        rows = [
            alpaca_raw_bar("2026-06-25T13:30:00.000Z", index=0),
            alpaca_raw_bar("2026-06-25T14:30:00.000Z", index=1),
            alpaca_raw_bar("2026-06-26T13:30:00.000Z", index=2),
        ]

        count = upload_raw_page_to_s3(
            s3,
            "bucket",
            "market-data/raw/alpaca",
            "bars",
            "sip",
            "2026-06-25T00:00:00.000Z",
            "2026-06-26T23:59:00.000Z",
            1,
            {"AAPL": rows},
        )

        self.assertEqual(count, 3)
        self.assertEqual(len(s3.objects), 2)
        keys = [obj["Key"] for obj in s3.objects]
        self.assertTrue(any(key.startswith("market-data/raw/alpaca/source=alpaca/channel=bars/symbol=AAPL/year=2026/month=06/day=25/part-000001-") for key in keys))
        self.assertTrue(any(key.startswith("market-data/raw/alpaca/source=alpaca/channel=bars/symbol=AAPL/year=2026/month=06/day=26/part-000001-") for key in keys))
        self.assertNotIn("/hour=", "\n".join(keys))

    def test_raw_partition_key_does_not_split_historical_pages_by_hour(self):
        self.assertEqual(
            raw_partition_key("market-data/raw/alpaca", "bars", "AAPL", "2026-06-25T13:30:00.000Z"),
            "market-data/raw/alpaca/source=alpaca/channel=bars/symbol=AAPL/year=2026/month=06/day=25",
        )

    def test_raw_chunk_partition_key_groups_historical_request(self):
        self.assertEqual(
            raw_chunk_partition_key("market-data/raw/alpaca", "daily-bars", "AAPL", "backfill_AAPL_1D_chunk"),
            "market-data/raw/alpaca/source=alpaca/channel=daily-bars/symbol=AAPL/request=backfill_AAPL_1D_chunk",
        )

    def test_raw_historical_archive_can_write_compact_chunk_object(self):
        s3 = S3ObjectStore()
        rows = [
            alpaca_raw_bar("2026-06-25T00:00:00.000Z", index=0),
            alpaca_raw_bar("2026-06-26T00:00:00.000Z", index=1),
        ]

        count = upload_raw_page_to_s3(
            s3,
            "bucket",
            "market-data/raw/alpaca",
            "daily-bars",
            "sip",
            "2026-06-25T00:00:00.000Z",
            "2026-06-27T00:00:00.000Z",
            1,
            {"AAPL": rows},
            manifest_prefix="market-data/manifest",
            object_id="backfill:AAPL:1D:chunk",
            partition_mode="chunk",
        )
        raw_keys = [key for key in s3.objects if key.startswith("market-data/raw/alpaca/")]
        manifest_keys = [key for key in s3.objects if key.startswith("market-data/manifest/raw/")]
        lookup_keys = raw_keys_from_manifest(
            s3,
            "bucket",
            "market-data/manifest",
            "AAPL",
            ["daily-bars"],
            "2026-06-26T00:00:00.000Z",
            "2026-06-27T00:00:00.000Z",
        )

        self.assertEqual(count, 2)
        self.assertEqual(len(raw_keys), 1)
        self.assertIn("/request=backfill_AAPL_1D_chunk/", raw_keys[0])
        self.assertEqual(len(manifest_keys), 1)
        self.assertEqual(lookup_keys, raw_keys)

    def test_raw_historical_archive_keys_include_range_or_job_suffix(self):
        s3 = RecordingS3()
        first_rows = [alpaca_raw_bar("2026-06-25T13:30:00.000Z", index=0)]
        second_rows = [alpaca_raw_bar("2026-06-25T14:30:00.000Z", index=1)]

        upload_raw_page_to_s3(
            s3,
            "bucket",
            "market-data/raw/alpaca",
            "bars",
            "sip",
            "2026-06-25T13:30:00.000Z",
            "2026-06-25T13:31:00.000Z",
            1,
            {"AAPL": first_rows},
            object_id="backfill:AAPL:1m:first",
        )
        upload_raw_page_to_s3(
            s3,
            "bucket",
            "market-data/raw/alpaca",
            "bars",
            "sip",
            "2026-06-25T14:30:00.000Z",
            "2026-06-25T14:31:00.000Z",
            1,
            {"AAPL": second_rows},
            object_id="backfill:AAPL:1m:second",
        )

        keys = [obj["Key"] for obj in s3.objects]
        self.assertEqual(len(set(keys)), 2)
        self.assertIn("part-000001-backfill_AAPL_1m_first.jsonl", keys[0])
        self.assertIn("part-000001-backfill_AAPL_1m_second.jsonl", keys[1])
        self.assertNotEqual(raw_object_suffix("bars", "sip", "start-a", "end-a", 1), raw_object_suffix("bars", "sip", "start-b", "end-b", 1))

    def test_daily_bar_normalizes_to_canonical_1d_candle(self):
        envelope = build_raw_envelope(
            {"T": "d", "S": "AAPL", "t": "2026-06-25T00:00:00.000Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 100},
            "sip",
        )
        candle = normalize_bar(envelope)

        self.assertEqual(candle["interval"], "1D")
        self.assertEqual(candle["source"], "alpaca.dailyBars")
        self.assertEqual(candle["sourceEventId"], envelope["sourceEventId"])

    def test_status_normalizes_and_builds_delta(self):
        envelope = build_raw_envelope(
            {"T": "s", "S": "AAPL", "t": "2026-06-25T13:30:00.000Z", "sc": "active", "st": "trading"},
            "sip",
        )
        status = normalize_status(envelope)
        event = market_status_event(status)

        self.assertEqual(status["eventType"], "MARKET_STATUS")
        self.assertEqual(event["type"], "MARKET_STATUS_UPDATE")
        self.assertIn("eventId", event)
        self.assertIn("cursor", event)

    def test_updated_bar_preserves_correction_type_and_source(self):
        envelope = build_raw_envelope(
            {"T": "u", "S": "AAPL", "t": "2026-06-25T10:15:00.000Z", "o": 1, "h": 3, "l": 1, "c": 2, "v": 200},
            "sip",
        )

        candle = normalize_bar(envelope, correction_type="UPDATED")

        self.assertEqual(candle["interval"], "1m")
        self.assertEqual(candle["source"], "alpaca.updatedBars")
        self.assertEqual(candle["correctionType"], "UPDATED")
        self.assertEqual(candle["sourceEventId"], envelope["sourceEventId"])

    def test_trade_profile_bin_and_live_delta_have_cursor(self):
        envelope = build_raw_envelope(
            {"T": "t", "S": "AAPL", "i": 123, "p": 195.22, "s": 10, "t": "2026-06-25T10:15:20.100Z"},
            "sip",
        )
        trade = normalize_trade(envelope)
        profile_bin = VolumeProfileBinBuilder(price_bin_size=0.05).update(trade)
        event = websocket_event("LIVE_CANDLE_UPDATE", "AAPL", "1m", {
            "timestamp": "2026-06-25T10:15:00.000Z",
            "open": 195.22,
            "high": 195.22,
            "low": 195.22,
            "close": 195.22,
            "volume": 10,
            "isClosed": False,
            "source": "alpaca.trades",
            "sourceInterval": "trades",
            "sourceEventId": envelope["sourceEventId"],
            "updatedAt": "2026-06-25T10:15:20.250Z",
        })

        self.assertEqual(profile_bin["eventType"], "VOLUME_PROFILE_BIN")
        self.assertEqual(profile_bin["priceBinSize"], 0.05)
        self.assertIn("eventId", event)
        self.assertIn("cursor", event)
        self.assertEqual(event["source"], "alpaca.trades")
        self.assertEqual(event["sourceInterval"], "trades")
        self.assertEqual(event["data"]["sourceInterval"], "trades")
        self.assertEqual(event["data"]["updatedAt"], "2026-06-25T10:15:20.250Z")

    def test_redis_provider_reads_interval_specific_live_candle(self):
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        redis.set(keys.live_candle("AAPL", "5m"), json.dumps({
            "eventType": "LIVE_CANDLE",
            "symbol": "AAPL",
            "interval": "5m",
            "timestamp": "2026-06-25T10:15:00.000Z",
            "open": 190,
            "high": 195,
            "low": 189,
            "close": 194,
            "volume": 110,
            "isClosed": False,
            "source": "derived.live",
            "sourceInterval": "1m",
            "updatedAt": "2026-06-25T10:17:20.250Z",
        }))
        provider = RedisMarketDataProvider.__new__(RedisMarketDataProvider)
        provider.redis = redis
        provider.keys = keys

        event = provider.live_event("AAPL", "5m")

        self.assertEqual(event["interval"], "5m")
        self.assertEqual(event["source"], "derived.live")
        self.assertEqual(event["sourceInterval"], "1m")
        self.assertEqual(event["data"]["updatedAt"], "2026-06-25T10:17:20.250Z")

    def test_alpaca_asset_maps_to_symbol_metadata_contract(self):
        metadata = asset_to_symbol_metadata(
            {
                "symbol": "NVDA",
                "name": "NVIDIA Corporation",
                "exchange": "NASDAQ",
                "asset_class": "us_equity",
                "tradable": True,
                "status": "active",
            },
            updated_at="2026-06-25T00:00:00.000Z",
        )
        row = symbol_to_clickhouse_row(metadata)

        self.assertEqual(metadata["eventType"], "SYMBOL_METADATA")
        self.assertEqual(metadata["market"], "US")
        self.assertEqual(row["symbol"], "NVDA")
        self.assertEqual(row["asset_class"], "us_equity")
        self.assertEqual(row["source"], "alpaca")

    def test_alpaca_news_article_maps_to_processed_event_and_clickhouse_row(self):
        article = {
            "id": 123,
            "headline": "NVIDIA announces new AI chip",
            "summary": "NVIDIA shares moved after a product announcement.",
            "url": "https://example.com/nvda-news",
            "source": "ExampleWire",
            "author": "Reporter",
            "created_at": "2026-06-29T01:02:03Z",
            "updated_at": "2026-06-29T02:03:04Z",
            "symbols": ["NVDA", "AAPL"],
        }

        events = build_news_events(article, requested_symbols=["NVDA"], received_at="2026-06-29T03:04:05Z")
        row = news_to_clickhouse_row(events[0])

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["eventType"], "NEWS_ARTICLE")
        self.assertEqual(events[0]["symbol"], "NVDA")
        self.assertEqual(events[0]["sourceEventId"], "alpaca/news/NVDA/123")
        self.assertEqual(row["symbol"], "NVDA")
        self.assertEqual(row["article_id"], "123")
        self.assertEqual(row["headline"], "NVIDIA announces new AI chip")
        self.assertEqual(row["published_at"], "2026-06-29 01:02:03.000")

    def test_candle_aggregator_builds_5m_contract(self):
        aggregator = CandleAggregator()
        aggregated = None
        for minute in range(5):
            aggregated = aggregator.update({
                "eventType": "CANDLE",
                "symbol": "AAPL",
                "interval": "1m",
                "timestamp": f"2026-06-25T10:{minute:02d}:00.000Z",
                "open": 100 + minute,
                "high": 101 + minute,
                "low": 99 + minute,
                "close": 100.5 + minute,
                "volume": 100 + minute,
                "tradeCount": 10 + minute,
                "vwap": 100.25 + minute,
                "correctionType": "NONE",
                "feed": "sip",
                "sourceEventId": f"event-{minute}",
                "createdAt": "2026-06-25T10:00:01.000Z",
            }, 5)

        self.assertIsNotNone(aggregated)
        self.assertEqual(aggregated["interval"], "5m")
        self.assertEqual(aggregated["timestamp"], "2026-06-25T10:00:00.000Z")
        self.assertEqual(aggregated["open"], 100)
        self.assertEqual(aggregated["close"], 104.5)
        self.assertEqual(aggregated["volume"], 510)

    def test_redis_key_prefix_keeps_contract_namespaced(self):
        keys = RedisKeyBuilder("gops-prod")

        self.assertEqual(keys.price_latest("AAPL"), "gops-prod:price:AAPL:latest")
        self.assertEqual(keys.market_events_symbol("AAPL"), "gops-prod:market.events:AAPL")
        self.assertEqual(keys.active_symbols(), "gops-prod:active:charts:symbols")
        self.assertEqual(keys.backfill_lock("AAPL", "1m", "abc"), "gops-prod:backfill:lock:AAPL:1m:abc")
        self.assertEqual(keys.backfill_status("request-1"), "gops-prod:backfill:status:request-1")
        self.assertEqual(keys.backfill_latest("AAPL", "1m"), "gops-prod:backfill:latest:AAPL:1m")
        self.assertEqual(keys.backfill_stream(), "gops-prod:backfill:stream")
        self.assertEqual(keys.backfill_dead_letter_stream(), "gops-prod:backfill:dead-letter")

    def test_backfill_store_deduplicates_same_symbol_interval_range(self):
        store = RedisBackfillStore(redis_client=MemoryRedis(), ttl_seconds=60)

        first, first_deduped = store.create_request(
            "INTC",
            "1m",
            start="2026-06-25T13:30:00.000Z",
            end="2026-06-25T14:30:00.000Z",
            mode="queue",
        )
        second, second_deduped = store.create_request(
            "INTC",
            "1m",
            start="2026-06-25T13:30:00.000Z",
            end="2026-06-25T14:30:00.000Z",
            mode="queue",
        )

        self.assertFalse(first_deduped)
        self.assertTrue(second_deduped)
        self.assertEqual(first["requestId"], second["requestId"])
        self.assertEqual(store.latest_status("INTC", "1m")["status"], "queued")
        self.assertEqual(store.pop_queued_request_id(), first["requestId"])
        self.assertIsNone(store.pop_queued_request_id())

    def test_backfill_store_stream_claim_ack_and_job_contract(self):
        redis_client = MemoryRedis()
        store = RedisBackfillStore(redis_client=redis_client, ttl_seconds=60)

        record, deduped = store.create_request(
            "INTC",
            "1m",
            start="2026-06-25T13:30:00.000Z",
            end="2026-06-25T14:30:00.000Z",
            mode="queue",
            job_type="gapfill",
        )
        item = store.read_next_queue_item(consumer_name="worker-a", timeout=0)
        claimed = store.mark_job_claimed(store.get_status(record["requestId"]), item, consumer_name="worker-a")

        self.assertFalse(deduped)
        self.assertEqual(record["queueBackend"], "streams")
        self.assertEqual(record["jobType"], "gapfill")
        self.assertEqual(record["sourcePreference"], "coverage-first")
        self.assertEqual(record["idempotencyKey"], record["requestId"])
        self.assertEqual(item.request_id, record["requestId"])
        self.assertEqual(item.stream_id, record["streamId"])
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(claimed["claimedBy"], "worker-a")
        self.assertEqual(claimed["attempt"], 1)
        self.assertEqual(store.ack_queue_item(item), 1)
        self.assertEqual(redis_client.xpending_range(store.keys.backfill_stream(), store.stream_group), [])

    def test_backfill_store_reclaims_stale_pending_stream_job(self):
        store = RedisBackfillStore(redis_client=MemoryRedis(), ttl_seconds=60)
        record, _deduped = store.create_request(
            "NVDA",
            "1D",
            start="2023-06-25T00:00:00.000Z",
            end="2026-06-25T00:00:00.000Z",
            mode="queue",
        )

        first_item = store.read_next_queue_item(consumer_name="worker-a", timeout=0)
        second_item = store.read_next_queue_item(consumer_name="worker-b", timeout=0, reclaim_idle_ms=0, max_attempts=3)
        claimed = store.mark_job_claimed(store.get_status(record["requestId"]), second_item, consumer_name="worker-b")

        self.assertEqual(first_item.request_id, record["requestId"])
        self.assertEqual(second_item.request_id, record["requestId"])
        self.assertEqual(second_item.stream_id, first_item.stream_id)
        self.assertEqual(second_item.delivery_count, 2)
        self.assertEqual(claimed["claimedBy"], "worker-b")
        self.assertEqual(claimed["attempt"], 2)

    def test_backfill_store_dead_letters_after_retry_limit(self):
        redis_client = MemoryRedis()
        store = RedisBackfillStore(redis_client=redis_client, ttl_seconds=60)
        record, _deduped = store.create_request(
            "TSLA",
            "1m",
            start="2026-06-25T13:30:00.000Z",
            end="2026-06-25T14:30:00.000Z",
            mode="queue",
        )

        first_item = store.read_next_queue_item(consumer_name="worker-a", timeout=0)
        second_item = store.read_next_queue_item(consumer_name="worker-b", timeout=0, reclaim_idle_ms=0, max_attempts=1)
        self.assertIsNone(second_item)

        status = store.get_status(record["requestId"])
        self.assertEqual(status["status"], "failed")
        self.assertIn("dead-lettered", status["error"])
        self.assertEqual(store.ack_queue_item(first_item), 0)
        self.assertEqual(redis_client.xlen(store.keys.backfill_dead_letter_stream()), 1)

    def test_backfill_store_queue_metrics_report_stream_health(self):
        redis_client = MemoryRedis()
        store = RedisBackfillStore(redis_client=redis_client, ttl_seconds=60)
        first, _ = store.create_request(
            "AAPL",
            "1m",
            start="2026-06-25T13:30:00.000Z",
            end="2026-06-25T14:30:00.000Z",
            mode="queue",
        )
        second, _ = store.create_request(
            "MSFT",
            "1m",
            start="2026-06-25T13:30:00.000Z",
            end="2026-06-25T14:30:00.000Z",
            mode="queue",
        )

        first_item = store.read_next_queue_item(consumer_name="worker-a", timeout=0)
        second_item = store.read_next_queue_item(consumer_name="worker-b", timeout=0)
        store.dead_letter_queue_item(second_item, store.get_status(second["requestId"]), reason="test_dead_letter")
        third, _ = store.create_request(
            "NVDA",
            "1D",
            start="2023-06-25T00:00:00.000Z",
            end="2026-06-25T00:00:00.000Z",
            mode="queue",
        )

        metrics = store.queue_metrics()

        self.assertEqual(first_item.request_id, first["requestId"])
        self.assertEqual(metrics["queueBackend"], "streams")
        self.assertEqual(metrics["stream"]["retainedLength"], 3)
        self.assertEqual(metrics["stream"]["pendingCount"], 1)
        self.assertEqual(metrics["stream"]["undeliveredCount"], 1)
        self.assertEqual(metrics["stream"]["backlogCount"], 2)
        self.assertEqual(metrics["stream"]["oldestPending"]["requestId"], first["requestId"])
        self.assertEqual(metrics["stream"]["lastDeliveredId"], second["streamId"])
        self.assertEqual(metrics["deadLetter"]["length"], 1)
        self.assertEqual(third["status"], "queued")

    def test_backfill_store_initial_load_requests_are_chunked_and_backpressured(self):
        store = RedisBackfillStore(redis_client=MemoryRedis(), ttl_seconds=60)

        result = store.create_initial_load_requests(
            ["aapl", "msft"],
            "1m",
            "2026-01-01T00:00:00.000Z",
            "2026-01-11T00:00:00.000Z",
            chunk_days=2,
            max_enqueued=3,
            max_backlog=3,
        )
        saturated = store.create_initial_load_requests(
            ["nvda"],
            "1m",
            "2026-01-01T00:00:00.000Z",
            "2026-01-03T00:00:00.000Z",
            chunk_days=1,
            max_enqueued=10,
            max_backlog=3,
        )
        metrics = store.queue_metrics()

        self.assertEqual(result["jobType"], "initial_load")
        self.assertEqual(result["chunkCount"], 10)
        self.assertEqual(result["createdCount"], 3)
        self.assertTrue(result["throttled"])
        self.assertEqual(result["requests"][0]["symbol"], "AAPL")
        first_status = store.get_status(result["requests"][0]["requestId"])
        self.assertEqual(first_status["jobType"], "initial_load")
        self.assertEqual(first_status["priority"], "bulk")
        self.assertEqual(first_status["range"], {
            "start": "2026-01-01T00:00:00.000Z",
            "end": "2026-01-03T00:00:00.000Z",
        })
        self.assertEqual(metrics["stream"]["backlogCount"], 3)
        self.assertTrue(saturated["throttled"])
        self.assertEqual(saturated["createdCount"], 0)
        self.assertEqual(saturated["backlogBefore"], 3)

    def test_initial_load_job_dry_run_and_enqueue_paths(self):
        module = load_initial_load_job_module()
        store = RedisBackfillStore(redis_client=MemoryRedis(), ttl_seconds=60)

        dry_run = module.plan_initial_load(
            store,
            symbols=["AAPL"],
            intervals=["1m"],
            start="2026-01-01T00:00:00.000Z",
            end="2026-01-11T00:00:00.000Z",
            dry_run=True,
        )
        enqueued = module.plan_initial_load(
            store,
            symbols=["AAPL"],
            intervals=["1m"],
            start="2026-01-01T00:00:00.000Z",
            end="2026-01-11T00:00:00.000Z",
            dry_run=False,
            max_enqueued=1,
            max_backlog=1,
        )

        self.assertTrue(dry_run["dryRun"])
        self.assertEqual(dry_run["failures"], 0)
        self.assertEqual(dry_run["items"][0]["chunkCount"], 2)
        self.assertEqual(dry_run["items"][0]["createdCount"], 0)
        self.assertEqual(dry_run["items"][0]["symbolSource"], "explicit")
        self.assertEqual(dry_run["items"][0]["chunksPerSymbol"], 2)
        self.assertGreater(dry_run["items"][0]["estimate"]["totalRows"], 0)
        self.assertEqual(dry_run["items"][0]["estimate"]["rawObjects"], 2)
        self.assertEqual(dry_run["items"][0]["resume"]["strategy"], "idempotent_chunk_requests")
        self.assertIn("row_count", dry_run["items"][0]["s3Validation"]["requiredBeforeRealPreload"])
        self.assertFalse(enqueued["dryRun"])
        self.assertEqual(enqueued["items"][0]["createdCount"], 1)
        self.assertTrue(enqueued["items"][0]["throttled"])
        request_id = enqueued["items"][0]["requests"][0]["requestId"]
        self.assertEqual(store.get_status(request_id)["jobType"], "initial_load")

    def test_initial_load_1m_estimate_uses_extended_hours_minutes(self):
        module = load_initial_load_job_module()

        with mock.patch.dict(os.environ, {"HISTORICAL_1M_MINUTES_PER_TRADING_DAY": "960"}):
            estimate = module.estimate_preload_size(
                symbol_count=2,
                interval="1m",
                start="2026-01-01T00:00:00.000Z",
                end="2026-01-08T00:00:00.000Z",
                chunks_per_symbol=2,
            )

        self.assertEqual(estimate["rowsPerSymbol"], module.estimate_trading_days("2026-01-01T00:00:00.000Z", "2026-01-08T00:00:00.000Z") * 960)
        self.assertEqual(estimate["totalRows"], estimate["rowsPerSymbol"] * 2)
        self.assertEqual(estimate["rawObjectPartitionMode"], "chunk")

    def test_initial_load_1m_rejects_ranges_before_configured_min_start(self):
        module = load_initial_load_job_module()
        store = RedisBackfillStore(redis_client=MemoryRedis(), ttl_seconds=60)

        with mock.patch.dict(os.environ, {"BACKFILL_INITIAL_LOAD_1M_MIN_START": "2025-04-01T00:00:00Z"}):
            with self.assertRaisesRegex(ValueError, "before BACKFILL_INITIAL_LOAD_1M_MIN_START"):
                store.create_initial_load_requests(
                    ["AAPL"],
                    "1m",
                    "2025-03-01T00:00:00.000Z",
                    "2025-04-01T00:00:00.000Z",
                    max_enqueued=1,
                    max_backlog=10,
                )

            dry_run = module.plan_initial_load(
                store,
                symbols=["AAPL"],
                intervals=["1m"],
                start="2025-03-01T00:00:00.000Z",
                end="2025-04-01T00:00:00.000Z",
                dry_run=True,
            )

            allowed_1m = store.create_initial_load_requests(
                ["AAPL"],
                "1m",
                "2025-04-01T00:00:00.000Z",
                "2025-04-06T00:00:00.000Z",
                max_enqueued=1,
                max_backlog=10,
            )
            allowed_1d = store.create_initial_load_requests(
                ["AAPL"],
                "1D",
                "2023-06-30T00:00:00.000Z",
                "2026-06-30T00:00:00.000Z",
                max_enqueued=1,
                max_backlog=10,
            )

        self.assertEqual(dry_run["failures"], 1)
        self.assertIn("2025-04-01T00:00:00.000Z", dry_run["items"][0]["error"])
        self.assertEqual(allowed_1m["createdCount"], 1)
        self.assertEqual(allowed_1d["createdCount"], 1)

    def test_initial_load_resume_skips_existing_chunks_without_consuming_capacity(self):
        store = RedisBackfillStore(redis_client=MemoryRedis(), ttl_seconds=60)
        first = store.create_initial_load_requests(
            ["AAPL"],
            "1m",
            "2026-01-01T00:00:00.000Z",
            "2026-01-07T00:00:00.000Z",
            chunk_days=2,
            max_enqueued=1,
            max_backlog=10,
        )

        second = store.create_initial_load_requests(
            ["AAPL"],
            "1m",
            "2026-01-01T00:00:00.000Z",
            "2026-01-07T00:00:00.000Z",
            chunk_days=2,
            max_enqueued=1,
            max_backlog=10,
        )

        self.assertEqual(first["createdCount"], 1)
        self.assertEqual(first["requests"][0]["range"]["start"], "2026-01-01T00:00:00.000Z")
        self.assertEqual(second["createdCount"], 1)
        self.assertEqual(second["skippedExistingCount"], 1)
        self.assertEqual(second["skippedExisting"][0]["requestId"], first["requests"][0]["requestId"])
        self.assertEqual(second["requests"][0]["range"], {
            "start": "2026-01-03T00:00:00.000Z",
            "end": "2026-01-05T00:00:00.000Z",
        })

    def test_initial_load_resume_requeues_succeeded_chunk_without_s3_evidence(self):
        store = RedisBackfillStore(redis_client=MemoryRedis(), ttl_seconds=60)
        first = store.create_initial_load_requests(
            ["AAPL"],
            "1D",
            "2026-01-01T00:00:00.000Z",
            "2026-01-03T00:00:00.000Z",
            chunk_days=2,
            max_enqueued=1,
            max_backlog=10,
        )
        existing = store.get_status(first["requests"][0]["requestId"])
        store.update_status(existing, "succeeded", result={
            "source": "clickhouse",
            "skipped": True,
            "reason": "pre-fix success without S3 evidence",
        })

        second = store.create_initial_load_requests(
            ["AAPL"],
            "1D",
            "2026-01-01T00:00:00.000Z",
            "2026-01-03T00:00:00.000Z",
            chunk_days=2,
            max_enqueued=1,
            max_backlog=10,
        )

        self.assertEqual(second["createdCount"], 1)
        self.assertEqual(second["skippedExistingCount"], 0)
        self.assertIn(":force:", second["requests"][0]["requestId"])

    def test_initial_load_resume_skips_succeeded_chunk_with_s3_evidence(self):
        store = RedisBackfillStore(redis_client=MemoryRedis(), ttl_seconds=60)
        first = store.create_initial_load_requests(
            ["AAPL"],
            "1D",
            "2026-01-01T00:00:00.000Z",
            "2026-01-03T00:00:00.000Z",
            chunk_days=2,
            max_enqueued=1,
            max_backlog=10,
        )
        existing = store.get_status(first["requests"][0]["requestId"])
        store.update_status(existing, "succeeded", result={
            "source": "alpaca",
            "processedObjects": ["s3://bucket/market-data/final/candles/part.parquet"],
            "materializedRowCount": 1,
        })

        second = store.create_initial_load_requests(
            ["AAPL"],
            "1D",
            "2026-01-01T00:00:00.000Z",
            "2026-01-03T00:00:00.000Z",
            chunk_days=2,
            max_enqueued=1,
            max_backlog=10,
        )

        self.assertEqual(second["createdCount"], 0)
        self.assertEqual(second["skippedExistingCount"], 1)
        self.assertEqual(second["skippedExisting"][0]["requestId"], first["requests"][0]["requestId"])

    def test_initial_load_resume_skips_succeeded_empty_chunk_with_marker(self):
        store = RedisBackfillStore(redis_client=MemoryRedis(), ttl_seconds=60)
        first = store.create_initial_load_requests(
            ["PSKY"],
            "1D",
            "2023-06-30T00:00:00.000Z",
            "2024-07-04T00:00:00.000Z",
            chunk_days=370,
            max_enqueued=1,
            max_backlog=10,
        )
        existing = store.get_status(first["requests"][0]["requestId"])
        store.update_status(existing, "succeeded", result={
            "source": "alpaca-empty",
            "emptyRange": True,
            "emptyMarker": "s3://bucket/market-data/manifest/empty/candles/interval=1D/symbol=PSKY/request=chunk.json",
            "processedRowCount": 0,
        })

        second = store.create_initial_load_requests(
            ["PSKY"],
            "1D",
            "2023-06-30T00:00:00.000Z",
            "2024-07-04T00:00:00.000Z",
            chunk_days=370,
            max_enqueued=1,
            max_backlog=10,
        )

        self.assertEqual(second["createdCount"], 0)
        self.assertEqual(second["skippedExistingCount"], 1)

    def test_initial_load_resume_skips_retry_success_for_base_unavailable(self):
        store = RedisBackfillStore(redis_client=MemoryRedis(), ttl_seconds=60)
        first = store.create_initial_load_requests(
            ["PSKY"],
            "1D",
            "2023-06-30T00:00:00.000Z",
            "2024-07-04T00:00:00.000Z",
            chunk_days=370,
            max_enqueued=1,
            max_backlog=10,
        )
        base = store.get_status(first["requests"][0]["requestId"])
        store.update_status(base, "unavailable", error="Historical provider returned no bars.")
        retry_plan = store.create_initial_load_requests(
            ["PSKY"],
            "1D",
            "2023-06-30T00:00:00.000Z",
            "2024-07-04T00:00:00.000Z",
            chunk_days=370,
            max_enqueued=1,
            max_backlog=10,
        )
        retry = store.get_status(retry_plan["requests"][0]["requestId"])
        store.update_status(retry, "succeeded", result={
            "source": "alpaca-empty",
            "emptyRange": True,
            "emptyMarker": "s3://bucket/market-data/manifest/empty/candles/interval=1D/symbol=PSKY/request=retry.json",
        })

        third = store.create_initial_load_requests(
            ["PSKY"],
            "1D",
            "2023-06-30T00:00:00.000Z",
            "2024-07-04T00:00:00.000Z",
            chunk_days=370,
            max_enqueued=1,
            max_backlog=10,
        )

        self.assertEqual(retry_plan["createdCount"], 1)
        self.assertIn(":retry:", retry_plan["requests"][0]["requestId"])
        self.assertEqual(third["createdCount"], 0)
        self.assertEqual(third["skippedExistingCount"], 1)
        self.assertEqual(third["skippedExisting"][0]["requestId"], retry["requestId"])

    def test_initial_load_job_universe_symbol_marker_uses_configured_collection(self):
        module = load_initial_load_job_module()

        symbols, source = module.resolve_initial_load_symbols("universe")

        self.assertEqual(source, "configured_collection")
        self.assertGreaterEqual(len(symbols), 500)
        self.assertIn("AAPL", symbols)
        self.assertIn("BRK.B", symbols)

    def test_sp500_universe_excludes_known_invalid_fdxf_symbol(self):
        symbols = configured_collection_symbols()

        self.assertIn("FDX", symbols)
        self.assertNotIn("FDXF", symbols)

    def test_initial_load_dry_run_reports_plan_even_when_queue_metrics_unavailable(self):
        module = load_initial_load_job_module()

        dry_run = module.plan_initial_load(
            QueueMetricsUnavailableStore(),
            symbols=["AAPL"],
            intervals=["1D"],
            start="2026-01-01T00:00:00.000Z",
            end="2026-02-01T00:00:00.000Z",
            dry_run=True,
        )

        self.assertEqual(dry_run["failures"], 0)
        self.assertIsNone(dry_run["items"][0]["backlogBefore"])
        self.assertIn("redis unavailable", dry_run["items"][0]["queueMetricsError"])
        self.assertGreater(dry_run["items"][0]["estimate"]["totalRows"], 0)

    def test_backfill_store_force_requeues_same_symbol_interval_range(self):
        store = RedisBackfillStore(redis_client=MemoryRedis(), ttl_seconds=60)

        first, first_deduped = store.create_request(
            "INTC",
            "1D",
            start="2021-06-25T00:00:00.000Z",
            end="2026-06-25T00:00:00.000Z",
            mode="queue",
        )
        store.update_status(first, "succeeded")
        second, second_deduped = store.create_request(
            "INTC",
            "1D",
            start="2021-06-25T00:00:00.000Z",
            end="2026-06-25T00:00:00.000Z",
            mode="queue",
            force=True,
        )

        self.assertFalse(first_deduped)
        self.assertFalse(second_deduped)
        self.assertNotEqual(first["requestId"], second["requestId"])
        self.assertTrue(second["force"])
        self.assertEqual(store.latest_status("INTC", "1D")["requestId"], second["requestId"])

    def test_backfill_store_requeues_retryable_terminal_status(self):
        store = RedisBackfillStore(redis_client=MemoryRedis(), ttl_seconds=60)

        first, first_deduped = store.create_request(
            "NVDA",
            "1D",
            start="2021-06-25T00:00:00.000Z",
            end="2026-06-25T00:00:00.000Z",
            mode="queue",
        )
        store.update_status(first, "unavailable", error="Alpaca credentials are not configured.")
        second, second_deduped = store.create_request(
            "NVDA",
            "1D",
            start="2021-06-25T00:00:00.000Z",
            end="2026-06-25T00:00:00.000Z",
            mode="queue",
        )

        self.assertFalse(first_deduped)
        self.assertFalse(second_deduped)
        self.assertNotEqual(first["requestId"], second["requestId"])
        self.assertIn(":retry:", second["requestId"])
        self.assertEqual(store.latest_status("NVDA", "1D")["requestId"], second["requestId"])
        self.assertEqual(store.pop_queued_request_id(), first["requestId"])
        self.assertEqual(store.pop_queued_request_id(), second["requestId"])

    def test_backfill_runner_skips_when_clickhouse_already_covers_range(self):
        record = {
            "requestId": "backfill:AAPL:1m:test",
            "symbol": "AAPL",
            "interval": "1m",
            "range": {"start": "2026-06-25T13:30:00.000Z", "end": "2026-06-25T14:30:00.000Z"},
            "jobType": "gapfill",
            "sourcePreference": "coverage-first",
        }
        coverage = {
            "rowCount": 60,
            "availableFrom": "2026-06-25T13:30:00.000Z",
            "availableTo": "2026-06-25T14:30:00.000Z",
        }
        runner = BackfillRunner(
            s3=RecordingS3(),
            clickhouse_client=RecordingClickHouseClient(),
            coverage_provider=StaticCoverageProvider(coverage),
        )

        with mock.patch.dict(os.environ, {"S3_BUCKET": "bucket"}):
            with mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", side_effect=AssertionError("Alpaca should not be called")):
                result = runner._run(record)

        self.assertEqual(result["source"], "clickhouse")
        self.assertTrue(result["skipped"])
        self.assertEqual(result["coverage"]["rowCount"], 60)

    def test_backfill_runner_uses_processed_s3_before_alpaca(self):
        object_key = "market-data/final/candles/interval=1m/symbol=AAPL/year=2026/month=06/day=25/part-1.jsonl"
        body = json.dumps({
            "eventType": "CANDLE",
            "symbol": "AAPL",
            "interval": "1m",
            "timestamp": "2026-06-25T13:30:00.000Z",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10.5,
            "volume": 1000,
            "tradeCount": 10,
            "vwap": 10.25,
            "sourceEventId": "event-1",
            "createdAt": "2026-06-25T13:30:01.000Z",
        }, separators=(",", ":")) + "\n"
        record = {
            "requestId": "backfill:AAPL:1m:test",
            "symbol": "AAPL",
            "interval": "1m",
            "range": {"start": "2026-06-25T13:30:00.000Z", "end": "2026-06-25T14:30:00.000Z"},
            "jobType": "gapfill",
            "sourcePreference": "coverage-first",
        }
        client = RecordingClickHouseClient()
        runner = BackfillRunner(
            s3=S3ObjectStore({object_key: body}),
            clickhouse_client=client,
            coverage_provider=StaticCoverageProvider({"rowCount": 0, "availableFrom": None, "availableTo": None}),
        )

        with mock.patch.dict(os.environ, {"S3_BUCKET": "bucket", "S3_FINAL_PREFIX": "market-data/final"}):
            with mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", side_effect=AssertionError("Alpaca should not be called")):
                result = runner._run(record)

        self.assertEqual(result["source"], "s3-processed")
        self.assertEqual(result["materializedRowCount"], 1)
        self.assertEqual(result["processedObjects"], [f"s3://bucket/{object_key}"])
        self.assertEqual(client.inserts[0][0], "chart_candles")
        self.assertEqual(client.inserts[1][0], "load_audit")

    def test_initial_load_fetches_even_when_clickhouse_is_covered_to_populate_s3(self):
        record = {
            "requestId": "backfill:AAPL:1D:covered",
            "symbol": "AAPL",
            "interval": "1D",
            "range": {"start": "2026-06-25T00:00:00.000Z", "end": "2026-06-26T00:00:00.000Z"},
            "jobType": "initial_load",
            "sourcePreference": "coverage-first",
        }
        coverage = {
            "rowCount": 10,
            "availableFrom": "2026-06-01T00:00:00.000Z",
            "availableTo": "2026-06-30T00:00:00.000Z",
        }
        s3 = S3ObjectStore()
        runner = BackfillRunner(
            s3=s3,
            clickhouse_client=RecordingClickHouseClient(),
            coverage_provider=StaticCoverageProvider(coverage),
        )

        with mock.patch.dict(os.environ, {"S3_BUCKET": "bucket", "S3_PROCESSED_FORMAT": "jsonl"}):
            with mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", return_value=[
                alpaca_raw_bar("2026-06-25T00:00:00.000Z", open_price=10)
            ]) as fetch:
                result = runner._run(record)

        raw_keys = [key for key in s3.objects if key.startswith("market-data/raw/alpaca/")]
        self.assertEqual(result["source"], "alpaca")
        self.assertTrue(result["clickhouseCoveredBeforeLoad"])
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(any("part-000001-backfill_AAPL_1D_covered.jsonl" in key for key in raw_keys))

    def test_initial_load_empty_provider_writes_empty_marker(self):
        record = {
            "requestId": "backfill:PSKY:1D:empty",
            "symbol": "PSKY",
            "interval": "1D",
            "range": {"start": "2023-06-30T00:00:00.000Z", "end": "2024-07-04T00:00:00.000Z"},
            "jobType": "initial_load",
            "sourcePreference": "coverage-first",
        }
        s3 = S3ObjectStore()
        runner = BackfillRunner(s3=s3, clickhouse_client=RecordingClickHouseClient(), coverage_provider=StaticCoverageProvider({}))

        with mock.patch.dict(os.environ, {"S3_BUCKET": "bucket", "S3_MANIFEST_PREFIX": "market-data/manifest"}):
            with mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", return_value=[]):
                result = runner._run(record)

        marker_keys = [key for key in s3.objects if key.startswith("market-data/manifest/empty/candles/")]
        marker = json.loads(s3.get_object(Bucket="bucket", Key=marker_keys[0])["Body"].read().decode("utf-8"))
        self.assertEqual(result["source"], "alpaca-empty")
        self.assertTrue(result["emptyRange"])
        self.assertEqual(result["processedRowCount"], 0)
        self.assertEqual(len(marker_keys), 1)
        self.assertEqual(marker["symbol"], "PSKY")
        self.assertEqual(marker["range"]["start"], "2023-06-30T00:00:00.000Z")

    def test_processed_s3_flush_writes_manifest_for_candle_objects(self):
        s3 = S3ObjectStore()
        row = {
            "eventType": "CANDLE",
            "symbol": "AAPL",
            "interval": "1m",
            "timestamp": "2026-06-25T13:30:00.000Z",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10.5,
            "volume": 1000,
            "isClosed": True,
        }

        object_key = flush_buffer(
            s3,
            "bucket",
            "market-data/final/candles/interval=1m/symbol=AAPL/year=2026/month=06/day=25",
            [row],
            "jsonl",
            manifest_prefix="market-data/manifest",
        )
        keys = processed_candle_keys_from_manifest(
            s3,
            "bucket",
            "market-data/manifest",
            "AAPL",
            "1m",
            "2026-06-25T13:30:00.000Z",
            "2026-06-25T13:31:00.000Z",
        )

        self.assertEqual(keys, [object_key])
        manifest_keys = [key for key in s3.objects if key.startswith("market-data/manifest/")]
        self.assertEqual(len(manifest_keys), 1)

    def test_processed_s3_flush_can_write_compact_backfill_manifest(self):
        s3 = S3ObjectStore()
        rows = [
            {
                "eventType": "CANDLE",
                "symbol": "AAPL",
                "interval": "1D",
                "timestamp": "2026-06-25T00:00:00.000Z",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "volume": 1000,
                "isClosed": True,
            },
            {
                "eventType": "CANDLE",
                "symbol": "AAPL",
                "interval": "1D",
                "timestamp": "2026-06-26T00:00:00.000Z",
                "open": 11,
                "high": 12,
                "low": 10,
                "close": 11.5,
                "volume": 1200,
                "isClosed": True,
            },
        ]

        object_key = flush_buffer(
            s3,
            "bucket",
            "market-data/final/candles/interval=1D/symbol=AAPL/backfill_request=backfill_AAPL_1D_chunk",
            rows,
            "jsonl",
            manifest_prefix="market-data/manifest",
            manifest_layout="compact",
        )
        keys = processed_candle_keys_from_manifest(
            s3,
            "bucket",
            "market-data/manifest",
            "AAPL",
            "1D",
            "2026-06-26T00:00:00.000Z",
            "2026-06-27T00:00:00.000Z",
        )
        manifest_keys = [key for key in s3.objects if key.startswith("market-data/manifest/candles/")]

        self.assertEqual(keys, [object_key])
        self.assertEqual(len(manifest_keys), 1)
        self.assertIn("/objects/", manifest_keys[0])

    def test_raw_s3_archive_writes_manifest_for_replay_lookup(self):
        s3 = S3ObjectStore()
        rows = [alpaca_raw_bar("2026-06-25T13:30:00.000Z", open_price=10)]

        count = upload_raw_page_to_s3(
            s3,
            "bucket",
            "market-data/raw/alpaca",
            "bars",
            "sip",
            "2026-06-25T13:30:00.000Z",
            "2026-06-25T13:31:00.000Z",
            1,
            {"AAPL": rows},
            manifest_prefix="market-data/manifest",
        )
        keys = raw_keys_from_manifest(
            s3,
            "bucket",
            "market-data/manifest",
            "AAPL",
            ["bars"],
            "2026-06-25T13:30:00.000Z",
            "2026-06-25T13:31:00.000Z",
        )

        self.assertEqual(count, 1)
        self.assertEqual(len(keys), 1)
        self.assertIn("/source=alpaca/channel=bars/symbol=AAPL/", keys[0])
        manifest_keys = [key for key in s3.objects if key.startswith("market-data/manifest/raw/")]
        self.assertEqual(len(manifest_keys), 1)

    def test_raw_live_s3_archive_writes_replay_manifest(self):
        s3 = S3ObjectStore()
        envelope = build_raw_envelope(
            {"T": "b", "S": "AAPL", "t": "2026-06-25T13:30:00.000Z", "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1000},
            "sip",
        )
        archive_row = raw_archive_row(envelope)
        partition_key = raw_envelope_partition_key("market-data/raw/alpaca", archive_row)

        object_key = flush_raw_buffer(
            s3,
            "bucket",
            partition_key,
            [archive_row],
            manifest_prefix="market-data/manifest",
        )
        keys = raw_keys_from_manifest(
            s3,
            "bucket",
            "market-data/manifest",
            "AAPL",
            ["bars"],
            "2026-06-25T13:30:00.000Z",
            "2026-06-25T13:31:00.000Z",
        )

        self.assertEqual(keys, [object_key])
        self.assertIn("/source=alpaca/channel=bars/symbol=AAPL/", object_key)
        self.assertEqual(json.loads(s3.get_object(Bucket="bucket", Key=object_key)["Body"].read().decode("utf-8").splitlines()[0])["raw"]["T"], "b")

    def test_raw_live_s3_archive_normalizes_channel_and_status_time(self):
        envelope = {
            "source": "alpaca",
            "feed": "sip",
            "channel": "updatedBars",
            "symbol": "AAPL",
            "eventTime": None,
            "receivedAt": "2026-06-25T13:31:00.000Z",
            "sourceEventId": "event-1",
            "raw": {"T": "u", "S": "AAPL", "o": 1, "h": 2, "l": 1, "c": 2, "v": 100},
        }

        archive_row = raw_archive_row(envelope)
        partition_key = raw_envelope_partition_key("market-data/raw/alpaca", archive_row)

        self.assertEqual(archive_row["eventTime"], "2026-06-25T13:31:00.000Z")
        self.assertIn("/channel=updated-bars/", partition_key)

    def test_s3_time_based_flush_handles_low_volume_partitions(self):
        s3 = S3ObjectStore()
        buffers = {
            "market-data/raw/alpaca/source=alpaca/channel=bars/symbol=AAPL/year=2026/month=06/day=25": [
                raw_archive_row(build_raw_envelope({"T": "b", "S": "AAPL", "t": "2026-06-25T13:30:00.000Z"}, "sip"))
            ]
        }
        last_updated_at = {
            "market-data/raw/alpaca/source=alpaca/channel=bars/symbol=AAPL/year=2026/month=06/day=25": datetime(2026, 6, 25, 13, 30, tzinfo=timezone.utc)
        }

        flushed = flush_due_buffers(
            buffers,
            last_updated_at,
            lambda partition_key, rows: flush_raw_buffer(s3, "bucket", partition_key, rows, manifest_prefix="market-data/manifest"),
            flush_interval_seconds=60,
            now=datetime(2026, 6, 25, 13, 31, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(len(flushed), 1)
        self.assertFalse(buffers["market-data/raw/alpaca/source=alpaca/channel=bars/symbol=AAPL/year=2026/month=06/day=25"])
        self.assertTrue(any(key.startswith("market-data/raw/alpaca/") for key in s3.objects))

    def test_raw_s3_archive_shutdown_flushes_remaining_buffer(self):
        s3 = S3ObjectStore()
        envelope = build_raw_envelope(
            {"T": "b", "S": "AAPL", "t": "2026-06-25T13:30:00.000Z", "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1000},
            "sip",
        )
        consumer = OneMessageThenInterruptConsumer(envelope)

        run_raw_s3_archive_sink(
            consumer,
            s3,
            s3_bucket="bucket",
            raw_prefix="market-data/raw/alpaca",
            manifest_prefix="market-data/manifest",
            flush_count=100,
            flush_interval_seconds=3600,
            put_retry_sleep_seconds=0,
        )

        raw_keys = [key for key in s3.objects if key.startswith("market-data/raw/alpaca/")]
        manifest_keys = [key for key in s3.objects if key.startswith("market-data/manifest/raw/")]
        self.assertEqual(len(raw_keys), 1)
        self.assertEqual(len(manifest_keys), 1)

    def test_s3_flush_retries_transient_put_failure(self):
        s3 = FlakyS3ObjectStore(failures_before_success=1)
        row = {
            "eventType": "CANDLE",
            "symbol": "AAPL",
            "interval": "1m",
            "timestamp": "2026-06-25T13:30:00.000Z",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10.5,
            "volume": 1000,
            "isClosed": True,
        }

        object_key = flush_buffer(
            s3,
            "bucket",
            "market-data/final/candles/interval=1m/symbol=AAPL/year=2026/month=06/day=25",
            [row],
            "jsonl",
            max_attempts=2,
            retry_sleep_seconds=0,
        )

        self.assertEqual(s3.put_attempts, 2)
        self.assertIn(object_key, s3.objects)

    def test_raw_s3_archive_retries_manifest_put_failure(self):
        s3 = FailOnPutNumbersS3ObjectStore(fail_on={2})
        envelope = build_raw_envelope(
            {"T": "b", "S": "AAPL", "t": "2026-06-25T13:30:00.000Z", "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1000},
            "sip",
        )
        archive_row = raw_archive_row(envelope)
        partition_key = raw_envelope_partition_key("market-data/raw/alpaca", archive_row)

        object_key = flush_raw_buffer(
            s3,
            "bucket",
            partition_key,
            [archive_row],
            manifest_prefix="market-data/manifest",
            max_attempts=2,
            retry_sleep_seconds=0,
        )

        manifest_keys = [key for key in s3.objects if key.startswith("market-data/manifest/raw/")]
        self.assertEqual(s3.put_attempts, 3)
        self.assertIn(object_key, s3.objects)
        self.assertEqual(len(manifest_keys), 1)

    def test_raw_s3_archive_runtime_config_uses_raw_topics(self):
        config = raw_s3_archive_runtime_config({
            "KAFKA_BOOTSTRAP_SERVERS": "kafka:29092",
            "KAFKA_RAW_TOPIC_PREFIX": "market.raw",
            "KAFKA_RAW_S3_GROUP_ID": "raw-archive",
            "S3_BUCKET": "bucket",
            "S3_RAW_PREFIX": "market-data/raw/alpaca",
            "S3_RAW_FLUSH_COUNT": "10",
            "S3_RAW_FLUSH_INTERVAL_SECONDS": "15",
        })

        self.assertEqual(config["group_id"], "raw-archive")
        self.assertEqual(config["topics"][0], "market.raw.bars")
        self.assertIn("market.raw.cancel-errors", config["topics"])
        self.assertEqual(config["flush_count"], 10)
        self.assertEqual(config["flush_interval_seconds"], 15)

    def test_backfill_runner_prefers_manifested_processed_s3_objects(self):
        s3 = S3ObjectStore()
        source_row = {
            "eventType": "CANDLE",
            "symbol": "AAPL",
            "interval": "1m",
            "timestamp": "2026-06-25T13:30:00.000Z",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10.5,
            "volume": 1000,
            "tradeCount": 10,
            "vwap": 10.25,
            "sourceEventId": "event-1",
            "createdAt": "2026-06-25T13:30:01.000Z",
        }
        object_key = flush_buffer(
            s3,
            "bucket",
            "market-data/final/candles/interval=1m/symbol=AAPL/year=2026/month=06/day=25",
            [source_row],
            "jsonl",
            manifest_prefix="market-data/manifest",
        )
        record = {
            "requestId": "backfill:AAPL:1m:test",
            "symbol": "AAPL",
            "interval": "1m",
            "range": {"start": "2026-06-25T13:30:00.000Z", "end": "2026-06-25T13:31:00.000Z"},
            "jobType": "gapfill",
            "sourcePreference": "coverage-first",
        }
        client = RecordingClickHouseClient()
        runner = BackfillRunner(
            s3=s3,
            clickhouse_client=client,
            coverage_provider=StaticCoverageProvider({"rowCount": 0, "availableFrom": None, "availableTo": None}),
        )

        with mock.patch.dict(os.environ, {"S3_BUCKET": "bucket", "S3_FINAL_PREFIX": "market-data/final", "S3_MANIFEST_PREFIX": "market-data/manifest"}):
            with mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", side_effect=AssertionError("Alpaca should not be called")):
                result = runner._run(record)

        self.assertEqual(result["source"], "s3-processed")
        self.assertEqual(result["processedObjects"], [f"s3://bucket/{object_key}"])

    def test_backfill_runner_replay_repair_materializes_processed_s3(self):
        s3 = S3ObjectStore()
        source_row = {
            "eventType": "CANDLE",
            "symbol": "AAPL",
            "interval": "1m",
            "timestamp": "2026-06-25T13:30:00.000Z",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10.5,
            "volume": 1000,
            "sourceEventId": "event-1",
            "createdAt": "2026-06-25T13:30:01.000Z",
        }
        object_key = flush_buffer(
            s3,
            "bucket",
            "market-data/final/candles/interval=1m/symbol=AAPL/year=2026/month=06/day=25",
            [source_row],
            "jsonl",
            manifest_prefix="market-data/manifest",
        )
        record = {
            "requestId": "backfill:AAPL:1m:replay",
            "symbol": "AAPL",
            "interval": "1m",
            "range": {"start": "2026-06-25T13:30:00.000Z", "end": "2026-06-25T13:31:00.000Z"},
            "jobType": "replay_repair",
            "sourcePreference": "s3-only",
        }
        client = RecordingClickHouseClient()
        runner = BackfillRunner(s3=s3, clickhouse_client=client, coverage_provider=StaticCoverageProvider({}))

        with mock.patch.dict(os.environ, {"S3_BUCKET": "bucket", "S3_FINAL_PREFIX": "market-data/final", "S3_MANIFEST_PREFIX": "market-data/manifest"}):
            result = runner._run(record)

        self.assertEqual(result["source"], "s3-processed-replay")
        self.assertEqual(result["processedObjects"], [f"s3://bucket/{object_key}"])
        self.assertEqual(result["materializedRowCount"], 1)
        self.assertEqual(client.inserts[0][0], "chart_candles")

    def test_backfill_runner_replay_repair_materializes_raw_s3(self):
        s3 = S3ObjectStore()
        upload_raw_page_to_s3(
            s3,
            "bucket",
            "market-data/raw/alpaca",
            "bars",
            "sip",
            "2026-06-25T13:30:00.000Z",
            "2026-06-25T13:31:00.000Z",
            1,
            {"AAPL": [alpaca_raw_bar("2026-06-25T13:30:00.000Z", open_price=10)]},
            manifest_prefix="market-data/manifest",
        )
        record = {
            "requestId": "backfill:AAPL:1m:raw-replay",
            "symbol": "AAPL",
            "interval": "1m",
            "range": {"start": "2026-06-25T13:30:00.000Z", "end": "2026-06-25T13:31:00.000Z"},
            "jobType": "replay_repair",
            "sourcePreference": "s3-only",
        }
        client = RecordingClickHouseClient()
        runner = BackfillRunner(s3=s3, clickhouse_client=client, coverage_provider=StaticCoverageProvider({}))

        with mock.patch.dict(os.environ, {"S3_BUCKET": "bucket", "S3_RAW_PREFIX": "market-data/raw/alpaca", "S3_MANIFEST_PREFIX": "market-data/manifest"}):
            result = runner._run(record)

        self.assertEqual(result["source"], "s3-raw-replay")
        self.assertEqual(result["processedRowCount"], 1)
        self.assertEqual(result["materializedRowCount"], 1)
        self.assertEqual(client.inserts[0][1][0]["source"], "alpaca.bars")

    def test_backfill_runner_replay_repair_materializes_live_raw_archive_s3(self):
        s3 = S3ObjectStore()
        envelope = build_raw_envelope(
            {"T": "b", "S": "AAPL", "t": "2026-06-25T13:30:00.000Z", "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1000},
            "sip",
        )
        archive_row = raw_archive_row(envelope)
        object_key = flush_raw_buffer(
            s3,
            "bucket",
            raw_envelope_partition_key("market-data/raw/alpaca", archive_row),
            [archive_row],
            manifest_prefix="market-data/manifest",
        )
        record = {
            "requestId": "backfill:AAPL:1m:live-raw-replay",
            "symbol": "AAPL",
            "interval": "1m",
            "range": {"start": "2026-06-25T13:30:00.000Z", "end": "2026-06-25T13:31:00.000Z"},
            "jobType": "replay_repair",
            "sourcePreference": "s3-only",
        }
        client = RecordingClickHouseClient()
        runner = BackfillRunner(s3=s3, clickhouse_client=client, coverage_provider=StaticCoverageProvider({}))

        with mock.patch.dict(os.environ, {"S3_BUCKET": "bucket", "S3_RAW_PREFIX": "market-data/raw/alpaca", "S3_MANIFEST_PREFIX": "market-data/manifest"}):
            result = runner._run(record)

        self.assertEqual(result["source"], "s3-raw-replay")
        self.assertEqual(result["rawObjects"], [f"s3://bucket/{object_key}"])
        self.assertEqual(result["processedRowCount"], 1)
        self.assertEqual(result["materializedRowCount"], 1)
        self.assertEqual(client.inserts[0][1][0]["source_event_id"], envelope["sourceEventId"])

    def test_backfill_runner_correction_replay_uses_updated_bars_raw_s3(self):
        s3 = S3ObjectStore()
        upload_raw_page_to_s3(
            s3,
            "bucket",
            "market-data/raw/alpaca",
            "updated-bars",
            "sip",
            "2026-06-25T13:30:00.000Z",
            "2026-06-25T13:31:00.000Z",
            1,
            {"AAPL": [alpaca_raw_bar("2026-06-25T13:30:00.000Z", open_price=12)]},
            manifest_prefix="market-data/manifest",
        )
        record = {
            "requestId": "backfill:AAPL:1m:correction",
            "symbol": "AAPL",
            "interval": "1m",
            "range": {"start": "2026-06-25T13:30:00.000Z", "end": "2026-06-25T13:31:00.000Z"},
            "jobType": "correction_replay",
            "sourcePreference": "s3-only",
        }
        client = RecordingClickHouseClient()
        runner = BackfillRunner(s3=s3, clickhouse_client=client, coverage_provider=StaticCoverageProvider({}))

        with mock.patch.dict(os.environ, {"S3_BUCKET": "bucket", "S3_RAW_PREFIX": "market-data/raw/alpaca", "S3_MANIFEST_PREFIX": "market-data/manifest"}):
            result = runner._run(record)

        row = client.inserts[0][1][0]
        self.assertEqual(result["source"], "s3-raw-replay")
        self.assertEqual(row["source"], "alpaca.updatedBars")
        self.assertEqual(row["correction_type"], "UPDATED")

    def test_backfill_runner_replay_rejects_alpaca_only_source_preference(self):
        record = {
            "requestId": "backfill:AAPL:1m:replay",
            "symbol": "AAPL",
            "interval": "1m",
            "range": {"start": "2026-06-25T13:30:00.000Z", "end": "2026-06-25T13:31:00.000Z"},
            "jobType": "replay_repair",
            "sourcePreference": "alpaca-only",
        }
        runner = BackfillRunner(s3=S3ObjectStore(), clickhouse_client=RecordingClickHouseClient(), coverage_provider=StaticCoverageProvider({}))

        with mock.patch.dict(os.environ, {"S3_BUCKET": "bucket"}):
            with self.assertRaisesRegex(BackfillUnavailable, "cannot use sourcePreference=alpaca-only"):
                runner._run(record)

    def test_backfill_runner_s3_only_fails_without_processed_objects(self):
        record = {
            "requestId": "backfill:AAPL:1m:test",
            "symbol": "AAPL",
            "interval": "1m",
            "range": {"start": "2026-06-25T13:30:00.000Z", "end": "2026-06-25T14:30:00.000Z"},
            "jobType": "gapfill",
            "sourcePreference": "s3-only",
        }
        runner = BackfillRunner(
            s3=S3ObjectStore(),
            clickhouse_client=RecordingClickHouseClient(),
            coverage_provider=StaticCoverageProvider({"rowCount": 0, "availableFrom": None, "availableTo": None}),
        )

        with mock.patch.dict(os.environ, {"S3_BUCKET": "bucket", "S3_FINAL_PREFIX": "market-data/final"}):
            with self.assertRaisesRegex(BackfillUnavailable, "No processed S3 candle objects"):
                runner._run(record)

    def test_backfill_runner_fetches_only_detected_gap_ranges(self):
        record = {
            "requestId": "backfill:AAPL:1m:test",
            "symbol": "AAPL",
            "interval": "1m",
            "range": {"start": "2026-06-25T13:30:00.000Z", "end": "2026-06-25T13:35:00.000Z"},
            "jobType": "gapfill",
            "sourcePreference": "coverage-first",
        }
        coverage = {
            "rowCount": 3,
            "availableFrom": "2026-06-25T13:30:00.000Z",
            "availableTo": "2026-06-25T13:34:00.000Z",
        }
        provider = TimestampCoverageProvider(coverage, timestamps=[
            "2026-06-25T13:30:00.000Z",
            "2026-06-25T13:33:00.000Z",
            "2026-06-25T13:34:00.000Z",
        ])
        calls = []

        def fake_fetch(symbol, start, end, feed, timeframe):
            calls.append({"symbol": symbol, "start": start, "end": end, "timeframe": timeframe})
            return [
                alpaca_raw_bar("2026-06-25T13:31:00.000Z", open_price=10, index=1),
                alpaca_raw_bar("2026-06-25T13:32:00.000Z", open_price=11, index=2),
            ]

        runner = BackfillRunner(
            s3=S3ObjectStore(),
            clickhouse_client=RecordingClickHouseClient(),
            coverage_provider=provider,
        )

        with mock.patch.dict(os.environ, {"S3_BUCKET": "bucket", "S3_FINAL_PREFIX": "market-data/final", "S3_PROCESSED_FORMAT": "jsonl"}):
            with mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", side_effect=fake_fetch):
                result = runner._run(record)

        self.assertEqual(calls, [{
            "symbol": "AAPL",
            "start": "2026-06-25T13:31:00.000Z",
            "end": "2026-06-25T13:33:00.000Z",
            "timeframe": "1Min",
        }])
        self.assertEqual(result["source"], "alpaca")
        self.assertEqual(result["gapRanges"], [{"start": "2026-06-25T13:31:00.000Z", "end": "2026-06-25T13:33:00.000Z", "missingCount": 2}])

    def test_fetch_alpaca_bars_uses_raw_adjustment_and_retries_rate_limits(self):
        responses = [
            FakeHttpResponse(status_code=429, text="rate limited", headers={"Retry-After": "0"}),
            FakeHttpResponse(status_code=200, payload={"bars": {"AAPL": [alpaca_raw_bar("2026-06-25T13:30:00.000Z")]}}),
        ]
        calls = []

        def fake_get(_endpoint, headers, params, timeout):
            calls.append({"headers": headers, "params": dict(params), "timeout": timeout})
            return responses.pop(0)

        with mock.patch.dict(os.environ, {
            "HISTORICAL_ADJUSTMENT": "raw",
            "HISTORICAL_MAX_RETRIES": "2",
            "HISTORICAL_RETRY_SLEEP_SECONDS": "0",
            "HISTORICAL_RETRY_MAX_SLEEP_SECONDS": "0",
        }):
            with mock.patch("alfaka.common.secrets.load_alpaca_credentials", return_value=("key", "secret")):
                with mock.patch("requests.get", side_effect=fake_get):
                    rows = fetch_alpaca_bars(
                        "AAPL",
                        "2026-06-25T13:30:00.000Z",
                        "2026-06-25T13:31:00.000Z",
                        "sip",
                        "1Min",
                    )

        self.assertEqual(len(rows), 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1]["params"]["adjustment"], "raw")
        self.assertEqual(calls[-1]["params"]["timeframe"], "1Min")

    def test_clickhouse_provider_can_query_deduped_candle_timestamps(self):
        provider = RecordingClickHouseProviderForAggregation([
            {"timestamp": "2026-06-25T13:30:00.000Z"},
            {"timestamp": "2026-06-25T13:31:00.000Z"},
        ])

        timestamps = provider.candle_timestamps(
            "AAPL",
            "1m",
            "2026-06-25T13:30:00.000Z",
            "2026-06-25T13:32:00.000Z",
            limit=10,
        )

        self.assertEqual(timestamps, ["2026-06-25T13:30:00.000Z", "2026-06-25T13:31:00.000Z"])
        query, params = provider.queries[-1]
        self.assertIn("row_number() OVER", query)
        self.assertIn("event_time >= parseDateTimeBestEffort", query)
        self.assertEqual(params["from"], "2026-06-25T13:30:00.000Z")
        self.assertEqual(params["to"], "2026-06-25T13:32:00.000Z")

    def test_gapfill_ranges_coalesce_adjacent_missing_minutes(self):
        ranges = detect_gapfill_ranges(
            "2026-06-25T13:30:00.000Z",
            "2026-06-25T13:35:00.000Z",
            "1m",
            actual_timestamps=[
                "2026-06-25T13:30:00.000Z",
                "2026-06-25T13:33:00.000Z",
                "2026-06-25T13:34:00.000Z",
            ],
        )

        self.assertEqual(len(ranges), 1)
        self.assertEqual(ranges[0].start, "2026-06-25T13:31:00.000Z")
        self.assertEqual(ranges[0].end, "2026-06-25T13:33:00.000Z")
        self.assertEqual(ranges[0].missingCount, 2)

    def test_gapfill_ranges_skip_weekends_and_configured_closed_dates(self):
        calendar = TradingCalendar(closed_dates=frozenset({"2026-07-03"}))
        ranges = detect_gapfill_ranges(
            "2026-07-03T13:30:00.000Z",
            "2026-07-06T13:32:00.000Z",
            "1m",
            actual_timestamps=["2026-07-06T13:30:00.000Z"],
            calendar=calendar,
        )

        self.assertEqual(len(ranges), 1)
        self.assertEqual(ranges[0].start, "2026-07-06T13:31:00.000Z")
        self.assertEqual(ranges[0].end, "2026-07-06T13:32:00.000Z")
        self.assertEqual(ranges[0].missingCount, 1)

    def test_gapfill_ranges_honor_configured_early_close(self):
        calendar = TradingCalendar(early_closes={"2026-11-27": time(13, 0)})
        ranges = detect_gapfill_ranges(
            "2026-11-27T18:00:00.000Z",
            "2026-11-27T19:00:00.000Z",
            "1m",
            actual_timestamps=[],
            calendar=calendar,
        )

        self.assertEqual(ranges, [])

    def test_trading_calendar_from_environment_controls_gapfill_sessions(self):
        with mock.patch.dict(os.environ, {
            "MARKET_CALENDAR_PROVIDER": "configured-nyse",
            "MARKET_TIMEZONE": "America/New_York",
            "MARKET_OPEN_TIME": "09:30",
            "MARKET_CLOSE_TIME": "16:00",
            "MARKET_CLOSED_DATES": "2026-07-03",
            "MARKET_EARLY_CLOSES": "2026-11-27=13:00",
        }):
            calendar = TradingCalendar.from_environment()
            closed_ranges = detect_gapfill_ranges(
                "2026-07-03T13:30:00.000Z",
                "2026-07-06T13:31:00.000Z",
                "1m",
                actual_timestamps=["2026-07-06T13:30:00.000Z"],
                calendar=calendar,
            )
            early_close_ranges = detect_gapfill_ranges(
                "2026-11-27T18:00:00.000Z",
                "2026-11-27T19:00:00.000Z",
                "1m",
                actual_timestamps=[],
                calendar=calendar,
            )

        self.assertEqual(calendar.provider, "configured-nyse")
        self.assertEqual(calendar.closed_dates, frozenset({"2026-07-03"}))
        self.assertEqual(calendar.session_close_for(datetime(2026, 11, 27).date()), time(13, 0))
        self.assertEqual(closed_ranges, [])
        self.assertEqual(early_close_ranges, [])

    def test_default_backfill_range_is_minute_stable_for_auto_requests(self):
        with mock.patch.dict(os.environ, {"BACKFILL_DEFAULT_LOOKBACK_HOURS": "24"}):
            first = default_backfill_range(now="2026-06-25T14:30:11.123Z")
            second = default_backfill_range(now="2026-06-25T14:30:59.999Z")

        self.assertEqual(first, second)
        self.assertEqual(first.start, "2025-04-01T00:00:00.000Z")
        self.assertEqual(first.end, "2026-06-25T14:30:00.000Z")

    def test_default_backfill_range_uses_interval_groups(self):
        intraday = default_backfill_range(now="2026-06-25T14:30:11.123Z", interval="1m")
        derived_intraday = default_backfill_range(now="2026-06-25T14:30:11.123Z", interval="5m")
        daily = default_backfill_range(now="2026-06-25T14:30:11.123Z", interval="1D")

        self.assertEqual(intraday.start, "2025-04-01T00:00:00.000Z")
        self.assertEqual(derived_intraday.start, "2025-04-01T00:00:00.000Z")
        self.assertEqual(daily.start, "2023-06-26T14:30:00.000Z")

    def test_explicit_backfill_lookback_hours_is_still_supported_for_direct_calls(self):
        value = default_backfill_range(now="2026-06-25T14:30:11.123Z", interval="1D", lookback_hours=24)

        self.assertEqual(value.start, "2026-06-24T14:30:00.000Z")

    def test_chart_candle_limit_defaults_to_interval_visible_bars(self):
        self.assertEqual(candle_count_for_24h("1m"), 390)
        self.assertEqual(candle_count_for_24h("5m"), 390)
        self.assertEqual(candle_count_for_24h("10m"), 390)
        self.assertEqual(candle_count_for_24h("1d"), 250)
        self.assertEqual(candle_count_for_24h("1W"), 260)
        self.assertEqual(candle_count_for_24h("1M"), 120)
        self.assertEqual(historical_target_bars("1m"), 122850)
        self.assertEqual(historical_target_bars("1D"), 756)
        self.assertEqual(historical_target_bars("1M"), 36)
        self.assertEqual(candle_count_for_1y("1m"), 122850)
        self.assertEqual(resolve_candle_limit("1m", None), 390)
        self.assertEqual(resolve_candle_limit("1m", 9999), 9999)
        self.assertEqual(resolve_candle_limit("1m", 999999), 122850)
        self.assertEqual(resolve_candle_limit("1M", 999999), 120)
        self.assertEqual(redis_closed_candle_cap("1m"), 780)
        self.assertEqual(redis_closed_candle_cap("5m"), 156)
        self.assertEqual(redis_closed_candle_cap("10m"), 78)
        self.assertEqual(redis_closed_candle_cap("1D"), 756)
        self.assertEqual(redis_closed_candle_cap("1W"), 156)
        self.assertEqual(redis_closed_candle_cap("1M"), 36)

    def test_clickhouse_provider_uses_database_override(self):
        provider = ClickHouseMarketDataProvider(database="custom_market_data")

        self.assertEqual(provider.table("symbols"), "custom_market_data.symbols")
        with self.assertRaises(ValueError):
            provider.table("bad-table-name")

    def test_clickhouse_direct_candles_use_deterministic_latest_source(self):
        rows = [{
            "timestamp": "2026-06-25T13:30:00.000Z",
            "open": 1,
            "high": 2,
            "low": 1,
            "close": 2,
            "volume": 100,
            "isClosed": 1,
            "source": "alpaca.bars",
            "feed": "sip",
        }]
        provider = RecordingClickHouseProviderForAggregation(rows)

        candles = provider.candles("AAPL", "1m", 5)

        query = provider.queries[0][0]
        self.assertIn("row_number() OVER", query)
        self.assertIn("PARTITION BY symbol, if(interval = '1d', '1D', interval), event_time", query)
        self.assertIn("ORDER BY inserted_at DESC, ifNull(source_event_id, '') DESC", query)
        self.assertEqual(candles[-1]["timestamp"], "2026-06-25T13:30:00.000Z")

    def test_clickhouse_query_time_minute_aggregation_uses_1m_source_and_attaches_ma(self):
        rows = [
            {
                "timestamp": f"2026-06-25T13:3{index}:00.000Z",
                "open": index + 1,
                "high": index + 1,
                "low": index + 1,
                "close": index + 1,
                "volume": 100 + index,
                "isClosed": 1,
                "source": "alpaca.bars",
                "feed": "sip",
            }
            for index in range(5)
        ]
        provider = RecordingClickHouseProviderForAggregation(rows)

        candles = provider.candles("AAPL", "5m", 5)

        self.assertIn("AND interval = '1m'", provider.queries[0][0])
        self.assertIn("row_number() OVER", provider.queries[0][0])
        self.assertEqual(candles[-1]["interval"], "5m")
        self.assertEqual(candles[-1]["ma5"], 3.0)

    def test_clickhouse_query_time_weekly_monthly_aggregation_uses_daily_source(self):
        rows = [
            {
                "timestamp": f"2026-06-{20 + index:02d}T00:00:00.000Z",
                "open": index + 1,
                "high": index + 1,
                "low": index + 1,
                "close": index + 1,
                "volume": 100 + index,
                "isClosed": 1,
                "source": "alpaca.dailyBars",
                "feed": "sip",
            }
            for index in range(5)
        ]
        provider = RecordingClickHouseProviderForAggregation(rows)

        weekly = provider.candles("AAPL", "1W", 5)
        monthly = provider.candles("AAPL", "1M", 5)

        self.assertIn("AND interval IN ('1D', '1d')", provider.queries[0][0])
        self.assertIn("AND interval IN ('1D', '1d')", provider.queries[1][0])
        self.assertIn("row_number() OVER", provider.queries[0][0])
        self.assertIn("row_number() OVER", provider.queries[1][0])
        self.assertEqual(weekly[-1]["interval"], "1W")
        self.assertEqual(monthly[-1]["interval"], "1M")
        self.assertEqual(weekly[-1]["ma5"], 3.0)

    def test_monthly_bucket_candles_are_not_removed_when_month_starts_on_weekend(self):
        rows = [
            {
                "timestamp": "2022-01-01T00:00:00.000Z",
                "interval": "1M",
                "open": 1,
                "high": 2,
                "low": 1,
                "close": 2,
                "volume": 100,
                "isClosed": True,
            },
            {
                "timestamp": "2022-05-01T00:00:00.000Z",
                "interval": "1M",
                "open": 2,
                "high": 3,
                "low": 2,
                "close": 3,
                "volume": 110,
                "isClosed": True,
            },
            {
                "timestamp": "2022-06-01T00:00:00.000Z",
                "interval": "1M",
                "open": 3,
                "high": 4,
                "low": 3,
                "close": 4,
                "volume": 120,
                "isClosed": True,
            },
        ]
        provider = MarketDataProvider(
            redis_provider=FakeRedisProvider(),
            clickhouse_provider=FakeClickHouseProvider(rows),
        )

        payload = provider.candle_snapshot("AAPL", "1M", 10)

        self.assertEqual([candle["timestamp"] for candle in payload["candles"]], [
            "2022-01-01T00:00:00.000Z",
            "2022-05-01T00:00:00.000Z",
            "2022-06-01T00:00:00.000Z",
        ])

    def test_chart_snapshot_clamps_clickhouse_reads_to_target_floor(self):
        rows = [
            {
                "timestamp": "2022-01-01T00:00:00.000Z",
                "interval": "1M",
                "open": 1,
                "high": 2,
                "low": 1,
                "close": 2,
                "volume": 100,
                "isClosed": True,
            },
            {
                "timestamp": "2024-01-01T00:00:00.000Z",
                "interval": "1M",
                "open": 2,
                "high": 3,
                "low": 2,
                "close": 3,
                "volume": 110,
                "isClosed": True,
            },
            {
                "timestamp": "2026-06-01T00:00:00.000Z",
                "interval": "1M",
                "open": 3,
                "high": 4,
                "low": 3,
                "close": 4,
                "volume": 120,
                "isClosed": True,
            },
        ]
        clickhouse = RecordingRangeClickHouseProvider(rows)
        provider = MarketDataProvider(
            redis_provider=FakeRedisProvider(),
            clickhouse_provider=clickhouse,
        )

        with mock.patch("alfaka.serving.provider.target_range_from_for_interval", return_value="2023-07-01T00:00:00.000Z"):
            payload = provider.candle_snapshot("AAPL", "1M", 80)

        self.assertEqual(clickhouse.calls[-1]["from_time"], "2023-07-01T00:00:00.000Z")
        self.assertEqual([candle["timestamp"] for candle in payload["candles"]], [
            "2024-01-01T00:00:00.000Z",
            "2026-06-01T00:00:00.000Z",
        ])

    def test_chart_snapshot_clamps_explicit_before_pagination_to_target_floor(self):
        rows = [
            {
                "timestamp": "2022-01-01T00:00:00.000Z",
                "interval": "1M",
                "open": 1,
                "high": 2,
                "low": 1,
                "close": 2,
                "volume": 100,
                "isClosed": True,
            },
            {
                "timestamp": "2024-01-01T00:00:00.000Z",
                "interval": "1M",
                "open": 2,
                "high": 3,
                "low": 2,
                "close": 3,
                "volume": 110,
                "isClosed": True,
            },
        ]
        clickhouse = RecordingRangeClickHouseProvider(rows)
        provider = MarketDataProvider(
            redis_provider=FakeRedisProvider(),
            clickhouse_provider=clickhouse,
        )

        with mock.patch("alfaka.serving.provider.target_range_from_for_interval", return_value="2023-07-01T00:00:00.000Z"):
            payload = provider.candle_snapshot(
                "AAPL",
                "1M",
                80,
                before="2023-01-01T00:00:00.000Z",
                from_time="2020-01-01T00:00:00.000Z",
            )

        self.assertEqual(clickhouse.calls[-1]["from_time"], "2023-07-01T00:00:00.000Z")
        self.assertEqual(payload["candles"], [])

    def test_has_more_before_uses_target_floor_not_old_storage(self):
        self.assertFalse(has_more_before_target(
            "2023-07-01T00:00:00.000Z",
            "2021-06-01T00:00:00.000Z",
            "2023-07-01T05:00:00.000Z",
        ))
        self.assertTrue(has_more_before_target(
            "2023-08-01T00:00:00.000Z",
            "2021-06-01T00:00:00.000Z",
            "2023-07-01T05:00:00.000Z",
        ))

    def test_clickhouse_daily_snapshot_groups_daily_source_by_calendar_day(self):
        rows = [
            {
                "timestamp": f"2026-06-{20 + index:02d}T00:00:00.000Z",
                "open": index + 1,
                "high": index + 1,
                "low": index + 1,
                "close": index + 1,
                "volume": 100 + index,
                "isClosed": 1,
                "source": "alpaca.dailyBars",
                "feed": "sip",
            }
            for index in range(5)
        ]
        provider = RecordingClickHouseProviderForAggregation(rows)

        daily = provider.candles("AAPL", "1D", 5)

        self.assertIn("toStartOfDay(event_time) AS bucket", provider.queries[0][0])
        self.assertIn("AND interval IN ('1D', '1d')", provider.queries[0][0])
        self.assertIn("row_number() OVER", provider.queries[0][0])
        self.assertEqual(daily[-1]["interval"], "1D")
        self.assertEqual(daily[-1]["ma5"], 3.0)

    def test_clickhouse_hot_ranking_uses_deduped_minute_source(self):
        provider = RecordingClickHouseProviderForAggregation([])

        provider.hot_symbols_by_dollar_volume(["AAPL", "MSFT"])

        query = provider.queries[0][0]
        self.assertIn("row_number() OVER", query)
        self.assertIn("AND interval = '1m'", query)
        self.assertIn("latest_session_date", query)
        self.assertIn("event_time >= subtractDays(now(), {lookbackDays:UInt32})", query)
        self.assertEqual(provider.queries[0][1]["lookbackDays"], 14)

    def test_clickhouse_rows_preserve_candle_status_contract(self):
        candle_row = candle_to_clickhouse_row({
            "timestamp": "2026-06-25T10:15:00.000Z",
            "symbol": "AAPL",
            "interval": "1m",
            "open": 1,
            "high": 2,
            "low": 1,
            "close": 2,
            "volume": 100,
            "tradeCount": 12,
            "vwap": 1.5,
            "ma": {"ma5": 1.2, "ma20": 1.3, "ma60": 1.4},
            "isClosed": True,
            "correctionType": "UPDATED",
            "source": "alpaca.updatedBars",
            "feed": "sip",
            "sourceEventId": "source-1",
            "createdAt": "2026-06-25T10:15:01.000Z",
        })
        status_row = status_to_clickhouse_row({
            "eventTime": "2026-06-25T13:30:00.000Z",
            "symbol": "_MARKET",
            "statusType": "trading",
            "status": "active",
            "sourceEventId": "status-1",
            "raw": {"T": "s"},
        })

        self.assertEqual(candle_row["correction_type"], "UPDATED")
        self.assertEqual(candle_row["source_event_id"], "source-1")
        self.assertEqual(candle_row["ma60"], 1.4)
        self.assertIsNone(status_row["symbol"])
        self.assertEqual(status_row["source_event_id"], "status-1")

    def test_s3_partition_keys_and_storage_rows_follow_contract(self):
        final_prefix = "market-data/final"
        live_prefix = "market-data/live"
        candle_key = s3_partition_key(final_prefix, live_prefix, {
            "eventType": "CANDLE",
            "timestamp": "2026-01-02T10:15:00.000Z",
            "symbol": "AAPL",
            "interval": "1m",
        })
        live_key = s3_partition_key(final_prefix, live_prefix, {
            "eventType": "LIVE_CANDLE",
            "timestamp": "2026-01-02T10:15:00.000Z",
            "symbol": "AAPL",
            "interval": "1m",
        })
        profile_key = s3_partition_key(final_prefix, live_prefix, {
            "eventType": "VOLUME_PROFILE_BIN",
            "eventMinute": "2026-01-02T10:15:00.000Z",
            "symbol": "AAPL",
        })
        storage_row = normalize_storage_row({"ma": {"ma5": 1.0}, "raw": {"T": "s"}})

        self.assertEqual(candle_key, "market-data/final/candles/interval=1m/symbol=AAPL/year=2026/month=01/day=02")
        self.assertEqual(live_key, "market-data/live/candles/interval=1m/symbol=AAPL/year=2026/month=01/day=02")
        self.assertEqual(profile_key, "market-data/final/volume-profile-bins/timeBucket=1m/symbol=AAPL/year=2026/month=01/day=02")
        self.assertEqual(storage_row["ma5"], 1.0)
        self.assertIsNone(storage_row["ma20"])
        self.assertEqual(storage_row["raw"], "{\"T\":\"s\"}")

    def test_local_and_aws_topic_lists_cover_same_market_contract(self):
        root = Path(__file__).resolve().parents[3]
        required_topics = {
            "market.raw.bars",
            "market.raw.updated-bars",
            "market.raw.trades",
            "market.raw.daily-bars",
            "market.raw.statuses",
            "market.raw.quotes",
            "market.raw.corrections",
            "market.raw.cancel-errors",
            "market.ticks.v1",
            "market.candles.live.1m.v1",
            "market.candles.closed.v1",
            "market.status.v1",
            "market.volume-profile-bins.1m.v1",
            "orders.commands.v1",
            "broker.submit-results.v1",
            "broker.order-events.v1",
            "orders.dlq.v1",
        }
        aws_topics = {
            line.strip()
            for line in (root / "platform" / "kafka" / "topics.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }
        local_script = (root / "scripts" / "local" / "create-kafka-topics.sh").read_text(encoding="utf-8")

        self.assertTrue(required_topics.issubset(aws_topics))
        for topic in required_topics:
            self.assertIn(topic, local_script)

    def test_gap_fill_includes_same_timestamp_correction_after_cursor(self):
        original = {
            "timestamp": "2026-06-25T10:15:00.000Z",
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100.5,
            "volume": 1000,
            "isClosed": True,
            "sourceEventId": "alpaca/sip/bars/AAPL/2026-06-25T10:15:00.000Z/original",
        }
        duplicate_original = dict(original)
        correction = {
            **original,
            "close": 101.25,
            "volume": 1200,
            "correctionType": "UPDATED",
            "sourceEventId": "alpaca/sip/updatedBars/AAPL/2026-06-25T10:15:00.000Z/correction",
        }
        later = {
            **original,
            "timestamp": "2026-06-25T10:16:00.000Z",
            "sourceEventId": "alpaca/sip/bars/AAPL/2026-06-25T10:16:00.000Z/original",
        }
        provider = MarketDataProvider(
            redis_provider=FakeRedisProvider([duplicate_original, correction, later]),
            clickhouse_provider=FakeClickHouseProvider(),
        )

        candles = provider.candles_since_cursor("AAPL", "1m", cursor_for("AAPL", "1m", original))

        self.assertEqual([candle["sourceEventId"] for candle in candles], [
            correction["sourceEventId"],
            later["sourceEventId"],
        ])

    def test_status_source_event_id_without_event_time_uses_payload_identity(self):
        first_payload = {"T": "s", "S": "AAPL", "sc": "active", "st": "trading"}
        same_payload = {"T": "s", "S": "AAPL", "sc": "active", "st": "trading"}
        different_payload = {"T": "s", "S": "AAPL", "sc": "active", "st": "halted"}

        first_id = source_event_id(first_payload, "sip", "statuses", "AAPL", "2026-06-25T13:30:00.001Z")
        same_id = source_event_id(same_payload, "sip", "statuses", "AAPL", "2026-06-25T13:30:00.999Z")
        different_id = source_event_id(different_payload, "sip", "statuses", "AAPL", "2026-06-25T13:30:00.001Z")

        self.assertEqual(first_id, same_id)
        self.assertNotEqual(first_id, different_id)
        self.assertNotIn("/s/", first_id)

    def test_symbol_detail_rejects_unknown_symbol_without_registry_hit(self):
        registry = SymbolRegistry(
            clickhouse_provider=FakeClickHouseProvider(symbols={}),
            redis_provider=FakeRedisProvider(symbol_metadata={}),
        )

        with self.assertRaises(LookupError):
            registry.detail("ZZZZ")

    def test_sp500_universe_and_seed_symbols_are_separated(self):
        previous_universe = os.environ.get("ALPACA_UNIVERSE")
        previous = os.environ.get("ALPACA_SYMBOLS")
        previous_collection_source = os.environ.get("ALPACA_COLLECTION_SYMBOL_SOURCE")
        os.environ["ALPACA_UNIVERSE"] = "sp500"
        os.environ["ALPACA_SYMBOLS"] = "AAPL,MSFT,NVDA"
        os.environ["ALPACA_COLLECTION_SYMBOL_SOURCE"] = "universe"
        try:
            self.assertEqual(configured_seed_symbols(), ["AAPL", "MSFT", "NVDA"])
            universe = configured_universe_symbols()
            self.assertGreaterEqual(len(universe), 500)
            self.assertIn("AAPL", universe)
            self.assertIn("MSFT", universe)
            self.assertIn("BRK.B", universe)
            self.assertEqual(configured_collection_symbols(), universe)

            registry = SymbolRegistry(
                clickhouse_provider=FakeClickHouseProvider(symbols={}),
                redis_provider=FakeRedisProvider(symbol_metadata={}),
            )

            aapl_detail = registry.detail("AAPL")
            self.assertEqual(aapl_detail["symbol"], "AAPL")
            self.assertEqual(aapl_detail["name"], "Apple Inc.")
            self.assertEqual(aapl_detail["market"], "NASDAQ")
            brk_results = registry.search("brk", 5)
            self.assertEqual([item["symbol"] for item in brk_results], ["BRK.B"])
            empty_results = registry.search("", 5)
            self.assertEqual([item["symbol"] for item in empty_results], ["MMM", "AOS", "ABT", "ABBV", "ACN"])
        finally:
            if previous_universe is None:
                os.environ.pop("ALPACA_UNIVERSE", None)
            else:
                os.environ["ALPACA_UNIVERSE"] = previous_universe
            if previous is None:
                os.environ.pop("ALPACA_SYMBOLS", None)
            else:
                os.environ["ALPACA_SYMBOLS"] = previous
            if previous_collection_source is None:
                os.environ.pop("ALPACA_COLLECTION_SYMBOL_SOURCE", None)
            else:
                os.environ["ALPACA_COLLECTION_SYMBOL_SOURCE"] = previous_collection_source

    def test_symbol_search_does_not_fill_dropdown_with_outside_universe_matches(self):
        class SearchClickHouseProvider(FakeClickHouseProvider):
            def search_symbols(self, query, limit):
                return [
                    {"symbol": "AAPB", "name": "Non S&P leveraged Apple product"},
                    {"symbol": "TSLA", "name": "Tesla, Inc."},
                    {"symbol": "ADAMG", "name": "Non S&P test symbol"},
                ]

        previous_universe = os.environ.get("ALPACA_UNIVERSE")
        os.environ["ALPACA_UNIVERSE"] = "sp500"
        try:
            registry = SymbolRegistry(
                clickhouse_provider=SearchClickHouseProvider(symbols={}),
                redis_provider=FakeRedisProvider(symbol_metadata={}),
            )

            results = registry.search("tes", 10)
        finally:
            if previous_universe is None:
                os.environ.pop("ALPACA_UNIVERSE", None)
            else:
                os.environ["ALPACA_UNIVERSE"] = previous_universe

        self.assertEqual([item["symbol"] for item in results], ["TSLA"])

    def test_trade_subscription_plan_prioritizes_active_watchlist_then_hot(self):
        plan = resolve_trade_subscription_plan(
            active_symbols=["tsla", "AAPL"],
            watchlist_symbols=["MSFT", "AAPL"],
            hot_symbols=["NVDA", "MSFT", "BAD!"],
            max_symbols=4,
        )

        self.assertEqual(plan["symbols"], ["TSLA", "AAPL", "MSFT", "NVDA"])
        self.assertEqual(plan["tiersBySymbol"]["AAPL"], ["active", "watchlist"])
        self.assertEqual(plan["tiersBySymbol"]["MSFT"], ["watchlist", "hot"])
        self.assertEqual(plan["counts"]["resolved"], 4)

    def test_ingestor_reads_trade_symbols_from_active_watchlist_and_hot_snapshot(self):
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        redis.sadd(keys.active_symbols(), "AAPL")
        redis.set(keys.active_symbol("AAPL"), "1")
        redis.sadd(keys.watchlist_symbols(), "MSFT")
        redis.set(keys.hot_symbols_snapshot(), json.dumps({
            "symbols": [{"rank": 1, "symbol": "NVDA"}, {"rank": 2, "symbol": "AAPL"}]
        }))

        self.assertEqual(read_trade_subscription_symbols(redis), {"AAPL", "MSFT", "NVDA"})

    def test_hot_symbols_payload_ranks_by_dollar_volume(self):
        payload = build_hot_symbols_payload([
            {"symbol": "AAPL", "name": "Apple", "candles": [{"volume": 10, "vwap": 200}]},
            {"symbol": "MSFT", "name": "Microsoft", "candles": [{"volume": 100, "close": 10}]},
            {"symbol": "NVDA", "name": "Nvidia", "sessionDollarVolume": 3000},
        ], limit=2, as_of="2026-06-30T00:00:00.000Z")

        self.assertEqual([item["symbol"] for item in payload["symbols"]], ["NVDA", "AAPL"])
        self.assertEqual(payload["symbols"][0]["rank"], 1)
        self.assertEqual(dollar_volume_from_candle({"volume": 2, "vw": 5}), 10)

    def test_request_config_path_resolves_repo_relative_env(self):
        previous_config = os.environ.get("ALFAKA_REQUEST_CONFIG")
        previous_cwd = Path.cwd()
        repo_root = Path(__file__).resolve().parents[3]
        os.environ["ALFAKA_REQUEST_CONFIG"] = "systems/market-data/config/market-data-request.json"
        try:
            os.chdir(repo_root / "systems" / "api-server" / "pods" / "api-server" / "gops-backend")
            self.assertTrue(resolve_request_config_path().exists())
            config = load_request_config()
            self.assertEqual(config["defaultUniverse"], "sp500")
            self.assertEqual(config["collectionSymbolSource"], "universe")
            self.assertTrue((repo_root / config["universeRegistryPath"]).exists())
        finally:
            os.chdir(previous_cwd)
            if previous_config is None:
                os.environ.pop("ALFAKA_REQUEST_CONFIG", None)
            else:
                os.environ["ALFAKA_REQUEST_CONFIG"] = previous_config

    def test_market_data_images_copy_config_to_env_contract_path(self):
        dockerfiles = [
            "Dockerfile.gops-backend",
            "Dockerfile.gops-backfill-worker",
            "Dockerfile.gops-market-ingestor",
            "Dockerfile.gops-market-processor",
            "Dockerfile.gops-market-storage",
            "Dockerfile.worker",
        ]
        for dockerfile in dockerfiles:
            content = (REPO_ROOT / "infra" / "docker" / dockerfile).read_text(encoding="utf-8")
            self.assertIn("COPY systems/market-data/config ./systems/market-data/config", content)

    def test_clickhouse_array_query_parameter_serializes_symbols(self):
        self.assertEqual(clickhouse_param_value(["AAPL", "BRK.B", "O'Reilly"]), "['AAPL','BRK.B','O\\'Reilly']")

    def test_alpaca_seed_symbols_reject_universe_name(self):
        previous = os.environ.get("ALPACA_SYMBOLS")
        os.environ["ALPACA_SYMBOLS"] = "semiconductor-100"
        try:
            with self.assertRaises(ValueError):
                configured_seed_symbols()
        finally:
            if previous is None:
                os.environ.pop("ALPACA_SYMBOLS", None)
            else:
                os.environ["ALPACA_SYMBOLS"] = previous

    def test_provider_logs_clickhouse_fallbacks(self):
        provider = MarketDataProvider(
            redis_provider=FakeRedisProvider(
                candles=[{
                    "timestamp": "2026-06-25T10:16:00.000Z",
                    "open": 1,
                    "high": 2,
                    "low": 1,
                    "close": 2,
                    "volume": 100,
                    "sourceEventId": "event-later",
                }],
                profile_bins=[{"priceBinSize": 0.05, "source": "redis", "feed": "sip"}],
            ),
            clickhouse_provider=FailingClickHouseProvider(),
        )

        with self.assertLogs("alfaka.serving.provider", level="WARNING") as logs:
            candles = provider.candles_since_cursor("AAPL", "1m", "v1:AAPL:1m:2026-06-25T10:15:00.000Z:abc")
            profile = provider.volume_profile_bins("AAPL", "from", "to", "auto")

        self.assertEqual(candles[0]["sourceEventId"], "event-later")
        self.assertEqual(profile["source"], "redis")
        self.assertIn("ClickHouse candles_since failed", "\n".join(logs.output))
        self.assertIn("ClickHouse volume_profile_bins failed", "\n".join(logs.output))

    def test_empty_snapshot_does_not_emit_gap_fill_cursor(self):
        payload = snapshot("INTC", "1m", [])

        self.assertIsNone(payload["snapshotCursor"])
        self.assertEqual(payload["candles"], [])
        self.assertEqual(payload["dataStatus"], "empty")
        self.assertEqual(payload["backfillStatus"], "not_requested")
        self.assertFalse(payload["canBackfill"])

    def test_ready_snapshot_marks_data_status_ready(self):
        payload = snapshot("INTC", "1m", [{
            "timestamp": "2026-06-25T10:15:00.000Z",
            "open": 1,
            "high": 2,
            "low": 1,
            "close": 2,
            "volume": 100,
        }])

        self.assertEqual(payload["dataStatus"], "ready")
        self.assertEqual(payload["backfillStatus"], "not_requested")
        self.assertFalse(payload["canBackfill"])

    def test_provider_fills_missing_snapshot_moving_averages(self):
        candles = [
            {
                "timestamp": f"2026-06-25T10:0{minute}:00.000Z",
                "open": 10 + minute,
                "high": 11 + minute,
                "low": 9 + minute,
                "close": 10 + minute,
                "volume": 100,
                "isClosed": True,
            }
            for minute in range(5)
        ]
        provider = MarketDataProvider(
            redis_provider=FakeRedisProvider(),
            clickhouse_provider=FakeClickHouseProvider(candles=candles),
        )

        payload = provider.candle_snapshot("AAPL", "1m", 5)

        self.assertEqual(payload["candles"][-1]["ma5"], 12.0)

    def test_provider_uses_lookback_rows_for_snapshot_moving_averages(self):
        start = datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc)
        candles = [
            {
                "timestamp": (start + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "open": minute + 1,
                "high": minute + 2,
                "low": minute,
                "close": minute + 1,
                "volume": 100,
                "isClosed": True,
            }
            for minute in range(65)
        ]
        provider = MarketDataProvider(
            redis_provider=FakeRedisProvider(),
            clickhouse_provider=FakeClickHouseProvider(candles=candles),
        )

        payload = provider.candle_snapshot("AAPL", "1m", 5)

        self.assertEqual(len(payload["candles"]), 5)
        self.assertEqual(payload["candles"][-1]["ma60"], 35.5)

    def test_provider_does_not_let_stale_redis_recent_hide_newer_clickhouse_rows(self):
        start = datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc)
        clickhouse_candles = [
            {
                "timestamp": (start + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "open": minute + 1,
                "high": minute + 2,
                "low": minute,
                "close": minute + 1,
                "volume": 100,
                "isClosed": True,
            }
            for minute in range(85)
        ]
        redis_candles = clickhouse_candles[:80]
        provider = MarketDataProvider(
            redis_provider=FakeRedisProvider(candles=redis_candles),
            clickhouse_provider=FakeClickHouseProvider(candles=clickhouse_candles),
        )

        payload = provider.candle_snapshot("AAPL", "1m", 20)

        self.assertEqual(payload["newestTimestamp"], "2026-06-25T11:24:00.000Z")
        self.assertFalse(payload["hasMoreAfter"])
        self.assertEqual(payload["candles"][-1]["timestamp"], "2026-06-25T11:24:00.000Z")

    def test_provider_merges_redis_live_candle_into_snapshot_without_duplicate_bucket(self):
        closed = {
            "timestamp": "2026-06-25T10:15:00Z",
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100.5,
            "volume": 1000,
            "isClosed": True,
        }
        live = {
            **closed,
            "timestamp": "2026-06-25T10:15:00.000Z",
            "close": 101.25,
            "volume": 1200,
            "isClosed": False,
            "sourceInterval": "trades",
        }
        provider = MarketDataProvider(
            redis_provider=FakeRedisProvider(candles=[closed], live_candle=live),
            clickhouse_provider=FakeClickHouseProvider(candles=[closed]),
        )

        payload = provider.candle_snapshot("AAPL", "1m", 5)

        self.assertEqual(len(payload["candles"]), 1)
        self.assertEqual(payload["candles"][0]["timestamp"], "2026-06-25T10:15:00.000Z")
        self.assertEqual(payload["candles"][0]["close"], 101.25)
        self.assertFalse(payload["candles"][0]["isClosed"])

    def test_provider_filters_weekend_stock_candles_from_serving_snapshot(self):
        candles = [
            {
                "timestamp": "2026-06-27T10:00:00.000Z",
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 100,
                "isClosed": True,
            },
            {
                "timestamp": "2026-06-29T10:00:00.000Z",
                "open": 101,
                "high": 102,
                "low": 100,
                "close": 101,
                "volume": 100,
                "isClosed": True,
            },
        ]
        provider = MarketDataProvider(
            redis_provider=FakeRedisProvider(candles=candles),
            clickhouse_provider=FakeClickHouseProvider(candles=candles),
        )

        payload = provider.candle_snapshot("AAPL", "1m", 30)

        self.assertEqual([candle["timestamp"] for candle in payload["candles"]], ["2026-06-29T10:00:00.000Z"])

    def test_empty_cursor_does_not_trigger_clickhouse_timestamp_query(self):
        provider = MarketDataProvider(
            redis_provider=FakeRedisProvider(),
            clickhouse_provider=FailingClickHouseProvider(),
        )

        self.assertIsNone(timestamp_from_cursor("v1:INTC:1m:empty:00000000"))
        with self.assertNoLogs("alfaka.serving.provider", level="WARNING"):
            candles = provider.candles_since_cursor("INTC", "1m", "v1:INTC:1m:empty:00000000")

        self.assertEqual(candles, [])

    def test_backfill_raw_bar_reuses_processed_candle_and_clickhouse_contract(self):
        raw_bar = alpaca_raw_bar("2026-06-25T13:30:00.000Z")
        candle = raw_bar_to_processed_candle("INTC", raw_bar, feed="sip")
        row = candle_to_clickhouse_row(candle)

        self.assertEqual(candle["eventType"], "CANDLE")
        self.assertEqual(candle["symbol"], "INTC")
        self.assertEqual(candle["interval"], "1m")
        self.assertEqual(candle["source"], "alpaca.bars")
        self.assertIn("sourceEventId", candle)
        self.assertEqual(row["symbol"], "INTC")
        self.assertEqual(row["interval"], "1m")

    def test_daily_backfill_reuses_processed_candle_and_clickhouse_contract(self):
        raw_bar = alpaca_raw_bar("2026-06-25T00:00:00.000Z")
        candle = raw_bar_to_processed_candle("INTC", raw_bar, feed="sip", interval="1D")
        row = candle_to_clickhouse_row(candle)

        self.assertEqual(candle["interval"], "1D")
        self.assertEqual(candle["source"], "alpaca.dailyBars")
        self.assertEqual(row["interval"], "1D")

    def test_backfill_processed_candles_include_moving_averages(self):
        raw_bars = [
            alpaca_raw_bar(f"2026-06-25T13:3{index}:00.000Z", open_price=10 + index, index=index)
            for index in range(5)
        ]
        candles = raw_bars_to_processed_candles("INTC", raw_bars, feed="sip")
        row = candle_to_clickhouse_row(candles[-1])

        self.assertIn("ma5", candles[-1])
        self.assertIsNotNone(row["ma5"])

    def test_s3_materializer_normalizes_rows_and_records_load_audit(self):
        client = RecordingClickHouseClient()
        source_row = {
            "eventType": "CANDLE",
            "symbol": "INTC",
            "interval": "1m",
            "timestamp": "2026-06-25T13:30:00.000Z",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10.5,
            "volume": 100,
            "ma5": 10.1,
            "isClosed": True,
            "source": "alpaca.bars",
            "feed": "sip",
            "feedProfile": "sip",
            "marketSession": "regular",
            "sourceEventId": "event-1",
        }

        normalized = normalize_processed_candle_row(source_row)
        updated_duplicate = {**source_row, "close": 10.8, "sourceEventId": "event-2"}
        result = materialize_processed_rows(client, "s3://bucket/market-data/final/candles/part-1.jsonl", [source_row, updated_duplicate])

        self.assertEqual(normalized["ma"]["ma5"], 10.1)
        self.assertEqual(normalized["feedProfile"], "sip")
        self.assertEqual(normalized["marketSession"], "regular")
        self.assertEqual(result["rowCount"], 1)
        self.assertEqual(client.inserts[0][0], "chart_candles")
        self.assertEqual(client.inserts[0][1][0]["source_event_id"], "event-2")
        self.assertEqual(client.inserts[0][1][0]["feed_profile"], "sip")
        self.assertEqual(client.inserts[0][1][0]["market_session"], "regular")
        self.assertEqual(client.inserts[0][1][0]["close"], 10.8)
        self.assertEqual(client.inserts[1][0], "load_audit")
        self.assertEqual(client.inserts[1][1][0]["object_path"], "s3://bucket/market-data/final/candles/part-1.jsonl")

    def test_s3_materializer_retry_after_audit_failure_reinserts_same_candle_safely(self):
        client = FailingAuditClickHouseClient()
        source_row = {
            "eventType": "CANDLE",
            "symbol": "INTC",
            "interval": "1m",
            "timestamp": "2026-06-25T13:30:00.000Z",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10.5,
            "volume": 100,
            "isClosed": True,
            "source": "alpaca.bars",
            "feed": "sip",
            "sourceEventId": "event-1",
        }
        object_path = "s3://bucket/market-data/final/candles/part-retry.jsonl"

        with self.assertRaisesRegex(RuntimeError, "load audit unavailable"):
            materialize_processed_rows(client, object_path, [source_row])
        result = materialize_processed_rows(client, object_path, [source_row])

        candle_inserts = [rows for table, rows in client.inserts if table == "chart_candles"]
        audit_inserts = [rows for table, rows in client.inserts if table == "load_audit"]
        self.assertEqual(result["rowCount"], 1)
        self.assertEqual(len(candle_inserts), 2)
        self.assertEqual(candle_inserts[0], candle_inserts[1])
        self.assertEqual(candle_inserts[1][0]["source_event_id"], "event-1")
        self.assertEqual(len(audit_inserts), 1)
        self.assertEqual(audit_inserts[0][0]["object_path"], object_path)

    def test_clickhouse_loader_routes_news_articles_to_news_table(self):
        client = RecordingClickHouseClient()
        load_payload(client, {
            "eventType": "NEWS_ARTICLE",
            "symbol": "NVDA",
            "articleId": "news-1",
            "headline": "NVIDIA news",
            "summary": "News summary",
            "publishedAt": "2026-06-29T01:02:03.000Z",
            "source": "alpaca",
            "raw": {"id": "news-1"},
        })

        self.assertEqual(client.inserts[0][0], "news_articles")
        self.assertEqual(client.inserts[0][1][0]["article_id"], "news-1")

    def test_storage_boundaries_skip_invalid_weekend_stock_candles(self):
        client = RecordingClickHouseClient()
        weekend_row = {
            "eventType": "CANDLE",
            "symbol": "NVDA",
            "interval": "1D",
            "timestamp": "2026-06-27T00:00:00.000Z",
            "open": 1,
            "high": 2,
            "low": 1,
            "close": 2,
            "volume": 100,
            "isClosed": True,
            "source": "alpaca.dailyBars",
            "feed": "sip",
            "sourceEventId": "weekend-row",
        }

        load_payload(client, weekend_row)
        result = materialize_processed_rows(client, "s3://bucket/market-data/final/candles/weekend.jsonl", [weekend_row])

        self.assertEqual(result["rowCount"], 0)
        self.assertEqual(result["skippedInvalidRowCount"], 1)
        self.assertFalse(any(table == "chart_candles" for table, _rows in client.inserts))

    def test_storage_boundaries_allow_monthly_bucket_on_weekend_month_start(self):
        client = RecordingClickHouseClient()
        monthly_row = {
            "eventType": "CANDLE",
            "symbol": "NVDA",
            "interval": "1M",
            "timestamp": "2022-01-01T00:00:00.000Z",
            "open": 1,
            "high": 2,
            "low": 1,
            "close": 2,
            "volume": 100,
            "isClosed": True,
            "source": "alpaca.dailyBars",
            "feed": "sip",
            "sourceEventId": "monthly-row",
        }

        load_payload(client, monthly_row)
        result = materialize_processed_rows(client, "s3://bucket/market-data/final/candles/monthly.jsonl", [monthly_row])

        self.assertEqual(result["rowCount"], 1)
        self.assertEqual(result["skippedInvalidRowCount"], 0)
        self.assertTrue(any(table == "chart_candles" for table, _rows in client.inserts))

    def test_s3_materializer_detects_jsonl_and_parquet(self):
        self.assertEqual(detect_s3_object_format("market-data/final/candles/part-1.jsonl"), "jsonl")
        self.assertEqual(detect_s3_object_format("market-data/final/candles/part-1.ndjson"), "jsonl")
        self.assertEqual(detect_s3_object_format("market-data/final/candles/part-1.parquet"), "parquet")

    def test_s3_materializer_lists_processed_final_objects(self):
        keys = list_s3_objects(FakeS3WithPaginator(), "bucket", "market-data/final/candles")

        self.assertEqual(keys, [
            "market-data/final/candles/part-1.jsonl",
            "market-data/final/candles/part-2.parquet",
        ])

    def test_s3_materializer_can_target_explicit_keys_for_smoke(self):
        s3 = S3ObjectStore({
            "market-data/final/candles/part-a.jsonl": "",
            "market-data/final/candles/part-b.jsonl": "",
        })

        with mock.patch.dict(os.environ, {"S3_MATERIALIZE_KEYS": "market-data/final/candles/part-b.jsonl"}, clear=False):
            keys = materialize_keys_from_env(s3, "bucket", "market-data/final/candles")

        self.assertEqual(keys, ["market-data/final/candles/part-b.jsonl"])

    def test_s3_materializer_reads_parquet_processed_candle_objects(self):
        s3 = S3ObjectStore()
        client = RecordingClickHouseClient()
        row = {
            "eventType": "CANDLE",
            "symbol": "AAPL",
            "interval": "1D",
            "timestamp": "2026-06-25T00:00:00.000Z",
            "open": 190,
            "high": 195,
            "low": 189,
            "close": 194,
            "volume": 1000000,
            "isClosed": True,
            "source": "alpaca.dailyBars",
            "feed": "sip",
            "sourceEventId": "daily-1",
        }

        object_key = flush_buffer(
            s3,
            "bucket",
            "market-data/final/candles/interval=1D/symbol=AAPL/year=2026/month=06/day=25",
            [row],
            "parquet",
            manifest_prefix="market-data/manifest",
        )
        rows = read_s3_rows(s3, "bucket", object_key)
        result = materialize_s3_processed_objects(client, s3, "bucket", [object_key], source_name="smoke")

        self.assertEqual(rows[0]["symbol"], "AAPL")
        self.assertEqual(result["rowCount"], 1)
        self.assertEqual(client.inserts[0][0], "chart_candles")
        self.assertEqual(client.inserts[1][1][0]["source_name"], "smoke")


if __name__ == "__main__":
    unittest.main()
