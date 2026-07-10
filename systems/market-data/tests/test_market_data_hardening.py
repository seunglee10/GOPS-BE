import json
import asyncio
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
sys.path.insert(0, str(REPO_ROOT / "systems" / "market-data" / "shared"))
sys.path.insert(0, str(REPO_ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend"))

from alfaka.alpaca.subscription import (
    build_subscription_request,
    configured_collection_symbols,
    configured_seed_symbols,
    configured_universe_symbols,
    load_request_config,
    load_symbols_and_channels,
    resolve_request_config_path,
)
from alfaka.alpaca.feed_profiles import feed_profile_active_for_session, market_session_for_timestamp, resolve_feed_profile, visible_extended_session_windows
from alfaka.alpaca.trade_tiers import resolve_trade_subscription_plan
from alfaka.alpaca.websocket_collector import (
    classify_alpaca_error,
    publish_worker_count_from_env,
    read_realtime_subscription_symbols_by_channel,
    read_trade_subscription_symbols,
    summarize_subscription_request,
)
from alfaka.alpaca.assets import asset_to_symbol_metadata
from alfaka.alpaca.news import build_news_events, iter_alpaca_news_pages
from alfaka.common.kafka_io import create_json_consumer, create_json_producer, producer_options_from_env
from alfaka.common.market_messages import build_raw_envelope, raw_topic_name, source_event_id
from alfaka.common.redis_keys import RedisKeyBuilder
from alfaka.common.runtime_health import read_component_health, write_component_health
from alfaka.common.runtime_config import has_placeholder_value, validate_required_values
from alfaka.common.s3_client import create_s3_client
from alfaka.common.secrets import load_alpaca_credentials, resolve_alpaca_credential_source
from alfaka.backfill.runner import BackfillRunner, BackfillUnavailable, fetch_alpaca_bars, raw_bar_to_processed_candle, raw_bars_to_processed_candles, repair_daily_bar_outliers
from alfaka.backfill.gapfill import TradingCalendar, detect_gapfill_ranges
from alfaka.backfill.status import RedisBackfillStore, default_backfill_range
from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider, clickhouse_param_value
from alfaka.serving.cursors import timestamp_from_cursor
from alfaka.serving.dto import cursor_for, market_status_event, snapshot, websocket_event
from alfaka.serving.hot_symbols import build_hot_symbols_payload, dollar_volume_from_candle
from alfaka.serving.intervals import candle_count_for_1y, candle_count_for_24h, historical_target_bars, redis_closed_candle_cap, resolve_candle_limit
from alfaka.serving.provider import MarketDataProvider, filter_stock_chart_candles, has_more_before_target, merge_candles, target_range_from_for_interval
from alfaka.serving.redis_provider import RedisMarketDataProvider
from alfaka.serving.news_hot_cache import (
    company_daily_summary_coverage_valid,
    read_company_daily_summaries_from_redis,
    read_company_daily_summary_coverage_from_redis,
    read_localized_news_from_redis,
    write_company_daily_summaries_to_redis,
    write_localized_news_to_redis,
)
from alfaka.serving.symbol_registry import SymbolRegistry
from alfaka.realtime.feed_control import active_feed_profile_for
from alfaka.storage.clickhouse_loader import (
    ClickHouseHttpClient,
    candle_to_clickhouse_row,
    clickhouse_param_value as storage_clickhouse_param_value,
    clickhouse_topics_from_env,
    flush_clickhouse_buffer,
    load_payload,
    load_payload_batch,
    market_event_to_clickhouse_row,
    news_to_clickhouse_row,
    status_to_clickhouse_row,
    symbol_to_clickhouse_row,
    trade_to_clickhouse_row,
    should_ensure_schema_on_start,
)
from alfaka.storage.candle_validation import invalid_candle_reason
from alfaka.storage.news_daily_summary import attach_price_changes_to_daily_summaries, build_daily_summary_record, clickhouse_row_to_daily_summary, daily_summary_to_clickhouse_row
from alfaka.storage.news_intelligence import build_news_intelligence_record, news_intelligence_to_clickhouse_row
from alfaka.storage.s3_materializer import (
    detect_s3_object_format,
    list_s3_objects,
    materialize_keys_from_env,
    materialize_processed_rows,
    materialize_s3_processed_objects,
    normalize_processed_candle_row,
    read_s3_rows,
)
from alfaka.storage.processed_s3_sink import flush_buffer, flush_due_buffers, normalize_storage_row, processed_topics_from_env, run_processed_s3_sink, s3_partition_key
from alfaka.storage.raw_s3_archive_sink import flush_raw_buffer, raw_archive_row, raw_envelope_partition_key, raw_s3_archive_runtime_config, run_raw_s3_archive_sink
from alfaka.storage.raw_s3_archive import raw_chunk_partition_key, raw_object_suffix, raw_partition_key, upload_raw_page_to_s3
from alfaka.storage.news_s3_archive import (
    canonical_news_article_key,
    news_symbol_index_key,
    upload_canonical_news_article_to_s3,
    write_news_symbol_index_to_s3,
)
from alfaka.storage.s3_manifest import processed_candle_keys_from_manifest, raw_keys_from_manifest
from alfaka.streaming.processor import ProcessorState, flush_ready_closed_candles, process_raw_envelope, processor_runtime_config, recover_processor_state_from_clickhouse, recover_processor_state_from_redis, run_stream_processor, write_closed_candle_to_redis, write_live_candle_to_redis, write_trade_to_redis
from alfaka.streaming.transforms import (
    CandleAggregator,
    VolumeProfileBinBuilder,
    normalize_bar,
    normalize_status,
    normalize_trade,
)
from alfaka.tools import live_path_trace
from alfaka.tools.canonical_candle_audit import canonical_candle_audit_query
from app.market_data.realtime.active_symbols import ActiveSymbolManager
from alfaka.realtime.subscription_cohorts import RealtimeSubscriptionCohortService


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


def canonical_candle_fields(price_adjustment="split"):
    return {
        "priceAdjustment": price_adjustment,
        "canonicalVersion": "v2",
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
        self.executions = []

    def insert_json_each_row(self, table, rows):
        self.inserts.append((table, list(rows)))

    def execute(self, query, parameters=None):
        self.executions.append((query, parameters or {}))


class QueryRecordingClickHouseClient(RecordingClickHouseClient):
    database = "market_data"

    def __init__(self, query_rows):
        super().__init__()
        self.query_rows = query_rows
        self.queries = []

    def query_json_each_row(self, query, parameters=None):
        self.queries.append((query, parameters or {}))
        if int((parameters or {}).get("offset") or 0) > 0:
            return []
        return list(self.query_rows)[: int((parameters or {}).get("limit") or len(self.query_rows))]


class NewsRebuildSchemaAwareClickHouseClient(RecordingClickHouseClient):
    database = "market_data"

    def __init__(self, columns, partitions, rows_by_partition):
        super().__init__()
        self.columns = list(columns)
        self.partitions = list(partitions)
        self.rows_by_partition = dict(rows_by_partition)
        self.queries = []

    def query_json_each_row(self, query, parameters=None):
        parameters = parameters or {}
        self.queries.append((query, parameters))
        if "FROM system.columns" in query:
            return [{"name": name} for name in self.columns]
        if "GROUP BY symbol, locale" in query:
            return list(self.partitions)
        key = (parameters.get("symbol"), parameters.get("locale"))
        return list(self.rows_by_partition.get(key, []))[: int(parameters.get("limit") or 0)]


class SequentialQueryClickHouseClient(RecordingClickHouseClient):
    database = "market_data"

    def __init__(self, query_batches):
        super().__init__()
        self.query_batches = [list(batch) for batch in query_batches]
        self.queries = []

    def query_json_each_row(self, query, parameters=None):
        self.queries.append((query, parameters or {}))
        if not self.query_batches:
            return []
        return self.query_batches.pop(0)


class AuditAwareClickHouseClient(RecordingClickHouseClient):
    def __init__(self, materialized_paths=None):
        super().__init__()
        self.materialized_paths = set(materialized_paths or [])
        self.audit_checks = []

    def s3_object_already_materialized(self, object_path):
        self.audit_checks.append(object_path)
        return object_path in self.materialized_paths


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
    if not module_path.exists():
        raise unittest.SkipTest("initial-load job was removed; chart history now uses API on-demand fill.")
    spec = importlib.util.spec_from_file_location("initial_load_job", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_news_intelligence_worker_module():
    module_path = REPO_ROOT / "systems/market-data/pods/news-intelligence-worker/main.py"
    spec = importlib.util.spec_from_file_location("news_intelligence_worker", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_news_daily_summary_worker_module():
    module_path = REPO_ROOT / "systems/market-data/pods/news-daily-summary-worker/main.py"
    spec = importlib.util.spec_from_file_location("news_daily_summary_worker", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_news_intelligence_rebuild_module():
    module_path = REPO_ROOT / "systems/market-data/jobs/news-intelligence-rebuild/main.py"
    spec = importlib.util.spec_from_file_location("news_intelligence_rebuild", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_news_backfill_module():
    module_path = REPO_ROOT / "systems/market-data/jobs/news-backfill/main.py"
    spec = importlib.util.spec_from_file_location("news_backfill", module_path)
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
        self.expirations = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def setex(self, key, seconds, value):
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def exists(self, key):
        return key in self.values or key in self.hashes or key in self.zsets or key in self.sets

    def hset(self, key, mapping=None, **kwargs):
        values = dict(mapping or kwargs)
        self.hashes.setdefault(key, {}).update(values)
        return len(values)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def delete(self, key):
        self.values.pop(key, None)
        self.hashes.pop(key, None)
        self.zsets.pop(key, None)
        self.sets.pop(key, None)
        return 1

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

    def zrevrange(self, key, start, end):
        ordered = list(reversed([member for member, _score in sorted(self.zsets.get(key, {}).items(), key=lambda item: (item[1], item[0]))]))
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

    def srem(self, key, value):
        self.sets.setdefault(key, set()).discard(value)
        return 1

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def expire(self, key, seconds):
        self.expirations[key] = seconds
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
        self.flush_count = 0

    def send(self, topic, key, value):
        self.sent.append({"topic": topic, "key": key, "value": value})

    def flush(self):
        self.flush_count += 1


class FailingProducer:
    def __init__(self, error):
        self.error = error
        self.sent = []

    def send(self, topic, key, value):
        self.sent.append({"topic": topic, "key": key, "value": value})
        raise RuntimeError(self.error)


def feed_fanout_messages(producer, redis, keys, state, topics, interval="1m"):
    fanout = [
        sent["value"]
        for sent in list(producer.sent)
        if sent["topic"] == f"market.realtime.ticks.to.{interval.lower()}.v1"
    ]
    results = []
    for payload in fanout:
        results.append(process_raw_envelope(payload, producer, redis, keys, state, topics))
    return results


def mermaid_processor_topics():
    return {
        "trades": "market.layer.trades.v1",
        "quotes": "market.layer.quotes.v1",
        "events": "market.layer.events.v1",
        "tick_fanout": {
            "1m": "market.realtime.ticks.to.1m.v1",
            "5m": "market.realtime.ticks.to.5m.v1",
            "10m": "market.realtime.ticks.to.10m.v1",
            "1D": "market.realtime.ticks.to.1d.v1",
            "1W": "market.realtime.ticks.to.1w.v1",
            "1M": "market.realtime.ticks.to.1mo.v1",
        },
        "live_candles": "market.layer.candles.live.v1",
        "closed_candles": "market.layer.candles.closed.v1",
    }


class RecordingKafkaConsumer:
    calls = []

    def __init__(self, *topics, **kwargs):
        self.topics = topics
        self.kwargs = kwargs
        RecordingKafkaConsumer.calls.append({"topics": topics, "kwargs": kwargs})


class OneBatchKafkaConsumer:
    def __init__(self, value):
        self.value = value
        self.poll_count = 0
        self.commits = 0

    def poll(self, timeout_ms=1000):
        self.poll_count += 1
        if self.poll_count == 1:
            return {None: [types.SimpleNamespace(value=self.value)]}
        raise KeyboardInterrupt

    def commit(self):
        self.commits += 1


class FakeWebSocket:
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def recv(self):
        frame = self.frames.pop(0)
        if frame == "timeout":
            raise asyncio.TimeoutError()
        if frame == "stop":
            raise RuntimeError("stop")
        return json.dumps([frame])


class FakeWebSocketConnect:
    def __init__(self, websocket):
        self.websocket = websocket

    async def __aenter__(self):
        return self.websocket

    async def __aexit__(self, exc_type, exc, tb):
        return False


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
                "ALPACA_COLLECTION_SYMBOLS",
                "ALPACA_COLLECTION_SYMBOL_SOURCE",
                "ALPACA_SYMBOLS",
                "ALPACA_CRYPTO_SYMBOLS",
                "BACKFILL_INITIAL_LOAD_1M_MIN_START",
            )
        }
        os.environ["ALFAKA_REQUEST_CONFIG"] = "systems/market-data/config/market-data-request.json"
        os.environ["ALPACA_UNIVERSE"] = ""
        os.environ["ALPACA_UNIVERSE_REGISTRY_PATH"] = ""
        os.environ["ALPACA_COLLECTION_SYMBOLS"] = ""
        os.environ["ALPACA_COLLECTION_SYMBOL_SOURCE"] = "defaultSymbols"
        os.environ["ALPACA_SYMBOLS"] = ""
        os.environ["ALPACA_CRYPTO_SYMBOLS"] = "BTCUSD"
        os.environ["BACKFILL_INITIAL_LOAD_1M_MIN_START"] = "2020-07-01T00:00:00Z"

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
        self.assertEqual(raw_topic_name("market.input", "t"), "market.input.realtime.trades.v1")
        self.assertIn("sourceEventId", envelope)
        self.assertEqual(envelope["sourceEventId"], "alpaca/sip/trades/AAPL/123/2026-06-25T10:15:20.100Z")

    def test_feed_profiles_and_market_session_contract(self):
        sip = resolve_feed_profile({"ALPACA_FEED_PROFILE": "sip"})
        self.assertEqual(sip.websocket_url, "wss://stream.data.alpaca.markets/v2/sip")
        self.assertEqual(sip.sessions, ("pre", "regular", "after"))
        self.assertTrue(feed_profile_active_for_session(sip, "regular"))
        self.assertFalse(feed_profile_active_for_session(sip, "overnight"))

        with self.assertRaises(ValueError):
            resolve_feed_profile({"ALPACA_FEED": "iex"})

        boats = resolve_feed_profile({"ALPACA_FEED_PROFILE": "overnight"})
        self.assertEqual(boats.feed, "boats")
        self.assertEqual(boats.sessions, ("overnight",))
        self.assertEqual(boats.websocket_url, "wss://stream.data.alpaca.markets/v1beta1/overnight")
        boats_primary = resolve_feed_profile({"ALPACA_FEED_PROFILE": "boats"})
        self.assertEqual(boats_primary.websocket_url, "wss://stream.data.alpaca.markets/v1beta1/boats")
        self.assertTrue(feed_profile_active_for_session(boats_primary, "overnight"))
        self.assertFalse(feed_profile_active_for_session(boats_primary, "regular"))
        self.assertEqual(market_session_for_timestamp("2026-06-29T08:30:00.000Z"), "pre")
        self.assertEqual(market_session_for_timestamp("2026-06-29T14:00:00.000Z"), "regular")
        self.assertEqual(market_session_for_timestamp("2026-06-29T21:00:00.000Z"), "after")
        self.assertEqual(market_session_for_timestamp("2026-06-30T02:00:00.000Z"), "overnight")
        self.assertEqual(market_session_for_timestamp("2026-07-06T02:00:00.000Z"), "overnight")
        self.assertEqual(market_session_for_timestamp("2026-06-27T01:00:00.000Z"), "closed")
        self.assertEqual(market_session_for_timestamp("2026-06-28T14:00:00.000Z"), "closed")
        self.assertEqual(market_session_for_timestamp("2026-06-27T02:00:00.000Z"), "closed")
        self.assertEqual(market_session_for_timestamp("2026-07-03T08:30:00.000Z"), "closed")
        self.assertEqual(
            active_feed_profile_for(datetime(2026, 7, 6, 2, 0, tzinfo=timezone.utc)),
            "boats",
        )
        overnight_windows = visible_extended_session_windows(datetime(2026, 6, 30, 2, 0, tzinfo=timezone.utc))
        self.assertEqual([session for session, _, _ in overnight_windows], ["after", "overnight"])
        self.assertEqual(overnight_windows[0][1], datetime(2026, 6, 29, 20, 0, tzinfo=timezone.utc))
        self.assertEqual(overnight_windows[0][2], datetime(2026, 6, 30, 0, 0, tzinfo=timezone.utc))
        pre_windows = visible_extended_session_windows(datetime(2026, 6, 29, 8, 30, tzinfo=timezone.utc))
        self.assertEqual([session for session, _, _ in pre_windows], ["overnight", "pre"])
        self.assertIsNone(active_feed_profile_for(datetime(2026, 6, 27, 2, 0, tzinfo=timezone.utc)))
        with mock.patch.dict(os.environ, {
            "MARKET_CLOSED_DATES": "2026-07-06",
            "MARKET_INCLUDE_DEFAULT_US_EQUITY_HOLIDAYS": "false",
        }):
            self.assertEqual(market_session_for_timestamp("2026-07-03T08:30:00.000Z"), "pre")
            self.assertEqual(market_session_for_timestamp("2026-07-06T02:00:00.000Z"), "closed")
            self.assertEqual(market_session_for_timestamp("2026-07-06T14:00:00.000Z"), "closed")

        crypto = resolve_feed_profile({"ALPACA_FEED_PROFILE": "crypto-us"})
        self.assertEqual(crypto.feed, "crypto")
        self.assertEqual(crypto.sessions, ("crypto",))
        self.assertEqual(crypto.websocket_url, "wss://stream.data.alpaca.markets/v1beta3/crypto/us")
        self.assertTrue(feed_profile_active_for_session(crypto, "closed"))

    def test_crypto_symbol_uses_internal_symbol_and_alpaca_provider_symbol(self):
        """BTCUSD는 내부 표기, BTC/USD는 Alpaca provider 표기로 쓰는 계약을 검증한다."""
        request = build_subscription_request(["BTCUSD"], ["bars", "trades"])
        self.assertEqual(request["bars"], ["BTC/USD"])
        self.assertEqual(request["trades"], ["BTC/USD"])

        envelope = build_raw_envelope(
            {"T": "t", "S": "BTC/USD", "i": 7, "p": 61500.5, "s": 0.013, "t": "2026-06-28T10:15:20.100Z"},
            "crypto",
            feed_profile="crypto-us",
        )

        self.assertEqual(envelope["symbol"], "BTCUSD")
        self.assertEqual(envelope["providerSymbol"], "BTC/USD")
        self.assertEqual(envelope["assetClass"], "crypto")
        self.assertEqual(envelope["marketSession"], "crypto")
        self.assertEqual(envelope["sourceEventId"], "alpaca/crypto/trades/BTCUSD/7/2026-06-28T10:15:20.100Z")

    def test_active_trade_subscription_waits_for_authenticated(self):
        import asyncio
        from alfaka.alpaca import websocket_collector

        profile = resolve_feed_profile({"ALPACA_FEED_PROFILE": "sip"})
        websocket = FakeWebSocket([
            {"T": "success", "msg": "connected"},
            "timeout",
            {"T": "success", "msg": "authenticated"},
            "stop",
        ])

        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        redis.sadd(keys.subscription_symbols(), "AAPL")
        redis.hset(keys.subscription_symbol("AAPL"), {"layers": "trades"})

        with mock.patch.object(websocket_collector.websockets, "connect", return_value=FakeWebSocketConnect(websocket)):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                asyncio.run(websocket_collector.run_stream_session(
                    alpaca_url=profile.websocket_url,
                    alpaca_key="key",
                    alpaca_secret="secret",
                    alpaca_feed=profile.feed,
                    feed_profile=profile,
                    producer=RecordingProducer(),
                    subscribe_request={"action": "subscribe", "bars": ["AAPL"]},
                    redis_client=redis,
                    active_channels=["trades"],
                    active_poll_seconds=0.01,
                    raw_topic_prefix="market.input",
                    enforce_session_window=False,
                ))

        self.assertEqual(websocket.sent[0]["action"], "auth")
        self.assertEqual(websocket.sent[1], {"action": "subscribe", "bars": ["AAPL"]})
        self.assertEqual(websocket.sent[2], {"action": "subscribe", "trades": ["AAPL"]})

    def test_alpaca_stream_session_reports_healthy_events_for_backoff_reset(self):
        import asyncio
        from alfaka.alpaca import websocket_collector

        profile = resolve_feed_profile({"ALPACA_FEED_PROFILE": "sip"})
        websocket = FakeWebSocket([
            {"T": "success", "msg": "connected"},
            {"T": "success", "msg": "authenticated"},
            {"T": "subscription", "bars": ["AAPL"]},
            {"T": "t", "S": "AAPL", "i": 123, "p": 195.2, "s": 10, "t": "2026-06-25T10:15:20.100Z"},
            "stop",
        ])
        healthy_events = []

        with mock.patch.object(websocket_collector.websockets, "connect", return_value=FakeWebSocketConnect(websocket)):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                asyncio.run(websocket_collector.run_stream_session(
                    alpaca_url=profile.websocket_url,
                    alpaca_key="key",
                    alpaca_secret="secret",
                    alpaca_feed=profile.feed,
                    feed_profile=profile,
                    producer=RecordingProducer(),
                    subscribe_request={"action": "subscribe", "bars": ["AAPL"]},
                    redis_client=MemoryRedis(),
                    active_channels=[],
                    active_poll_seconds=0.01,
                    raw_topic_prefix="market.input",
                    enforce_session_window=False,
                    on_session_healthy=lambda reason: healthy_events.append(reason),
                ))

        self.assertIn("authenticated", healthy_events)
        self.assertIn("subscribed", healthy_events)
        self.assertIn("data", healthy_events)

    def test_kafka_publish_failure_does_not_block_alpaca_websocket_receive_loop(self):
        import asyncio
        from alfaka.alpaca import websocket_collector

        profile = resolve_feed_profile({"ALPACA_FEED_PROFILE": "sip"})
        websocket = FakeWebSocket([
            {"T": "success", "msg": "connected"},
            {"T": "success", "msg": "authenticated"},
            {"T": "t", "S": "AAPL", "i": 123, "p": 195.2, "s": 10, "t": "2026-06-25T10:15:20.100Z"},
            "stop",
        ])
        redis = MemoryRedis()

        with mock.patch.object(websocket_collector.websockets, "connect", return_value=FakeWebSocketConnect(websocket)):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                asyncio.run(websocket_collector.run_stream_session(
                    alpaca_url=profile.websocket_url,
                    alpaca_key="key",
                    alpaca_secret="secret",
                    alpaca_feed=profile.feed,
                    feed_profile=profile,
                    producer=FailingProducer("kafka blocked"),
                    subscribe_request={"action": "subscribe"},
                    redis_client=redis,
                    active_channels=[],
                    active_poll_seconds=0.01,
                    raw_topic_prefix="market.input",
                    enforce_session_window=False,
                ))

        health = read_component_health(redis, RedisKeyBuilder(), "market-ingestor-sip")
        self.assertEqual(health["status"], "error")
        self.assertEqual(health["errorCategory"], "kafka_publish_failed")

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
        self.assertEqual(candle["priceAdjustment"], "live")
        self.assertEqual(candle["canonicalVersion"], "v2")
        self.assertEqual(candle_row["feed_profile"], "sip")
        self.assertEqual(candle_row["market_session"], "after")
        self.assertEqual(candle_row["price_adjustment"], "live")
        self.assertEqual(candle_row["canonical_version"], "v2")

    def test_raw_topic_mapping_covers_required_channels(self):
        expected_topics = {
            "b": "market.input.realtime.bars.1m.v1",
            "u": "market.input.realtime.updated-bars.1m.v1",
            "t": "market.input.realtime.trades.v1",
            "q": "market.input.realtime.quotes.v1",
            "d": "market.input.realtime.daily-bars.v1",
            "s": "market.input.realtime.events.v1",
            "c": "market.input.realtime.events.v1",
            "x": "market.input.realtime.events.v1",
        }

        for message_type, topic in expected_topics.items():
            with self.subTest(message_type=message_type):
                self.assertEqual(raw_topic_name("market.input", message_type), topic)

    def test_processor_runtime_config_prefers_processor_group_id(self):
        config = processor_runtime_config({
            "KAFKA_BOOTSTRAP_SERVERS": "kafka:29092",
            "KAFKA_PROCESSOR_GROUP_ID": "alfaka-market-processor",
            "KAFKA_INPUT_TOPIC_PREFIX": "market.input",
            "REDIS_URL": "redis://redis:6379/0",
            "PROCESSOR_RECOVERY_SYMBOLS": "AAPL,MSFT",
            "PROCESSOR_RECOVERY_CLICKHOUSE_ENABLED": "true",
        })

        self.assertEqual(config["group_id"], "alfaka-market-processor")
        self.assertEqual(config["raw_topics"][0], "market.input.realtime.trades.v1")
        self.assertIn("market.input.realtime.events.v1", config["raw_topics"])
        self.assertEqual(config["tick_fanout_topics"], {})
        self.assertNotIn("market.realtime.ticks.to.1m.v1", config["raw_topics"])
        self.assertNotIn("market.realtime.ticks.to.5m.v1", config["raw_topics"])
        self.assertEqual(config["closed_candle_topic"]["1m"], "market.layer.candles.1m.closed.v1")
        self.assertEqual(config["closed_candle_topic"]["1h"], "market.layer.candles.1h.closed.v1")
        self.assertEqual(config["closed_candle_topic"]["4h"], "market.layer.candles.4h.closed.v1")
        self.assertEqual(config["closed_candle_topic"]["1M"], "market.layer.candles.1mo.closed.v1")
        self.assertEqual(config["recovery_symbols"], ["AAPL", "MSFT"])
        self.assertTrue(config["clickhouse_recovery_enabled"])

    def test_processor_runtime_config_can_enable_all_tick_fanout_topics(self):
        config = processor_runtime_config({
            "KAFKA_BOOTSTRAP_SERVERS": "kafka:29092",
            "KAFKA_PROCESSOR_GROUP_ID": "alfaka-market-processor",
            "KAFKA_INPUT_TOPIC_PREFIX": "market.input",
            "REDIS_URL": "redis://redis:6379/0",
            "KAFKA_TICK_FANOUT_INTERVALS": "all",
        })

        self.assertEqual(
            list(config["tick_fanout_topics"]),
            ["1m", "5m", "10m", "1D", "1W", "1M"],
        )
        self.assertIn("market.realtime.ticks.to.1mo.v1", config["raw_topics"])

    def test_processor_runtime_config_can_select_raw_topics_for_split_processors(self):
        config = processor_runtime_config({
            "KAFKA_BOOTSTRAP_SERVERS": "kafka:29092",
            "KAFKA_PROCESSOR_GROUP_ID": "alfaka-market-quote-processor",
            "KAFKA_INPUT_TOPIC_PREFIX": "market.input",
            "REDIS_URL": "redis://redis:6379/0",
            "KAFKA_PROCESSOR_RAW_TOPICS": "market.input.realtime.quotes.v1",
            "KAFKA_TICK_FANOUT_INTERVALS": "all",
        })

        self.assertEqual(config["group_id"], "alfaka-market-quote-processor")
        self.assertEqual(config["raw_topics"], ["market.input.realtime.quotes.v1"])
        self.assertEqual(config["tick_fanout_topics"], {})

    def test_clickhouse_topic_defaults_append_ticks_only_when_trade_load_enabled(self):
        environ = {
            "KAFKA_CLICKHOUSE_TOPICS": "market.layer.candles.closed.v1,market.layer.events.v1",
            "KAFKA_TRADES_LAYER_TOPIC": "market.layer.trades.v1",
        }

        self.assertEqual(clickhouse_topics_from_env(environ, load_trades=False), [
            "market.layer.candles.closed.v1",
            "market.layer.events.v1",
            "market.layer.quotes.v1",
        ])
        self.assertEqual(clickhouse_topics_from_env(environ, load_trades=True), [
            "market.layer.candles.closed.v1",
            "market.layer.events.v1",
            "market.layer.quotes.v1",
            "market.layer.trades.v1",
        ])

    def test_clickhouse_batch_loader_groups_tick_rows_and_commits_once(self):
        client = RecordingClickHouseClient()

        class CommitRecordingConsumer:
            def __init__(self):
                self.commits = 0

            def commit(self):
                self.commits += 1

        payloads = [
            {"eventType": "TRADE", "symbol": "AAPL", "timestamp": "2026-06-25T10:15:01.000Z", "tradeId": 1, "price": 100, "size": 2},
            {"eventType": "TRADE", "symbol": "AAPL", "timestamp": "2026-06-25T10:15:02.000Z", "tradeId": 2, "price": 101, "size": 3},
            {"eventType": "QUOTE", "symbol": "AAPL", "timestamp": "2026-06-25T10:15:03.000Z", "bidPrice": 100, "askPrice": 101},
        ]

        consumer = CommitRecordingConsumer()
        inserted = flush_clickhouse_buffer(
            consumer,
            client,
            payloads,
            load_trades=True,
            load_quotes=True,
            enable_auto_commit=False,
        )

        self.assertEqual(inserted, 3)
        self.assertEqual(consumer.commits, 1)
        self.assertEqual([(table, len(rows)) for table, rows in client.inserts], [("trade_ticks", 2), ("quote_ticks", 1)])

    def test_clickhouse_batch_insert_skips_disabled_quote_rows(self):
        client = RecordingClickHouseClient()
        inserted = load_payload_batch(
            client,
            [{"eventType": "QUOTE", "symbol": "AAPL", "timestamp": "2026-06-25T10:15:03.000Z", "bidPrice": 100, "askPrice": 101}],
            load_trades=True,
            load_quotes=False,
        )

        self.assertEqual(inserted, 0)
        self.assertEqual(client.inserts, [])

    def test_clickhouse_schema_ensure_is_opt_in_for_runtime_starts(self):
        self.assertFalse(should_ensure_schema_on_start({}))
        self.assertFalse(should_ensure_schema_on_start({"CLICKHOUSE_ENSURE_SCHEMA_ON_START": "false"}))
        self.assertTrue(should_ensure_schema_on_start({"CLICKHOUSE_ENSURE_SCHEMA_ON_START": "true"}))

    def test_processor_runtime_config_rejects_placeholders(self):
        with self.assertRaisesRegex(RuntimeError, "placeholder"):
            processor_runtime_config({
                "KAFKA_BOOTSTRAP_SERVERS": "YOUR_MSK_BOOTSTRAP_SERVERS",
                "KAFKA_PROCESSOR_GROUP_ID": "alfaka-market-processor",
                "KAFKA_INPUT_TOPIC_PREFIX": "REPLACE_INPUT_TOPIC_PREFIX",
                "REDIS_URL": "redis://redis:6379/0",
            })
        with self.assertRaisesRegex(RuntimeError, "redis_url contains placeholder"):
            processor_runtime_config({
                "KAFKA_BOOTSTRAP_SERVERS": "kafka:29092",
                "KAFKA_PROCESSOR_GROUP_ID": "alfaka-market-processor",
                "KAFKA_INPUT_TOPIC_PREFIX": "market.input",
                "REDIS_URL": "redis://YOUR_REDIS_ENDPOINT:6379/0",
            })

    def test_runtime_config_validation_rejects_empty_and_embedded_placeholders(self):
        self.assertTrue(has_placeholder_value("http://YOUR_CLICKHOUSE_ENDPOINT:8123"))
        self.assertTrue(has_placeholder_value(["market.layer.candles.closed.v1", "REPLACE_TOPIC"]))
        with self.assertRaisesRegex(RuntimeError, "Invalid test component runtime config"):
            validate_required_values("test component", {
                "kafka_servers": "kafka:29092",
                "topics": ["market.layer.candles.closed.v1", ""],
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
            with mock.patch.object(sys.modules["boto3"], "client", return_value=client) as boto_client:
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
            with mock.patch.object(sys.modules["boto3"], "client") as boto_client:
                key, secret = load_alpaca_credentials()

        self.assertEqual((key, secret), ("local-key", "local-secret"))
        boto_client.assert_not_called()
        self.assertEqual(resolve_alpaca_credential_source({"ALPACA_CREDENTIAL_SOURCE": "aws"}), "aws-secrets-manager")

    def test_alpaca_local_env_supports_aliases_and_ignores_placeholders(self):
        with mock.patch.dict(os.environ, {
            "ALPACA_CREDENTIAL_SOURCE": "env",
            "APCA_API_KEY_ID": "your_key_id",
            "APCA_API_SECRET_KEY": "your_secret_key",
            "ALPACA_API_KEY_ID": " alias-key ",
            "ALPACA_API_SECRET_KEY": " alias-secret ",
        }):
            with mock.patch.object(sys.modules["boto3"], "client") as boto_client:
                key, secret = load_alpaca_credentials()

        self.assertEqual((key, secret), ("alias-key", "alias-secret"))
        boto_client.assert_not_called()

    def test_alpaca_ingestor_log_helpers_redact_bulk_subscription_and_classify_errors(self):
        request = {
            "action": "subscribe",
            "bars": ["AAPL", "MSFT", "NVDA", "TSLA", "GOOGL", "META", "AMZN", "AMD", "AVGO"],
            "trades": ["NVDA"],
            "quotes": ["NVDA"],
        }

        summary = summarize_subscription_request(request)

        self.assertEqual(summary["bars"]["count"], 9)
        self.assertEqual(summary["bars"]["sample"], ["AAPL", "MSFT", "NVDA", "TSLA", "GOOGL", "META", "AMZN", "AMD"])
        self.assertNotIn("AVGO", str(summary))
        self.assertEqual(summary["trades"], {"count": 1, "sample": ["NVDA"]})
        self.assertEqual(classify_alpaca_error({"code": 406, "msg": "connection limit exceeded"}), "connection_limit")
        self.assertEqual(classify_alpaca_error({"msg": "auth timeout"}), "auth_timeout")
        self.assertEqual(classify_alpaca_error({"msg": "auth failed"}), "auth_failed")

    def test_json_producer_uses_burst_tuning_env(self):
        captured = {}

        class FakeKafkaProducer:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        fake_kafka = types.SimpleNamespace(KafkaProducer=FakeKafkaProducer)
        with mock.patch.dict(sys.modules, {"kafka": fake_kafka}):
            with mock.patch.dict(os.environ, {
                "KAFKA_PRODUCER_LINGER_MS": "20",
                "KAFKA_PRODUCER_BATCH_SIZE": "65536",
                "KAFKA_PRODUCER_BUFFER_MEMORY": "67108864",
                "KAFKA_PRODUCER_MAX_BLOCK_MS": "100",
                "KAFKA_PRODUCER_ACKS": "1",
            }, clear=False):
                producer = create_json_producer("kafka:29092", "alpaca-sip")

        self.assertIsInstance(producer, FakeKafkaProducer)
        self.assertEqual(captured["linger_ms"], 20)
        self.assertEqual(captured["batch_size"], 65536)
        self.assertNotIn("buffer_memory", captured)
        self.assertEqual(captured["max_block_ms"], 100)
        self.assertEqual(captured["acks"], 1)

    def test_json_producer_tuning_options_are_supported_by_kafka_python(self):
        from kafka import KafkaProducer

        options = producer_options_from_env(
            {
                "KAFKA_PRODUCER_LINGER_MS": "20",
                "KAFKA_PRODUCER_BATCH_SIZE": "65536",
                "KAFKA_PRODUCER_BUFFER_MEMORY": "67108864",
                "KAFKA_PRODUCER_MAX_BLOCK_MS": "100",
                "KAFKA_PRODUCER_ACKS": "1",
            }
        )

        self.assertNotIn("buffer_memory", options)
        self.assertEqual(options["linger_ms"], 20)
        self.assertEqual(options["batch_size"], 65536)
        self.assertEqual(options["max_block_ms"], 100)
        self.assertEqual(options["acks"], 1)
        self.assertTrue(set(options).issubset(KafkaProducer.DEFAULT_CONFIG))

    def test_alpaca_publish_worker_count_is_configurable_and_never_zero(self):
        self.assertEqual(publish_worker_count_from_env({"ALPACA_KAFKA_PUBLISH_WORKERS": "4"}), 4)
        self.assertEqual(publish_worker_count_from_env({"ALPACA_KAFKA_PUBLISH_WORKERS": "0"}), 1)
        self.assertEqual(publish_worker_count_from_env({"ALPACA_KAFKA_PUBLISH_WORKERS": "not-a-number"}), 1)

    def test_alpaca_aws_secret_supports_canonical_and_legacy_field_names(self):
        class FakeSecretsManager:
            def get_secret_value(self, SecretId):
                return {"SecretString": json.dumps({
                    "key_id": "aws-key",
                    "secret_key": "aws-secret",
                })}

        with mock.patch.dict(os.environ, {
            "ALPACA_CREDENTIAL_SOURCE": "aws-secrets-manager",
            "ALPACA_SECRET_NAME": "dev/alpaca",
            "AWS_REGION": "ap-northeast-2",
            "APCA_API_KEY_ID": "",
            "APCA_API_SECRET_KEY": "",
        }):
            with mock.patch.object(sys.modules["boto3"], "client", return_value=FakeSecretsManager()):
                key, secret = load_alpaca_credentials()

        self.assertEqual((key, secret), ("aws-key", "aws-secret"))

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
                ["market.layer.candles.closed.v1"],
                "kafka:29092",
                "alfaka-clickhouse-loader",
                "alfaka-clickhouse-consumer",
                enable_auto_commit=False,
            )

        self.assertIsInstance(consumer, RecordingKafkaConsumer)
        self.assertEqual(consumer.topics, ("market.layer.candles.closed.v1",))
        self.assertFalse(consumer.kwargs["enable_auto_commit"])
        self.assertEqual(consumer.kwargs["group_id"], "alfaka-clickhouse-loader")

    def test_kafka_consumer_accepts_slow_worker_poll_controls(self):
        RecordingKafkaConsumer.calls = []
        kafka_module = types.SimpleNamespace(KafkaConsumer=RecordingKafkaConsumer)

        with mock.patch.dict(sys.modules, {"kafka": kafka_module}):
            consumer = create_json_consumer(
                ["market.news.alpaca.v1"],
                "kafka:29092",
                "alfaka-news-intelligence-worker",
                "alfaka-news-intelligence-consumer",
                enable_auto_commit=False,
                max_poll_interval_ms=900000,
                max_poll_records=1,
            )

        self.assertIsInstance(consumer, RecordingKafkaConsumer)
        self.assertEqual(consumer.kwargs["max_poll_interval_ms"], 900000)
        self.assertEqual(consumer.kwargs["max_poll_records"], 1)

    def test_live_path_trace_builds_read_only_contract(self):
        with mock.patch.dict(os.environ, {
            "KAFKA_INPUT_TOPIC_PREFIX": "market.input",
            "KAFKA_PROCESSOR_GROUP_ID": "alfaka-market-processor",
            "KAFKA_TRADES_LAYER_TOPIC": "market.layer.trades.v1",
            "KAFKA_LIVE_CANDLE_TOPIC": "market.layer.candles.live.v1",
            "KAFKA_CLOSED_CANDLE_TOPIC": "market.layer.candles.closed.v1",
            "KAFKA_STATUS_TOPIC": "market.layer.events.v1",
            "KAFKA_PROCESSOR_RAW_TOPICS": "market.input.realtime.trades.v1,market.input.realtime.bars.1m.v1",
        }):
            with mock.patch.object(live_path_trace, "check_api", return_value=live_path_trace.trace_check("api", "ok")):
                with mock.patch.object(live_path_trace, "check_redis", return_value=live_path_trace.trace_check("redis", "warn")):
                    with mock.patch.object(live_path_trace, "check_kafka", return_value=live_path_trace.trace_check("kafka", "ok")):
                        trace = live_path_trace.collect_trace(symbol="nvda", interval="1m")

        self.assertEqual(trace["symbol"], "NVDA")
        self.assertEqual(trace["status"], "warn")
        self.assertEqual(trace["config"]["processorGroupId"], "alfaka-market-processor")
        self.assertIn("input Kafka raw trade/quote topics", trace["path"])
        self.assertEqual(live_path_trace.expected_raw_topics("market.input", {})[0], "market.input.realtime.trades.v1")
        self.assertEqual(
            live_path_trace.expected_raw_topics(
                "unused",
                {"KAFKA_PROCESSOR_RAW_TOPICS": "market.input.realtime.quotes.v1"},
            ),
            ["market.input.realtime.quotes.v1"],
        )
        self.assertEqual(live_path_trace.expected_raw_topics("custom.input", {})[0], "custom.input.realtime.trades.v1")
        self.assertEqual(live_path_trace.expected_processed_topics({})[0], "market.layer.trades.v1")
        self.assertEqual(live_path_trace.overall_status([live_path_trace.trace_check("x", "ok")]), "ok")
        self.assertEqual(live_path_trace.overall_status([live_path_trace.trace_check("x", "fail")]), "fail")
        self.assertEqual(live_path_trace.redact_url("redis://user:pass@redis:6379/0"), "redis://***@redis:6379/0")
        self.assertEqual(live_path_trace.recommended_realtime_feed_for_session("pre"), "sip")
        self.assertEqual(live_path_trace.recommended_realtime_feed_for_session("regular"), "sip")
        self.assertEqual(live_path_trace.recommended_realtime_feed_for_session("after"), "sip")
        self.assertEqual(live_path_trace.recommended_realtime_feed_for_session("overnight"), "boats")
        self.assertIsNone(live_path_trace.recommended_realtime_feed_for_session("closed"))
        self.assertEqual(live_path_trace.local_ingestor_service_for_feed("boats"), "alpaca-ingestor-boats")
        self.assertEqual(live_path_trace.recommended_realtime_feed_for_session("closed", symbol="BTCUSD"), "crypto")
        self.assertEqual(live_path_trace.local_ingestor_service_for_feed("crypto"), "alpaca-ingestor-crypto")

    def test_processor_smoke_processes_trade_to_topics_and_redis(self):
        producer = RecordingProducer()
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        topics = mermaid_processor_topics()
        envelope = build_raw_envelope(
            {"T": "t", "S": "AAPL", "i": 123, "p": 195.2, "s": 10, "t": "2026-06-25T10:15:20.100Z"},
            "sip",
        )

        state = ProcessorState()
        result = process_raw_envelope(envelope, producer, redis, keys, state, topics)

        self.assertEqual(result, "trades")
        self.assertEqual([sent["topic"] for sent in producer.sent[:2]], [
            "market.layer.trades.v1",
            "market.layer.candles.live.v1",
        ])
        self.assertNotIn("market.realtime.ticks.to.1m.v1", [sent["topic"] for sent in producer.sent])
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

    def test_processor_live_redis_state_uses_short_ttl(self):
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        trade = {
            "symbol": "MLM",
            "price": 602.94,
            "size": 10,
            "timestamp": "2026-07-06T13:15:20.000Z",
            "feed": "sip",
            "feedProfile": "sip",
            "marketSession": "pre",
        }
        candle = {
            "eventType": "LIVE_CANDLE",
            "symbol": "MLM",
            "interval": "1m",
            "timestamp": "2026-07-06T13:15:00.000Z",
            "open": 602.94,
            "high": 602.94,
            "low": 602.94,
            "close": 602.94,
            "volume": 10,
            "updatedAt": "2026-07-06T13:15:20.000Z",
        }

        with mock.patch.dict(os.environ, {
            "LIVE_CANDLE_TTL_SECONDS": "120",
            "LIVE_TRADE_TTL_SECONDS": "90",
        }):
            write_trade_to_redis(redis, keys, trade)
            write_live_candle_to_redis(redis, keys, candle)

        self.assertEqual(redis.expirations[keys.live_trade("MLM")], 90)
        self.assertEqual(redis.expirations[keys.live_candle("MLM", "1m")], 120)

    def test_processor_allows_newer_daily_live_candle_for_same_closed_bucket(self):
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        closed = {
            "eventType": "CANDLE",
            "symbol": "NVDA",
            "interval": "1D",
            "timestamp": "2026-07-07T04:00:00.000Z",
            "open": 190,
            "high": 198,
            "low": 189,
            "close": 196,
            "volume": 1000,
            "isClosed": True,
            "createdAt": "2026-07-07T20:05:00.000Z",
        }
        live = {
            **closed,
            "eventType": "LIVE_CANDLE",
            "close": 197.64,
            "volume": 1250,
            "isClosed": False,
            "source": "derived.live",
            "sourceInterval": "1m",
            "updatedAt": "2026-07-08T01:26:50.000Z",
        }

        write_closed_candle_to_redis(redis, keys, closed)
        stored = write_live_candle_to_redis(redis, keys, live)

        self.assertTrue(stored)
        stored_live = json.loads(redis.values[keys.live_candle("NVDA", "1D")])
        self.assertEqual(stored_live["close"], 197.64)
        self.assertFalse(stored_live["isClosed"])

    def test_processor_blocks_older_daily_live_candle_for_same_closed_bucket(self):
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        closed = {
            "eventType": "CANDLE",
            "symbol": "NVDA",
            "interval": "1D",
            "timestamp": "2026-07-07T04:00:00.000Z",
            "open": 190,
            "high": 198,
            "low": 189,
            "close": 196,
            "volume": 1000,
            "isClosed": True,
            "createdAt": "2026-07-07T20:05:00.000Z",
        }
        live = {
            **closed,
            "eventType": "LIVE_CANDLE",
            "close": 195,
            "isClosed": False,
            "source": "derived.live",
            "sourceInterval": "1m",
            "updatedAt": "2026-07-07T19:59:00.000Z",
        }

        write_closed_candle_to_redis(redis, keys, closed)
        stored = write_live_candle_to_redis(redis, keys, live)

        self.assertFalse(stored)
        self.assertNotIn(keys.live_candle("NVDA", "1D"), redis.values)

    def test_processor_still_blocks_same_bucket_intraday_live_after_closed_candle(self):
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        closed = {
            "eventType": "CANDLE",
            "symbol": "NVDA",
            "interval": "1m",
            "timestamp": "2026-07-07T20:05:00.000Z",
            "open": 196,
            "high": 197,
            "low": 195,
            "close": 196.5,
            "volume": 100,
            "isClosed": True,
            "createdAt": "2026-07-07T20:06:00.000Z",
        }
        live = {
            **closed,
            "eventType": "LIVE_CANDLE",
            "close": 197,
            "isClosed": False,
            "source": "alpaca.trades",
            "sourceInterval": "trades",
            "updatedAt": "2026-07-07T20:06:30.000Z",
        }

        write_closed_candle_to_redis(redis, keys, closed)
        stored = write_live_candle_to_redis(redis, keys, live)

        self.assertFalse(stored)
        self.assertNotIn(keys.live_candle("NVDA", "1m"), redis.values)

    def test_processor_emits_provisional_live_candles_for_all_chart_intervals(self):
        producer = RecordingProducer()
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        topics = mermaid_processor_topics()
        state = ProcessorState()
        closed_1d = {
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
            "source": "test",
            "feed": "sip",
        }
        closed_1m = {
            "eventType": "CANDLE",
            "symbol": "AAPL",
            "interval": "1m",
            "timestamp": "2026-06-25T10:15:00.000Z",
            "open": 190,
            "high": 191,
            "low": 189,
            "close": 190.5,
            "volume": 100,
            "tradeCount": 1,
            "isClosed": True,
            "source": "test",
            "feed": "sip",
        }
        state.provisional_state.record_closed(closed_1d)
        state.provisional_state.record_closed(closed_1m)
        process_raw_envelope(
            build_raw_envelope({"T": "t", "S": "AAPL", "i": 124, "p": 195.2, "s": 10, "t": "2026-06-25T10:17:20.100Z"}, "sip"),
            producer,
            redis,
            keys,
            state,
            topics,
        )

        live_5m = json.loads(redis.values[keys.live_candle("AAPL", "5m")])
        live_10m = json.loads(redis.values[keys.live_candle("AAPL", "10m")])
        live_1h = json.loads(redis.values[keys.live_candle("AAPL", "1h")])
        live_4h = json.loads(redis.values[keys.live_candle("AAPL", "4h")])
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
        self.assertEqual(live_1h["timestamp"], "2026-06-25T10:00:00.000Z")
        self.assertEqual(live_1h["sourceInterval"], "1m")
        self.assertEqual(live_4h["timestamp"], "2026-06-25T08:00:00.000Z")
        self.assertEqual(live_4h["sourceInterval"], "1m")
        self.assertEqual(live_1d["timestamp"], "2026-06-25T04:00:00.000Z")
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
        self.assertTrue({"1m", "5m", "10m", "1h", "4h", "1D", "1W", "1M"}.issubset(intervals))
        latest_5m_event = [event for event in live_events if event["interval"] == "5m"][-1]
        self.assertEqual(latest_5m_event["source"], "derived.live")
        self.assertEqual(latest_5m_event["sourceInterval"], "1m")
        self.assertEqual(latest_5m_event["data"]["sourceInterval"], "1m")
        self.assertIn("updatedAt", latest_5m_event["data"])

    def test_processor_recovers_provisional_state_from_redis_before_next_trade(self):
        producer = RecordingProducer()
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        topics = mermaid_processor_topics()
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
        topics = mermaid_processor_topics()
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

    def test_bar_event_replaces_provisional_candle_with_confirmed_candle(self):
        producer = RecordingProducer()
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        topics = mermaid_processor_topics()
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

        self.assertIn("market.layer.candles.closed.v1", [sent["topic"] for sent in producer.sent])
        self.assertIn(keys.pending_replace_candle("AAPL", "1m", "2026-06-25T10:15:00.000Z"), redis.values)
        health = read_component_health(redis, keys, "market-processor")
        self.assertEqual(health["lastResult"], "bars_confirmed_replace")

    def test_closed_candle_watermark_blocks_late_live_candle_update(self):
        producer = RecordingProducer()
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        topics = mermaid_processor_topics()
        state = ProcessorState()

        first_trade = build_raw_envelope(
            {"T": "t", "S": "AAPL", "i": 123, "p": 195.2, "s": 10, "t": "2026-06-25T10:15:20.100Z"},
            "sip",
        )
        closed_bar = build_raw_envelope(
            {"T": "b", "S": "AAPL", "t": "2026-06-25T10:15:00.000Z", "o": 195, "h": 196, "l": 194, "c": 195.5, "v": 100},
            "sip",
        )
        late_trade = build_raw_envelope(
            {"T": "t", "S": "AAPL", "i": 124, "p": 196.2, "s": 5, "t": "2026-06-25T10:15:45.000Z"},
            "sip",
        )

        self.assertEqual(process_raw_envelope(first_trade, producer, redis, keys, state, topics), "trades")
        self.assertIn(keys.live_candle("AAPL", "1m"), redis.values)
        self.assertEqual(process_raw_envelope(closed_bar, producer, redis, keys, state, topics), "bars_confirmed_replace")
        self.assertEqual(redis.values[keys.closed_candle_watermark("AAPL", "1m")], "2026-06-25T10:15:00.000Z")
        self.assertNotIn(keys.live_candle("AAPL", "1m"), redis.values)

        live_events_before = [
            message
            for _channel, message in redis.published
            if json.loads(message).get("type") == "LIVE_CANDLE_UPDATE"
        ]
        self.assertEqual(process_raw_envelope(late_trade, producer, redis, keys, state, topics), "trades_blocked_by_closed_watermark")
        self.assertNotIn(keys.live_candle("AAPL", "1m"), redis.values)
        live_events_after = [
            message
            for _channel, message in redis.published
            if json.loads(message).get("type") == "LIVE_CANDLE_UPDATE"
        ]
        self.assertEqual(live_events_after, live_events_before)

    def test_processor_flushes_tick_window_to_closed_candle_and_pubsub(self):
        producer = RecordingProducer()
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        topics = mermaid_processor_topics()
        state = ProcessorState()
        envelope = build_raw_envelope({"T": "t", "S": "AAPL", "i": 123, "p": 2, "s": 100, "t": "2026-06-25T10:15:20.100Z"}, "sip")

        result = process_raw_envelope(envelope, producer, redis, keys, state, topics)
        flushed = flush_ready_closed_candles(producer, redis, keys, state, topics, reference_time="2026-06-25T10:16:06.000Z")

        self.assertEqual(result, "trades")
        self.assertEqual(flushed, 1)
        closed_messages = [sent for sent in producer.sent if sent["topic"] == "market.layer.candles.closed.v1"]
        self.assertTrue(closed_messages)
        self.assertEqual(closed_messages[-1]["value"]["interval"], "1m")
        self.assertTrue(closed_messages[-1]["value"]["isClosed"])
        self.assertIn(keys.latest_candle("AAPL", "1m"), redis.values)
        self.assertIn(keys.recent_candles("AAPL", "1m"), redis.zsets)
        published_events = [json.loads(value) for _, value in redis.published]
        self.assertTrue(any(event["type"] == "CANDLE_CLOSED" and event["interval"] == "1m" for event in published_events))

    def test_tick_window_uses_event_time_for_out_of_order_ohlc(self):
        producer = RecordingProducer()
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        topics = mermaid_processor_topics()
        state = ProcessorState()

        for message in (
            {"T": "t", "S": "AAPL", "i": 2, "p": 102, "s": 10, "t": "2026-06-25T10:15:50.000Z"},
            {"T": "t", "S": "AAPL", "i": 1, "p": 100, "s": 5, "t": "2026-06-25T10:15:05.000Z"},
            {"T": "t", "S": "AAPL", "i": 3, "p": 101, "s": 7, "t": "2026-06-25T10:15:30.000Z"},
        ):
            process_raw_envelope(build_raw_envelope(message, "sip"), producer, redis, keys, state, topics)

        flushed = flush_ready_closed_candles(producer, redis, keys, state, topics, reference_time="2026-06-25T10:16:06.000Z")
        candle = producer.sent[-1]["value"]

        self.assertEqual(flushed, 1)
        self.assertEqual(candle["open"], 100)
        self.assertEqual(candle["close"], 102)
        self.assertEqual(candle["high"], 102)
        self.assertEqual(candle["low"], 100)
        self.assertEqual(candle["volume"], 22)

    def test_tick_window_flushes_same_symbol_minute_once(self):
        producer = RecordingProducer()
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        topics = mermaid_processor_topics()
        state = ProcessorState()

        process_raw_envelope(build_raw_envelope({"T": "t", "S": "AAPL", "i": 1, "p": 100, "s": 1, "t": "2026-06-25T10:15:05.000Z"}, "sip"), producer, redis, keys, state, topics)
        self.assertEqual(flush_ready_closed_candles(producer, redis, keys, state, topics, reference_time="2026-06-25T10:16:06.000Z"), 1)
        self.assertEqual(flush_ready_closed_candles(producer, redis, keys, state, topics, reference_time="2026-06-25T10:17:00.000Z"), 0)

    def test_late_tick_after_watermark_does_not_reopen_closed_candle(self):
        producer = RecordingProducer()
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        topics = mermaid_processor_topics()
        state = ProcessorState()

        process_raw_envelope(build_raw_envelope({"T": "t", "S": "AAPL", "i": 1, "p": 100, "s": 1, "t": "2026-06-25T10:15:05.000Z"}, "sip"), producer, redis, keys, state, topics)
        flush_ready_closed_candles(producer, redis, keys, state, topics, reference_time="2026-06-25T10:16:06.000Z")
        result = process_raw_envelope(build_raw_envelope({"T": "t", "S": "AAPL", "i": 2, "p": 99, "s": 1, "t": "2026-06-25T10:15:10.000Z"}, "sip"), producer, redis, keys, state, topics)

        self.assertEqual(result, "trades_blocked_by_closed_watermark")
        self.assertEqual(flush_ready_closed_candles(producer, redis, keys, state, topics, reference_time="2026-06-25T10:17:00.000Z"), 0)

    def test_processor_commits_after_produce_and_flush_success(self):
        producer = RecordingProducer()
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        topics = mermaid_processor_topics()
        consumer = OneBatchKafkaConsumer(
            build_raw_envelope({"T": "t", "S": "AAPL", "i": 1, "p": 100, "s": 1, "t": "2026-06-25T10:15:05.000Z"}, "sip")
        )

        run_stream_processor(
            consumer,
            producer,
            redis,
            keys,
            ProcessorState(),
            topics,
            enable_auto_commit=False,
            now_fn=lambda: datetime(2026, 6, 25, 10, 16, 6, tzinfo=timezone.utc),
        )

        self.assertGreaterEqual(consumer.commits, 1)
        self.assertGreaterEqual(producer.flush_count, 1)
        self.assertIn("market.layer.candles.live.v1", [sent["topic"] for sent in producer.sent])
        self.assertNotIn("market.realtime.ticks.to.1m.v1", [sent["topic"] for sent in producer.sent])
        self.assertIn("market.layer.candles.closed.v1", [sent["topic"] for sent in producer.sent])

    def test_closed_candle_redis_series_is_trimmed_to_interval_cap(self):
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        cap = 120
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
        app_kustomization = (REPO_ROOT / "infra/k8s/base/app/kustomization.yaml").read_text(encoding="utf-8")
        deployment = (REPO_ROOT / "infra/k8s/base/app/deployment-market-processor.yaml").read_text(encoding="utf-8")
        quote_deployment = (REPO_ROOT / "infra/k8s/base/app/deployment-market-quote-processor.yaml").read_text(encoding="utf-8")
        clickhouse_loader_deployment = (
            REPO_ROOT / "infra/k8s/base/app/deployment-clickhouse-loader.yaml"
        ).read_text(encoding="utf-8")
        agent_orchestrator_deployment = (
            REPO_ROOT / "infra/k8s/base/app/deployment-agent-orchestrator.yaml"
        ).read_text(encoding="utf-8")
        raw_archive_deployment = (REPO_ROOT / "infra/k8s/base/app/deployment-raw-s3-archive.yaml").read_text(
            encoding="utf-8"
        )
        news_worker_deployment = (
            REPO_ROOT / "infra/k8s/base/app/deployment-news-intelligence-worker.yaml"
        ).read_text(encoding="utf-8")
        news_backfill_job = (REPO_ROOT / "infra/k8s/base/job-news-backfill.yaml").read_text(encoding="utf-8")
        news_rebuild_job = (REPO_ROOT / "infra/k8s/base/job-news-intelligence-rebuild.yaml").read_text(encoding="utf-8")
        news_rebuild_script = (REPO_ROOT / "scripts/aws/run-news-cache-rebuild-jobs.sh").read_text(encoding="utf-8")
        configmap = (REPO_ROOT / "infra/k8s/base/app/configmap.yaml").read_text(encoding="utf-8")
        alpaca_ingestor_deployment = (REPO_ROOT / "infra/k8s/base/app/deployment-alpaca-ingestor.yaml").read_text(encoding="utf-8")
        aws_overlay = (REPO_ROOT / "infra/k8s/overlays/aws/kustomization.yaml").read_text(encoding="utf-8")
        aws_ci_overlay = (REPO_ROOT / "infra/k8s/overlays/aws-incluster-app-ci/kustomization.yaml").read_text(encoding="utf-8")

        self.assertIn("  - app", base_kustomization)
        self.assertIn("job-news-backfill.yaml", base_kustomization)
        self.assertIn("deployment-market-processor.yaml", app_kustomization)
        self.assertIn("deployment-market-quote-processor.yaml", app_kustomization)
        self.assertIn("deployment-raw-s3-archive.yaml", app_kustomization)
        self.assertIn("name: alfaka-market-processor", deployment)
        self.assertIn("app: alfaka-market-processor", deployment)
        self.assertIn("gops-market-processor:latest", deployment)
        self.assertIn("systems/market-data/pods/market-processor/local_main.py", deployment)
        self.assertIn("KAFKA_PROCESSOR_RAW_TOPICS", deployment)
        self.assertIn("market.input.realtime.trades.v1,market.input.realtime.bars.1m.v1", deployment)
        self.assertNotIn("market.input.realtime.quotes.v1", deployment)
        self.assertIn("name: alfaka-market-quote-processor", quote_deployment)
        self.assertIn("app: alfaka-market-quote-processor", quote_deployment)
        self.assertIn("KAFKA_PROCESSOR_GROUP_ID", quote_deployment)
        self.assertIn("value: alfaka-market-quote-processor", quote_deployment)
        self.assertIn("value: market.input.realtime.quotes.v1", quote_deployment)
        self.assertIn("name: alfaka-clickhouse-loader", clickhouse_loader_deployment)
        self.assertIn("name: alfaka-clickhouse-tick-loader", clickhouse_loader_deployment)
        self.assertIn("replicas: 3", clickhouse_loader_deployment)
        self.assertIn("value: market.layer.trades.v1,market.layer.quotes.v1", clickhouse_loader_deployment)
        self.assertIn("value: market.layer.candles.1m.closed.v1,market.layer.candles.5m.closed.v1,market.layer.candles.10m.closed.v1,market.layer.candles.1h.closed.v1,market.layer.candles.4h.closed.v1,market.layer.candles.1d.closed.v1,market.layer.candles.1w.closed.v1,market.layer.candles.1mo.closed.v1,market.layer.events.v1,market.news.alpaca.v1", clickhouse_loader_deployment)
        self.assertIn("value: alfaka-clickhouse-tick-loader", clickhouse_loader_deployment)
        self.assertIn("requests:\n              cpu: 100m\n              memory: 128Mi", clickhouse_loader_deployment)
        self.assertIn("limits:\n              cpu: 500m\n              memory: 512Mi", clickhouse_loader_deployment)
        self.assertIn("requests:\n              cpu: 250m\n              memory: 256Mi", clickhouse_loader_deployment)
        self.assertIn('limits:\n              cpu: "1"\n              memory: 768Mi', clickhouse_loader_deployment)
        self.assertIn("name: alfaka-raw-s3-archive", raw_archive_deployment)
        self.assertIn("systems/market-data/pods/s3-sink/raw_archive_sink.py", raw_archive_deployment)
        self.assertIn("gops-market-storage:latest", raw_archive_deployment)
        self.assertIn("name: alfaka-news-intelligence-worker", news_worker_deployment)
        self.assertIn("replicas: 3", news_worker_deployment)
        self.assertIn("name: agent-orchestrator", agent_orchestrator_deployment)
        self.assertIn("name: alfaka-clickhouse-secret", agent_orchestrator_deployment)
        self.assertIn("name: alfaka-news-backfill", news_backfill_job)
        self.assertIn("systems/market-data/jobs/news-backfill/main.py", news_backfill_job)
        self.assertIn("NEWS_BACKFILL_DRY_RUN", news_backfill_job)
        self.assertIn('value: "true"', news_backfill_job)
        self.assertIn("NEWS_BACKFILL_SHARD_INDEX", news_backfill_job)
        self.assertIn("NEWS_BACKFILL_SHARD_COUNT", news_backfill_job)
        self.assertIn("NEWS_BACKFILL_PUBLISH_RECENT_TO_KAFKA", news_backfill_job)
        self.assertIn("NEWS_INTELLIGENCE_REBUILD_DRY_RUN", news_rebuild_job)
        self.assertIn("restartPolicy: Never", news_rebuild_job)
        self.assertIn("../../base/app", aws_overlay)
        self.assertIn("../aws-incluster-app", aws_ci_overlay)
        self.assertNotIn("kind: Job", aws_ci_overlay)
        self.assertNotIn("name: alfaka-news-backfill", aws_ci_overlay)
        self.assertNotIn("name: alfaka-news-intelligence-rebuild", aws_ci_overlay)
        self.assertIn("KAFKA_PROCESSOR_GROUP_ID: alfaka-market-processor", configmap)
        self.assertIn("KAFKA_RAW_S3_GROUP_ID: alfaka-raw-s3-archive", configmap)
        self.assertIn("ALPACA_COLLECTION_SYMBOL_SOURCE: universe", configmap)
        self.assertIn('ALPACA_MAX_TRADE_SYMBOLS: "100"', configmap)
        self.assertIn("name: alfaka-alpaca-ingestor-sip", alpaca_ingestor_deployment)
        self.assertIn("name: alfaka-alpaca-ingestor-boats", alpaca_ingestor_deployment)
        self.assertNotIn("name: alfaka-alpaca-tick-ingestor-sip", alpaca_ingestor_deployment)
        self.assertNotIn("value: alfaka-alpaca-tick-ingestor-sip", alpaca_ingestor_deployment)
        self.assertIn("name: ALPACA_ACTIVE_CHANNELS", alpaca_ingestor_deployment)
        self.assertIn("value: bars,updatedBars,dailyBars,trades,quotes", alpaca_ingestor_deployment)
        self.assertIn('ALPACA_KAFKA_PUBLISH_WORKERS: "4"', configmap)
        self.assertIn('ALPACA_KAFKA_PUBLISH_QUEUE_MAXSIZE: "20000"', configmap)
        self.assertIn('KAFKA_PRODUCER_LINGER_MS: "20"', configmap)
        self.assertIn('KAFKA_PRODUCER_BATCH_SIZE: "65536"', configmap)
        self.assertNotIn("KAFKA_PRODUCER_BUFFER_MEMORY", configmap)
        self.assertIn('CLICKHOUSE_PROVIDER_TIMEOUT_SECONDS: "8"', configmap)
        self.assertIn('CLICKHOUSE_PROVIDER_RETRY_ATTEMPTS: "2"', configmap)
        self.assertIn('KAFKA_CLICKHOUSE_MAX_POLL_RECORDS: "1000"', configmap)
        self.assertIn('CLICKHOUSE_INSERT_BATCH_SIZE: "1000"', configmap)
        self.assertIn('CLICKHOUSE_FLUSH_INTERVAL_SECONDS: "1"', configmap)
        self.assertIn('NEWS_BACKFILL_DAYS: "365"', configmap)
        self.assertIn('NEWS_BACKFILL_SHARD_INDEX: "0"', configmap)
        self.assertIn('NEWS_BACKFILL_SHARD_COUNT: "1"', configmap)
        self.assertIn('NEWS_BACKFILL_INCLUDE_CONTENT: "true"', configmap)
        self.assertIn('NEWS_S3_ARCHIVE_ENABLED: "true"', configmap)
        self.assertIn('NEWS_INTELLIGENCE_REBUILD_REWRITE_CLICKHOUSE: "false"', configmap)
        self.assertIn('CLICKHOUSE_ENSURE_SCHEMA_ON_START: "false"', configmap)
        self.assertIn('CLICKHOUSE_HTTP_TIMEOUT_SECONDS: "10"', configmap)
        self.assertIn('S3_RAW_FLUSH_INTERVAL_SECONDS: "60"', configmap)
        self.assertIn('KAFKA_CLICKHOUSE_ENABLE_AUTO_COMMIT: "false"', configmap)
        self.assertIn('ON_DEMAND_FILL_FOREGROUND_ALPACA_ENABLED: "false"', configmap)
        self.assertIn('ON_DEMAND_FILL_FOREGROUND_MAX_BARS: "120"', configmap)
        self.assertIn('ON_DEMAND_FILL_FOREGROUND_AUTO_INTERVALS: "1m,5m,10m,1h,4h,1D,1W,1M"', configmap)
        self.assertIn('ON_DEMAND_FILL_FOREGROUND_AUTO_MAX_BARS: "500"', configmap)
        self.assertIn("wait_for_rebuild_job", news_rebuild_script)
        self.assertIn('status.conditions[?(@.type=="Failed")].status', news_rebuild_script)
        self.assertIn("--previous=true", news_rebuild_script)
        self.assertIn("kubectl get events", news_rebuild_script)
        self.assertNotIn("kubectl wait --for=condition=complete", news_rebuild_script)
        self.assertIn("Python market-processor pod", aws_overlay)
        self.assertNotIn("managed stream processor", aws_overlay)

    def test_aws_service_detection_includes_news_storage_runtime_units(self):
        lib = (REPO_ROOT / "scripts/aws/lib-gops-images.sh").read_text(encoding="utf-8")
        detector = (REPO_ROOT / "scripts/aws/detect-changed-services.sh").read_text(encoding="utf-8")

        self.assertIn("alfaka-market-quote-processor", lib)
        self.assertIn("alfaka-clickhouse-tick-loader", lib)
        self.assertIn("alfaka-news-intelligence-worker", lib)
        self.assertIn("systems/market-data/pods/news-intelligence-worker/*", detector)
        self.assertIn("systems/market-data/jobs/news-backfill/*", detector)
        self.assertIn("systems/market-data/jobs/news-intelligence-rebuild/*", detector)

    def test_market_ingestor_rollout_targets_all_feed_deployments(self):
        lib = (REPO_ROOT / "scripts/aws/lib-gops-images.sh").read_text(encoding="utf-8")

        self.assertIn("alfaka-alpaca-ingestor-sip", lib)
        self.assertNotIn("alfaka-alpaca-tick-ingestor-sip", lib)
        self.assertIn("alfaka-alpaca-ingestor-boats", lib)
        self.assertIn("alfaka-alpaca-ingestor-crypto", lib)
        self.assertIn("alfaka-alpaca-news-ingestor", lib)

    def test_deploy_smoke_uses_lightweight_health_endpoint(self):
        workflow = (REPO_ROOT / ".github/workflows/deploy-dev.yml").read_text(encoding="utf-8")

        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("  push:", workflow)
        self.assertIn("smoke_url https://stargops.com/api/health", workflow)
        self.assertNotIn("smoke_url https://stargops.com/api/charts/symbols", workflow)

    def test_deploy_workflow_prunes_retired_sip_tick_ingestor(self):
        workflow = (REPO_ROOT / ".github/workflows/deploy-dev.yml").read_text(encoding="utf-8")

        self.assertIn("Delete retired app workloads", workflow)
        self.assertIn("kubectl delete deployment alfaka-alpaca-tick-ingestor-sip", workflow)
        self.assertIn("--ignore-not-found=true", workflow)
        self.assertLess(
            workflow.index("Delete retired app workloads"),
            workflow.index("Deploy app workloads"),
        )

    def test_initial_load_compose_uses_on_demand_universe_contract(self):
        compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertNotIn("initial-load:", compose)
        self.assertNotIn("systems/market-data/jobs/initial-load", compose)
        self.assertIn('ON_DEMAND_FILL_TIMEOUT_SECONDS: "${ON_DEMAND_FILL_TIMEOUT_SECONDS:-30}"', compose)
        self.assertIn("news-backfill:", compose)
        self.assertIn('NEWS_BACKFILL_UNIVERSE: "${NEWS_BACKFILL_UNIVERSE:-sp500}"', compose)
        self.assertIn("systems/market-data/jobs/news-backfill/main.py", compose)

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
            "market-data/rebuild-20260702-lazy-v1/raw/alpaca",
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
        self.assertTrue(any(key.startswith("market-data/rebuild-20260702-lazy-v1/raw/alpaca/source=alpaca/channel=bars/symbol=AAPL/year=2026/month=06/day=25/part-000001-") for key in keys))
        self.assertTrue(any(key.startswith("market-data/rebuild-20260702-lazy-v1/raw/alpaca/source=alpaca/channel=bars/symbol=AAPL/year=2026/month=06/day=26/part-000001-") for key in keys))
        self.assertNotIn("/hour=", "\n".join(keys))

    def test_raw_partition_key_does_not_split_historical_pages_by_hour(self):
        self.assertEqual(
            raw_partition_key("market-data/rebuild-20260702-lazy-v1/raw/alpaca", "bars", "AAPL", "2026-06-25T13:30:00.000Z"),
            "market-data/rebuild-20260702-lazy-v1/raw/alpaca/source=alpaca/channel=bars/symbol=AAPL/year=2026/month=06/day=25",
        )

    def test_raw_chunk_partition_key_groups_historical_request(self):
        self.assertEqual(
            raw_chunk_partition_key("market-data/rebuild-20260702-lazy-v1/raw/alpaca", "daily-bars", "AAPL", "backfill_AAPL_1D_chunk"),
            "market-data/rebuild-20260702-lazy-v1/raw/alpaca/source=alpaca/channel=daily-bars/symbol=AAPL/request=backfill_AAPL_1D_chunk",
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
            "market-data/rebuild-20260702-lazy-v1/raw/alpaca",
            "daily-bars",
            "sip",
            "2026-06-25T00:00:00.000Z",
            "2026-06-27T00:00:00.000Z",
            1,
            {"AAPL": rows},
            manifest_prefix="market-data/rebuild-20260702-lazy-v1/manifest",
            object_id="backfill:AAPL:1D:chunk",
            partition_mode="chunk",
            price_adjustment="split",
            canonical_version="v2",
        )
        raw_keys = [key for key in s3.objects if key.startswith("market-data/rebuild-20260702-lazy-v1/raw/alpaca/")]
        manifest_keys = [key for key in s3.objects if key.startswith("market-data/rebuild-20260702-lazy-v1/manifest/raw/")]
        lookup_keys = raw_keys_from_manifest(
            s3,
            "bucket",
            "market-data/rebuild-20260702-lazy-v1/manifest",
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
            "market-data/rebuild-20260702-lazy-v1/raw/alpaca",
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
            "market-data/rebuild-20260702-lazy-v1/raw/alpaca",
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

    def test_alpaca_news_pages_follow_page_token_and_include_content(self):
        responses = [
            FakeHttpResponse(status_code=429, text="rate limited", headers={"Retry-After": "0"}),
            FakeHttpResponse(status_code=200, payload={"news": [{"id": "news-1"}], "next_page_token": "page-2"}),
            FakeHttpResponse(status_code=200, payload={"news": [{"id": "news-2"}]}),
        ]
        calls = []

        def fake_get(_endpoint, headers, params, timeout):
            calls.append({"headers": headers, "params": dict(params), "timeout": timeout})
            return responses.pop(0)

        with mock.patch.dict(os.environ, {
            "ALPACA_NEWS_MAX_RETRIES": "2",
            "ALPACA_NEWS_RETRY_SLEEP_SECONDS": "0",
            "ALPACA_NEWS_RETRY_MAX_SLEEP_SECONDS": "0",
        }):
            with mock.patch("requests.get", side_effect=fake_get):
                pages = list(iter_alpaca_news_pages(
                    "key",
                    "secret",
                    symbols=["AAPL"],
                    limit=50,
                    include_content=True,
                    start="2026-06-01T00:00:00.000Z",
                    end="2026-07-01T00:00:00.000Z",
                    sort="asc",
                ))

        self.assertEqual([page["pageNumber"] for page in pages], [1, 2])
        self.assertEqual([row["id"] for page in pages for row in page["news"]], ["news-1", "news-2"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[1]["params"]["include_content"], "true")
        self.assertNotIn("page_token", calls[1]["params"])
        self.assertEqual(calls[2]["params"]["page_token"], "page-2")

    def test_news_s3_archive_stores_raw_article_once_and_symbol_indexes_separately(self):
        s3 = S3ObjectStore()
        article = {
            "id": "shared-article",
            "headline": "Apple and Nvidia supply chain update",
            "summary": "Apple and Nvidia are discussed in the same report.",
            "content": "Full article body from Alpaca.",
            "created_at": "2026-06-29T01:02:03.000Z",
            "url": "https://example.com/shared",
            "symbols": ["AAPL", "NVDA"],
        }

        first = upload_canonical_news_article_to_s3(s3, "bucket", "market-data/raw/alpaca", article)
        second = upload_canonical_news_article_to_s3(s3, "bucket", "market-data/raw/alpaca", article)
        aapl_index = write_news_symbol_index_to_s3(
            s3,
            "bucket",
            "market-data/raw/alpaca",
            article,
            symbol="AAPL",
            canonical_key=first["key"],
        )
        nvda_index = write_news_symbol_index_to_s3(
            s3,
            "bucket",
            "market-data/raw/alpaca",
            article,
            symbol="NVDA",
            canonical_key=first["key"],
        )

        raw_keys = [key for key in s3.objects if "/news/articles/" in key]
        index_keys = [key for key in s3.objects if "/news/index/" in key]
        raw_payload = json.loads(s3.objects[first["key"]]["Body"].decode("utf-8"))

        self.assertTrue(first["stored"])
        self.assertFalse(second["stored"])
        self.assertEqual(raw_keys, [canonical_news_article_key("market-data/raw/alpaca", "shared-article")])
        self.assertEqual(len(index_keys), 2)
        self.assertEqual(raw_payload["raw"]["content"], "Full article body from Alpaca.")
        self.assertEqual(aapl_index["key"], news_symbol_index_key("market-data/raw/alpaca", "AAPL", "2026-06-29T01:02:03.000Z", "shared-article"))
        self.assertIn("symbol=NVDA", nvda_index["key"])

    def test_news_backfill_writes_canonical_s3_and_publishes_only_recent_events(self):
        backfill = load_news_backfill_module()
        s3 = S3ObjectStore()
        producer = RecordingProducer()
        recent = {
            "id": "shared-recent",
            "headline": "Apple and Nvidia supply chain update",
            "summary": "Apple and Nvidia are both discussed.",
            "content": "Full body.",
            "created_at": "2026-06-20T01:02:03.000Z",
            "url": "https://example.com/recent",
            "symbols": ["AAPL", "NVDA"],
        }
        old = {
            "id": "old-aapl",
            "headline": "Apple old update",
            "summary": "Old Apple story.",
            "content": "Old body.",
            "created_at": "2026-05-10T01:02:03.000Z",
            "url": "https://example.com/old",
            "symbols": ["AAPL"],
        }
        out_of_range = {
            "id": "ancient-aapl",
            "headline": "Apple article updated recently but published years ago",
            "summary": "This should not be included in a one-year backfill chunk.",
            "content": "Ancient body.",
            "created_at": "2024-05-10T01:02:03.000Z",
            "updated_at": "2026-06-30T01:02:03.000Z",
            "url": "https://example.com/ancient",
            "symbols": ["AAPL"],
        }
        calls = []

        def fake_pages(_key, _secret, symbols, limit, include_content, start, end, sort, max_pages=None):
            calls.append({
                "symbols": list(symbols),
                "limit": limit,
                "include_content": include_content,
                "start": start,
                "end": end,
                "sort": sort,
                "max_pages": max_pages,
            })
            symbol = symbols[0]
            return [{"news": [recent, old, out_of_range] if symbol == "AAPL" else [recent], "nextPageToken": None, "pageNumber": 1}]

        config = {
            "symbols": ["AAPL", "NVDA"],
            "start": "2026-05-01T00:00:00.000Z",
            "end": "2026-07-01T00:00:00.000Z",
            "chunkDays": 90,
            "clickhouseDays": 30,
            "limit": 50,
            "includeContent": True,
            "sort": "asc",
            "s3Bucket": "bucket",
            "s3RawPrefix": "market-data/raw/alpaca",
            "force": False,
            "skipCompletedChunks": True,
            "sleepSeconds": 0,
            "maxPagesPerChunk": 0,
            "maxChunks": 0,
            "publishRecentToKafka": True,
            "kafkaNewsTopic": "market.news.alpaca.v1",
        }

        result = backfill.run_news_backfill(
            config,
            s3=s3,
            producer=producer,
            key_id="key",
            secret_key="secret",
            fetch_pages_fn=fake_pages,
        )

        raw_keys = [key for key in s3.objects if "/news/articles/" in key]
        index_keys = [key for key in s3.objects if "/news/index/" in key]
        marker_keys = [key for key in s3.objects if "/news/manifest/backfill-chunks/" in key]

        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call["include_content"] for call in calls))
        self.assertEqual(len(raw_keys), 2)
        self.assertEqual(len(index_keys), 3)
        self.assertEqual(len(marker_keys), 2)
        self.assertEqual(result["s3RawStored"], 2)
        self.assertEqual(result["s3RawSkipped"], 1)
        self.assertEqual(result["articlesOutOfRange"], 1)
        self.assertEqual(result["eventsPublished"], 2)
        self.assertEqual([item["value"]["articleId"] for item in producer.sent], ["shared-recent", "shared-recent"])
        self.assertEqual({item["key"] for item in producer.sent}, {"AAPL", "NVDA"})
        self.assertNotIn("old-aapl", [item["value"]["articleId"] for item in producer.sent])
        self.assertNotIn("ancient-aapl", [item["value"]["articleId"] for item in producer.sent])

    def test_news_backfill_dry_run_reports_plan_without_fetching(self):
        backfill = load_news_backfill_module()
        config = {
            "symbols": ["AAPL", "NVDA"],
            "start": "2026-06-01T00:00:00.000Z",
            "end": "2026-06-15T00:00:00.000Z",
            "chunkDays": 7,
            "clickhouseDays": 30,
            "includeContent": True,
            "s3Bucket": "bucket",
            "s3RawPrefix": "market-data/raw/alpaca",
            "publishRecentToKafka": False,
        }

        plan = backfill.plan_news_backfill(config)

        self.assertTrue(plan["dryRun"])
        self.assertEqual(plan["symbols"], 2)
        self.assertEqual(plan["chunksPerSymbol"], 2)
        self.assertEqual(plan["totalChunks"], 4)
        self.assertFalse(plan["publishRecentToKafka"])

    def test_news_backfill_empty_universe_registry_path_uses_default_sp500_file(self):
        backfill = load_news_backfill_module()

        symbols = backfill.resolve_news_backfill_symbols({
            "NEWS_BACKFILL_UNIVERSE": "sp500",
            "NEWS_BACKFILL_UNIVERSE_REGISTRY_PATH": "",
            "ALPACA_UNIVERSE_REGISTRY_PATH": "",
            "ALPACA_SYMBOLS": "AAPL,NVDA",
        })

        self.assertIn("AAPL", symbols)
        self.assertIn("NVDA", symbols)
        self.assertGreater(len(symbols), 100)

    def test_news_backfill_shards_symbols_deterministically(self):
        backfill = load_news_backfill_module()

        first = backfill.news_backfill_runtime_config({
            "NEWS_BACKFILL_SYMBOLS": "AAPL,MSFT,NVDA,AMZN,META,GOOGL,TSLA,DDOG",
            "NEWS_BACKFILL_SHARD_INDEX": "0",
            "NEWS_BACKFILL_SHARD_COUNT": "3",
        })
        second = backfill.news_backfill_runtime_config({
            "NEWS_BACKFILL_SYMBOLS": "AAPL,MSFT,NVDA,AMZN,META,GOOGL,TSLA,DDOG",
            "NEWS_BACKFILL_SHARD_INDEX": "1",
            "NEWS_BACKFILL_SHARD_COUNT": "3",
        })
        third = backfill.news_backfill_runtime_config({
            "NEWS_BACKFILL_SYMBOLS": "AAPL,MSFT,NVDA,AMZN,META,GOOGL,TSLA,DDOG",
            "NEWS_BACKFILL_SHARD_INDEX": "2",
            "NEWS_BACKFILL_SHARD_COUNT": "3",
        })

        self.assertEqual(first["symbols"], ["AAPL", "AMZN", "TSLA"])
        self.assertEqual(second["symbols"], ["MSFT", "META", "DDOG"])
        self.assertEqual(third["symbols"], ["NVDA", "GOOGL"])
        combined = first["symbols"] + second["symbols"] + third["symbols"]
        self.assertEqual(sorted(combined), sorted(["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "DDOG"]))
        self.assertEqual(len(combined), len(set(combined)))
        plan = backfill.plan_news_backfill(first)
        self.assertEqual(plan["shardIndex"], 0)
        self.assertEqual(plan["shardCount"], 3)

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

        with mock.patch.dict(os.environ, {"LIVE_CANDLE_STALE_SECONDS": "0"}):
            event = provider.live_event("AAPL", "5m")

        self.assertEqual(event["interval"], "5m")
        self.assertEqual(event["source"], "derived.live")
        self.assertEqual(event["sourceInterval"], "1m")
        self.assertEqual(event["data"]["updatedAt"], "2026-06-25T10:17:20.250Z")

    def test_redis_provider_allows_newer_daily_live_candle_for_same_closed_bucket(self):
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        closed = {
            "eventType": "CANDLE",
            "symbol": "NVDA",
            "interval": "1D",
            "timestamp": "2026-07-07T04:00:00.000Z",
            "open": 190,
            "high": 198,
            "low": 189,
            "close": 196,
            "volume": 1000,
            "isClosed": True,
            "createdAt": "2026-07-07T20:05:00.000Z",
        }
        redis.set(keys.latest_closed_candle("NVDA", "1D"), json.dumps(closed))
        redis.set(keys.closed_candle_watermark("NVDA", "1D"), closed["timestamp"])
        redis.set(keys.live_candle("NVDA", "1D"), json.dumps({
            **closed,
            "eventType": "LIVE_CANDLE",
            "close": 197.64,
            "volume": 1250,
            "isClosed": False,
            "source": "derived.live",
            "sourceInterval": "1m",
            "updatedAt": "2026-07-08T01:26:50.000Z",
        }))
        provider = RedisMarketDataProvider.__new__(RedisMarketDataProvider)
        provider.redis = redis
        provider.keys = keys

        with mock.patch.dict(os.environ, {"LIVE_CANDLE_STALE_SECONDS": "0"}):
            event = provider.live_event("NVDA", "1D")

        self.assertEqual(event["interval"], "1D")
        self.assertEqual(event["data"]["close"], 197.64)
        self.assertFalse(event["data"]["isClosed"])

    def test_redis_provider_ignores_stale_live_candle(self):
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        redis.set(keys.live_candle("MLM", "1m"), json.dumps({
            "eventType": "LIVE_CANDLE",
            "symbol": "MLM",
            "interval": "1m",
            "timestamp": "2000-01-01T13:15:00.000Z",
            "close": 602.94,
            "updatedAt": "2000-01-01T13:15:20.000Z",
        }))
        provider = RedisMarketDataProvider.__new__(RedisMarketDataProvider)
        provider.redis = redis
        provider.keys = keys

        with mock.patch.dict(os.environ, {"LIVE_CANDLE_STALE_SECONDS": "180"}):
            event = provider.live_event("MLM", "1m")

        self.assertIsNone(event)

    def test_redis_market_data_provider_uses_short_socket_timeouts(self):
        calls = []

        def fake_from_url(url, **kwargs):
            calls.append((url, kwargs))
            return MemoryRedis()

        import alfaka.serving.redis_provider as redis_provider_module

        with mock.patch.object(redis_provider_module, "redis", types.SimpleNamespace(from_url=fake_from_url)):
            with mock.patch.dict(os.environ, {
                "REDIS_CONNECT_TIMEOUT_SECONDS": "0.13",
                "REDIS_SOCKET_TIMEOUT_SECONDS": "0.27",
                "REDIS_HEALTH_CHECK_INTERVAL_SECONDS": "9",
            }):
                RedisMarketDataProvider(redis_url="redis://market-data")

        self.assertEqual(calls[0][0], "redis://market-data")
        self.assertTrue(calls[0][1]["decode_responses"])
        self.assertEqual(calls[0][1]["socket_connect_timeout"], 0.13)
        self.assertEqual(calls[0][1]["socket_timeout"], 0.27)
        self.assertEqual(calls[0][1]["health_check_interval"], 9)

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

    def test_candle_aggregator_builds_hourly_contract(self):
        aggregator = CandleAggregator()
        aggregated = None
        for minute in range(60):
            aggregated = aggregator.update({
                "eventType": "CANDLE",
                "symbol": "AAPL",
                "interval": "1m",
                "timestamp": f"2026-06-25T10:{minute:02d}:00.000Z",
                "open": 100 + minute,
                "high": 101 + minute,
                "low": 99 + minute,
                "close": 100.5 + minute,
                "volume": 100,
                "tradeCount": 1,
                "vwap": 100.25 + minute,
                "correctionType": "NONE",
                "feed": "sip",
                "sourceEventId": f"event-{minute}",
                "createdAt": "2026-06-25T10:00:01.000Z",
            }, 60)

        self.assertIsNotNone(aggregated)
        self.assertEqual(aggregated["interval"], "1h")
        self.assertEqual(aggregated["timestamp"], "2026-06-25T10:00:00.000Z")
        self.assertEqual(aggregated["open"], 100)
        self.assertEqual(aggregated["close"], 159.5)
        self.assertEqual(aggregated["volume"], 6000)

    def test_redis_key_prefix_keeps_contract_namespaced(self):
        keys = RedisKeyBuilder("gops-prod")

        self.assertEqual(keys.price_latest("AAPL"), "gops-prod:live:trade:AAPL")
        self.assertEqual(keys.recent_candles("AAPL", "1m"), "gops-prod:cache:candles:AAPL:1m")
        self.assertEqual(keys.live_candle("AAPL", "1m"), "gops-prod:live:candle:AAPL:1m")
        self.assertEqual(keys.latest_closed_candle("AAPL", "1m"), "gops-prod:latest:closed:candle:AAPL:1m")
        self.assertEqual(keys.live_quote("AAPL"), "gops-prod:live:quote:AAPL")
        self.assertEqual(keys.subscription_symbols(), "gops-prod:subscription:symbols")
        self.assertEqual(keys.market_events_symbol("AAPL"), "gops-prod:market.events:AAPL")
        self.assertEqual(keys.active_symbols(), "gops-prod:active:charts:symbols")
        self.assertEqual(keys.backfill_lock("AAPL", "1m", "abc"), "gops-prod:backfill:lock:abc")
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

    def test_backfill_store_rejects_oversized_1m_gapfill(self):
        store = RedisBackfillStore(redis_client=MemoryRedis(), ttl_seconds=60)

        with mock.patch.dict(os.environ, {"BACKFILL_MAX_GAPFILL_1M_RANGE_HOURS": "336"}):
            with self.assertRaisesRegex(ValueError, "Rejected oversized 1m gapfill"):
                store.create_request(
                    "AAPL",
                    "1m",
                    start="2020-07-01T00:00:00.000Z",
                    end="2026-06-30T00:00:00.000Z",
                    mode="queue",
                    force=True,
                )

        self.assertEqual(store.queue_metrics()["stream"]["retainedLength"], 0)

    def test_backfill_store_marks_stale_gapfill_failed_and_allows_retry(self):
        redis_client = MemoryRedis()
        store = RedisBackfillStore(redis_client=redis_client, ttl_seconds=60)
        record, _deduped = store.create_request(
            "AAPL",
            "1m",
            start="2026-06-25T13:30:00.000Z",
            end="2026-06-25T14:30:00.000Z",
            mode="queue",
        )
        stale = {
            **record,
            "status": "running",
            "heartbeatAt": "2026-06-25T13:30:00.000Z",
            "startedAt": "2026-06-25T13:30:00.000Z",
        }
        redis_client.set(store.keys.backfill_status(record["requestId"]), json.dumps(stale, separators=(",", ":")))

        with mock.patch.dict(os.environ, {"BACKFILL_ACTIVE_STALE_SECONDS": "60"}):
            status = store.latest_status("AAPL", "1m")
            retry, deduped = store.create_request(
                "AAPL",
                "1m",
                start="2026-06-25T13:30:00.000Z",
                end="2026-06-25T14:30:00.000Z",
                mode="queue",
            )

        self.assertEqual(status["status"], "failed")
        self.assertIn("marked stale", status["error"])
        self.assertFalse(deduped)
        self.assertIn(":retry:", retry["requestId"])

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

    def test_backfill_store_reclaims_real_redis_pending_without_request_id_in_xpending(self):
        class RealisticPendingRedis(MemoryRedis):
            def xpending_range(self, key, group, min="-", max="+", count=10):
                rows = super().xpending_range(key, group, min=min, max=max, count=count)
                return [
                    {field: value for field, value in row.items() if field != "requestId"}
                    for row in rows
                ]

        redis_client = RealisticPendingRedis()
        store = RedisBackfillStore(redis_client=redis_client, ttl_seconds=60)
        record, _deduped = store.create_request(
            "NVDA",
            "1m",
            start="2026-06-25T13:30:00.000Z",
            end="2026-06-25T14:30:00.000Z",
            mode="queue",
        )

        first_item = store.read_next_queue_item(consumer_name="worker-a", timeout=0)
        second_item = store.read_next_queue_item(consumer_name="worker-b", timeout=0, reclaim_idle_ms=0, max_attempts=3)

        self.assertEqual(first_item.request_id, record["requestId"])
        self.assertEqual(second_item.request_id, record["requestId"])
        self.assertEqual(second_item.stream_id, first_item.stream_id)

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

        with mock.patch.dict(os.environ, {"BACKFILL_INITIAL_LOAD_1M_MIN_START": "2020-07-01T00:00:00Z"}):
            with self.assertRaisesRegex(ValueError, "before BACKFILL_INITIAL_LOAD_1M_MIN_START"):
                store.create_initial_load_requests(
                    ["AAPL"],
                    "1m",
                    "2020-06-01T00:00:00.000Z",
                    "2020-07-01T00:00:00.000Z",
                    max_enqueued=1,
                    max_backlog=10,
                )

            dry_run = module.plan_initial_load(
                store,
                symbols=["AAPL"],
                intervals=["1m"],
                start="2020-06-01T00:00:00.000Z",
                end="2020-07-01T00:00:00.000Z",
                dry_run=True,
            )

            allowed_1m = store.create_initial_load_requests(
                ["AAPL"],
                "1m",
                "2020-07-01T00:00:00.000Z",
                "2023-07-06T00:00:00.000Z",
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
        self.assertIn("2020-07-01T00:00:00.000Z", dry_run["items"][0]["error"])
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
            "processedObjects": ["s3://bucket/market-data/rebuild-20260702-lazy-v1/final/candles/part.parquet"],
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
            "emptyMarker": "s3://bucket/market-data/rebuild-20260702-lazy-v1/manifest/empty/candles/interval=1D/symbol=PSKY/request=chunk.json",
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
            "emptyMarker": "s3://bucket/market-data/rebuild-20260702-lazy-v1/manifest/empty/candles/interval=1D/symbol=PSKY/request=retry.json",
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
        self.assertEqual(symbols, [])

    def test_default_symbol_collection_is_empty_for_on_demand_startup(self):
        symbols = configured_collection_symbols()

        self.assertEqual(symbols, [])

    def test_initial_load_explicit_symbols_are_allowed_without_default_universe(self):
        module = load_initial_load_job_module()

        symbols, source = module.resolve_initial_load_symbols("IBM,ORCL,IBM")

        self.assertEqual(source, "explicit")
        self.assertEqual(symbols, ["IBM", "ORCL"])

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

    def test_backfill_runner_skips_unmanifested_processed_s3_in_canonical_mode(self):
        object_key = "market-data/rebuild-20260702-lazy-v1/final/candles/interval=1m/symbol=AAPL/year=2026/month=06/day=25/part-1.jsonl"
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

        with mock.patch.dict(os.environ, {"S3_BUCKET": "bucket", "S3_FINAL_PREFIX": "market-data/rebuild-20260702-lazy-v1/final", "S3_PROCESSED_FORMAT": "jsonl"}):
            with mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", return_value=[
                alpaca_raw_bar("2026-06-25T13:30:00.000Z", open_price=10),
            ]):
                result = runner._run(record)

        self.assertEqual(result["source"], "alpaca")
        self.assertEqual(result["materializedRowCount"], 1)
        self.assertEqual(client.inserts[0][0], "chart_candles")
        self.assertEqual(client.inserts[0][1][0]["price_adjustment"], "split")
        self.assertEqual(client.inserts[0][1][0]["canonical_version"], "v2")
        self.assertEqual(client.inserts[1][0], "storage_object_audit")
        self.assertEqual(client.inserts[1][1][0]["dataset"], "candles")
        self.assertEqual(client.inserts[2][0], "load_audit")

    def test_processed_s3_canonical_backfill_uses_deterministic_key_and_skips_duplicate_upload(self):
        s3 = S3ObjectStore()
        rows = [
            {
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
                "source": "alpaca.bars",
                **canonical_candle_fields(),
            },
            {
                "eventType": "CANDLE",
                "symbol": "AAPL",
                "interval": "1m",
                "timestamp": "2026-06-25T13:31:00.000Z",
                "open": 10.5,
                "high": 11.5,
                "low": 10,
                "close": 11,
                "volume": 1100,
                "isClosed": True,
                "source": "alpaca.bars",
                **canonical_candle_fields(),
            },
        ]
        partition = "market-data/rebuild-20260702-lazy-v1/final/candles/interval=1m/symbol=AAPL/backfill_request=req-1"

        first = flush_buffer(s3, "bucket", partition, rows, "jsonl", manifest_prefix="market-data/rebuild-20260702-lazy-v1/manifest", manifest_layout="compact")
        second = flush_buffer(s3, "bucket", partition, rows, "jsonl", manifest_prefix="market-data/rebuild-20260702-lazy-v1/manifest", manifest_layout="compact")

        data_keys = [key for key in s3.objects if key.startswith("market-data/rebuild-20260702-lazy-v1/final/")]
        self.assertEqual(first, second)
        self.assertEqual(len(data_keys), 1)
        self.assertIn("/range=20260625T133000Z_20260625T133100Z/adjustment=split/canonical=v2.jsonl", first)
        self.assertNotIn("/part-", first)

    def test_processed_s3_canonical_backfill_force_creates_revision_key(self):
        s3 = S3ObjectStore()
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
            **canonical_candle_fields(),
        }
        partition = "market-data/rebuild-20260702-lazy-v1/final/candles/interval=1D/symbol=AAPL/backfill_request=req-1"

        base_key = flush_buffer(s3, "bucket", partition, [row], "jsonl", manifest_prefix="market-data/rebuild-20260702-lazy-v1/manifest", manifest_layout="compact")
        revision_key = flush_buffer(s3, "bucket", partition, [row], "jsonl", manifest_prefix="market-data/rebuild-20260702-lazy-v1/manifest", manifest_layout="compact", force=True)

        data_keys = [key for key in s3.objects if key.startswith("market-data/rebuild-20260702-lazy-v1/final/")]
        self.assertNotEqual(base_key, revision_key)
        self.assertIn("/revisions/revision=", revision_key)
        self.assertEqual(len(data_keys), 2)

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

        with mock.patch.dict(os.environ, {
            "S3_BUCKET": "bucket",
            "S3_RAW_PREFIX": "market-data/rebuild-20260702-lazy-v1/raw/alpaca",
            "S3_FINAL_PREFIX": "market-data/rebuild-20260702-lazy-v1/final",
            "S3_PROCESSED_FORMAT": "jsonl",
        }):
            with mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", return_value=[
                alpaca_raw_bar("2026-06-25T00:00:00.000Z", open_price=10)
            ]) as fetch:
                result = runner._run(record)

        raw_keys = [key for key in s3.objects if key.startswith("market-data/rebuild-20260702-lazy-v1/raw/alpaca/")]
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

        with mock.patch.dict(os.environ, {"S3_BUCKET": "bucket", "S3_MANIFEST_PREFIX": "market-data/rebuild-20260702-lazy-v1/manifest"}):
            with mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", return_value=[]):
                result = runner._run(record)

        marker_keys = [key for key in s3.objects if key.startswith("market-data/rebuild-20260702-lazy-v1/manifest/empty/candles/")]
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
            **canonical_candle_fields(),
        }

        object_key = flush_buffer(
            s3,
            "bucket",
            "market-data/rebuild-20260702-lazy-v1/final/candles/interval=1m/symbol=AAPL/year=2026/month=06/day=25",
            [row],
            "jsonl",
            manifest_prefix="market-data/rebuild-20260702-lazy-v1/manifest",
        )
        keys = processed_candle_keys_from_manifest(
            s3,
            "bucket",
            "market-data/rebuild-20260702-lazy-v1/manifest",
            "AAPL",
            "1m",
            "2026-06-25T13:30:00.000Z",
            "2026-06-25T13:31:00.000Z",
        )

        self.assertEqual(keys, [object_key])
        manifest_keys = [key for key in s3.objects if key.startswith("market-data/rebuild-20260702-lazy-v1/manifest/")]
        self.assertEqual(len(manifest_keys), 1)
        manifest = json.loads(s3.get_object(Bucket="bucket", Key=manifest_keys[0])["Body"].read().decode("utf-8"))
        self.assertEqual(manifest["priceAdjustment"], "split")
        self.assertEqual(manifest["canonicalVersion"], "v2")

    def test_processed_manifest_excludes_legacy_unknown_candles_from_lookup(self):
        s3 = S3ObjectStore()
        legacy_row = {
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

        flush_buffer(
            s3,
            "bucket",
            "market-data/rebuild-20260702-lazy-v1/final/candles/interval=1m/symbol=AAPL/year=2026/month=06/day=25",
            [legacy_row],
            "jsonl",
            manifest_prefix="market-data/rebuild-20260702-lazy-v1/manifest",
        )
        keys = processed_candle_keys_from_manifest(
            s3,
            "bucket",
            "market-data/rebuild-20260702-lazy-v1/manifest",
            "AAPL",
            "1m",
            "2026-06-25T13:30:00.000Z",
            "2026-06-25T13:31:00.000Z",
        )

        self.assertEqual(keys, [])

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
                **canonical_candle_fields(),
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
                **canonical_candle_fields(),
            },
        ]

        object_key = flush_buffer(
            s3,
            "bucket",
            "market-data/rebuild-20260702-lazy-v1/final/candles/interval=1D/symbol=AAPL/backfill_request=backfill_AAPL_1D_chunk",
            rows,
            "jsonl",
            manifest_prefix="market-data/rebuild-20260702-lazy-v1/manifest",
            manifest_layout="compact",
        )
        keys = processed_candle_keys_from_manifest(
            s3,
            "bucket",
            "market-data/rebuild-20260702-lazy-v1/manifest",
            "AAPL",
            "1D",
            "2026-06-26T00:00:00.000Z",
            "2026-06-27T00:00:00.000Z",
        )
        manifest_keys = [key for key in s3.objects if key.startswith("market-data/rebuild-20260702-lazy-v1/manifest/candles/")]

        self.assertEqual(keys, [object_key])
        self.assertEqual(len(manifest_keys), 1)
        self.assertIn("/objects/", manifest_keys[0])

    def test_processed_manifest_prefers_historical_object_over_live_overlap(self):
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
                **canonical_candle_fields("live"),
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
                **canonical_candle_fields("live"),
            },
        ]
        live_key = flush_buffer(
            s3,
            "bucket",
            "market-data/live/candles/interval=1D/symbol=AAPL/year=2026/month=06/day=25",
            rows,
            "jsonl",
            manifest_prefix="market-data/rebuild-20260702-lazy-v1/manifest",
            manifest_layout="compact",
        )
        historical_rows = [{**row, **canonical_candle_fields()} for row in rows]
        historical_key = flush_buffer(
            s3,
            "bucket",
            "market-data/rebuild-20260702-lazy-v1/final/candles/interval=1D/symbol=AAPL/backfill_request=backfill_AAPL_1D_chunk",
            historical_rows,
            "jsonl",
            manifest_prefix="market-data/rebuild-20260702-lazy-v1/manifest",
            manifest_layout="compact",
        )

        keys = processed_candle_keys_from_manifest(
            s3,
            "bucket",
            "market-data/rebuild-20260702-lazy-v1/manifest",
            "AAPL",
            "1D",
            "2026-06-25T00:00:00.000Z",
            "2026-06-27T00:00:00.000Z",
        )

        self.assertEqual(keys, [historical_key])
        self.assertNotIn(live_key, keys)

    def test_raw_s3_archive_writes_manifest_for_replay_lookup(self):
        s3 = S3ObjectStore()
        rows = [alpaca_raw_bar("2026-06-25T13:30:00.000Z", open_price=10)]

        count = upload_raw_page_to_s3(
            s3,
            "bucket",
            "market-data/rebuild-20260702-lazy-v1/raw/alpaca",
            "bars",
            "sip",
            "2026-06-25T13:30:00.000Z",
            "2026-06-25T13:31:00.000Z",
            1,
            {"AAPL": rows},
            manifest_prefix="market-data/rebuild-20260702-lazy-v1/manifest",
            price_adjustment="split",
            canonical_version="v2",
        )
        keys = raw_keys_from_manifest(
            s3,
            "bucket",
            "market-data/rebuild-20260702-lazy-v1/manifest",
            "AAPL",
            ["bars"],
            "2026-06-25T13:30:00.000Z",
            "2026-06-25T13:31:00.000Z",
        )

        self.assertEqual(count, 1)
        self.assertEqual(len(keys), 1)
        self.assertIn("/source=alpaca/channel=bars/symbol=AAPL/", keys[0])
        manifest_keys = [key for key in s3.objects if key.startswith("market-data/rebuild-20260702-lazy-v1/manifest/raw/")]
        self.assertEqual(len(manifest_keys), 1)

    def test_raw_live_s3_archive_writes_replay_manifest(self):
        s3 = S3ObjectStore()
        envelope = build_raw_envelope(
            {"T": "b", "S": "AAPL", "t": "2026-06-25T13:30:00.000Z", "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1000},
            "sip",
        )
        archive_row = raw_archive_row(envelope)
        archive_row.update(canonical_candle_fields())
        partition_key = raw_envelope_partition_key("market-data/rebuild-20260702-lazy-v1/raw/alpaca", archive_row)

        object_key = flush_raw_buffer(
            s3,
            "bucket",
            partition_key,
            [archive_row],
            manifest_prefix="market-data/rebuild-20260702-lazy-v1/manifest",
        )
        keys = raw_keys_from_manifest(
            s3,
            "bucket",
            "market-data/rebuild-20260702-lazy-v1/manifest",
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
        partition_key = raw_envelope_partition_key("market-data/rebuild-20260702-lazy-v1/raw/alpaca", archive_row)

        self.assertEqual(archive_row["eventTime"], "2026-06-25T13:31:00.000Z")
        self.assertIn("/channel=updated-bars/", partition_key)

    def test_s3_time_based_flush_handles_low_volume_partitions(self):
        s3 = S3ObjectStore()
        buffers = {
            "market-data/rebuild-20260702-lazy-v1/raw/alpaca/source=alpaca/channel=bars/symbol=AAPL/year=2026/month=06/day=25": [
                raw_archive_row(build_raw_envelope({"T": "b", "S": "AAPL", "t": "2026-06-25T13:30:00.000Z"}, "sip"))
            ]
        }
        last_updated_at = {
            "market-data/rebuild-20260702-lazy-v1/raw/alpaca/source=alpaca/channel=bars/symbol=AAPL/year=2026/month=06/day=25": datetime(2026, 6, 25, 13, 30, tzinfo=timezone.utc)
        }

        flushed = flush_due_buffers(
            buffers,
            last_updated_at,
            lambda partition_key, rows: flush_raw_buffer(s3, "bucket", partition_key, rows, manifest_prefix="market-data/rebuild-20260702-lazy-v1/manifest"),
            flush_interval_seconds=60,
            now=datetime(2026, 6, 25, 13, 31, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(len(flushed), 1)
        self.assertFalse(buffers["market-data/rebuild-20260702-lazy-v1/raw/alpaca/source=alpaca/channel=bars/symbol=AAPL/year=2026/month=06/day=25"])
        self.assertTrue(any(key.startswith("market-data/rebuild-20260702-lazy-v1/raw/alpaca/") for key in s3.objects))

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
            raw_prefix="market-data/rebuild-20260702-lazy-v1/raw/alpaca",
            manifest_prefix="market-data/rebuild-20260702-lazy-v1/manifest",
            flush_count=100,
            flush_interval_seconds=3600,
            put_retry_sleep_seconds=0,
        )

        raw_keys = [key for key in s3.objects if key.startswith("market-data/rebuild-20260702-lazy-v1/raw/alpaca/")]
        manifest_keys = [key for key in s3.objects if key.startswith("market-data/rebuild-20260702-lazy-v1/manifest/raw/")]
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
            "market-data/rebuild-20260702-lazy-v1/final/candles/interval=1m/symbol=AAPL/year=2026/month=06/day=25",
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
        partition_key = raw_envelope_partition_key("market-data/rebuild-20260702-lazy-v1/raw/alpaca", archive_row)

        object_key = flush_raw_buffer(
            s3,
            "bucket",
            partition_key,
            [archive_row],
            manifest_prefix="market-data/rebuild-20260702-lazy-v1/manifest",
            max_attempts=2,
            retry_sleep_seconds=0,
        )

        manifest_keys = [key for key in s3.objects if key.startswith("market-data/rebuild-20260702-lazy-v1/manifest/raw/")]
        self.assertEqual(s3.put_attempts, 3)
        self.assertIn(object_key, s3.objects)
        self.assertEqual(len(manifest_keys), 1)

    def test_raw_s3_archive_runtime_config_uses_raw_topics(self):
        config = raw_s3_archive_runtime_config({
            "KAFKA_BOOTSTRAP_SERVERS": "kafka:29092",
            "KAFKA_RAW_S3_GROUP_ID": "raw-archive",
            "S3_BUCKET": "bucket",
            "S3_RAW_PREFIX": "market-data/rebuild-20260702-lazy-v1/raw/alpaca",
            "S3_RAW_FLUSH_COUNT": "10",
            "S3_RAW_FLUSH_INTERVAL_SECONDS": "15",
        })

        self.assertEqual(config["group_id"], "raw-archive")
        self.assertEqual(config["topics"][0], "market.input.realtime.trades.v1")
        self.assertIn("market.input.realtime.events.v1", config["topics"])
        self.assertIn("market.input.realtime.quotes.v1", config["topics"])
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
            **canonical_candle_fields(),
        }
        object_key = flush_buffer(
            s3,
            "bucket",
            "market-data/rebuild-20260702-lazy-v1/final/candles/interval=1m/symbol=AAPL/year=2026/month=06/day=25",
            [source_row],
            "jsonl",
            manifest_prefix="market-data/rebuild-20260702-lazy-v1/manifest",
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

        with mock.patch.dict(os.environ, {"S3_BUCKET": "bucket", "S3_FINAL_PREFIX": "market-data/rebuild-20260702-lazy-v1/final", "S3_MANIFEST_PREFIX": "market-data/rebuild-20260702-lazy-v1/manifest"}):
            with mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", side_effect=AssertionError("Alpaca should not be called")):
                result = runner._run(record)

        self.assertEqual(result["source"], "s3-processed")
        self.assertEqual(result["processedObjects"], [f"s3://bucket/{object_key}"])

    def test_backfill_runner_force_bypasses_manifested_processed_s3_objects(self):
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
            **canonical_candle_fields(),
        }
        flush_buffer(
            s3,
            "bucket",
            "market-data/rebuild-20260702-lazy-v1/final/candles/interval=1m/symbol=AAPL/year=2026/month=06/day=25",
            [source_row],
            "jsonl",
            manifest_prefix="market-data/rebuild-20260702-lazy-v1/manifest",
        )
        record = {
            "requestId": "backfill:AAPL:1m:force",
            "symbol": "AAPL",
            "interval": "1m",
            "range": {"start": "2026-06-25T13:30:00.000Z", "end": "2026-06-25T13:31:00.000Z"},
            "jobType": "gapfill",
            "sourcePreference": "coverage-first",
            "force": True,
        }
        client = RecordingClickHouseClient()
        runner = BackfillRunner(
            s3=s3,
            clickhouse_client=client,
            coverage_provider=StaticCoverageProvider({"rowCount": 0, "availableFrom": None, "availableTo": None}),
        )

        with mock.patch.dict(os.environ, {"S3_BUCKET": "bucket", "S3_FINAL_PREFIX": "market-data/rebuild-20260702-lazy-v1/final", "S3_MANIFEST_PREFIX": "market-data/rebuild-20260702-lazy-v1/manifest", "S3_PROCESSED_FORMAT": "jsonl"}):
            with mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", return_value=[
                alpaca_raw_bar("2026-06-25T13:30:00.000Z", open_price=12),
            ]):
                result = runner._run(record)

        self.assertEqual(result["source"], "alpaca")
        self.assertEqual(client.inserts[0][1][0]["open"], 12)

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
            **canonical_candle_fields(),
        }
        object_key = flush_buffer(
            s3,
            "bucket",
            "market-data/rebuild-20260702-lazy-v1/final/candles/interval=1m/symbol=AAPL/year=2026/month=06/day=25",
            [source_row],
            "jsonl",
            manifest_prefix="market-data/rebuild-20260702-lazy-v1/manifest",
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

        with mock.patch.dict(os.environ, {"S3_BUCKET": "bucket", "S3_FINAL_PREFIX": "market-data/rebuild-20260702-lazy-v1/final", "S3_MANIFEST_PREFIX": "market-data/rebuild-20260702-lazy-v1/manifest"}):
            result = runner._run(record)

        self.assertEqual(result["source"], "s3-processed-replay")
        self.assertEqual(result["processedObjects"], [f"s3://bucket/{object_key}"])
        self.assertEqual(result["materializedRowCount"], 1)
        self.assertEqual(client.inserts[0][0], "chart_candles")

    def test_backfill_runner_replay_repair_rejects_raw_s3_backup_only(self):
        s3 = S3ObjectStore()
        upload_raw_page_to_s3(
            s3,
            "bucket",
            "market-data/rebuild-20260702-lazy-v1/raw/alpaca",
            "bars",
            "sip",
            "2026-06-25T13:30:00.000Z",
            "2026-06-25T13:31:00.000Z",
            1,
            {"AAPL": [alpaca_raw_bar("2026-06-25T13:30:00.000Z", open_price=10)]},
            manifest_prefix="market-data/rebuild-20260702-lazy-v1/manifest",
            price_adjustment="split",
            canonical_version="v2",
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

        with mock.patch.dict(os.environ, {"S3_BUCKET": "bucket", "S3_RAW_PREFIX": "market-data/rebuild-20260702-lazy-v1/raw/alpaca", "S3_MANIFEST_PREFIX": "market-data/rebuild-20260702-lazy-v1/manifest"}):
            with self.assertRaisesRegex(BackfillUnavailable, "No S3 final candle objects"):
                runner._run(record)
        self.assertEqual(client.inserts, [])

    def test_backfill_runner_gapfill_ignores_raw_s3_and_fetches_alpaca_when_processed_missing(self):
        s3 = S3ObjectStore()
        upload_raw_page_to_s3(
            s3,
            "bucket",
            "market-data/rebuild-20260702-lazy-v1/raw/alpaca",
            "bars",
            "sip",
            "2026-06-25T13:30:00.000Z",
            "2026-06-25T13:31:00.000Z",
            1,
            {"AAPL": [alpaca_raw_bar("2026-06-25T13:30:00.000Z", open_price=10)]},
            manifest_prefix="market-data/rebuild-20260702-lazy-v1/manifest",
            price_adjustment="split",
            canonical_version="v2",
        )
        record = {
            "requestId": "backfill:AAPL:1m:gapfill-raw",
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

        with mock.patch.dict(os.environ, {
            "S3_BUCKET": "bucket",
            "S3_FINAL_PREFIX": "market-data/rebuild-20260702-lazy-v1/final",
            "S3_RAW_PREFIX": "market-data/rebuild-20260702-lazy-v1/raw/alpaca",
            "S3_MANIFEST_PREFIX": "market-data/rebuild-20260702-lazy-v1/manifest",
        }):
            with mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", return_value=[
                alpaca_raw_bar("2026-06-25T13:30:00.000Z", open_price=10)
            ]) as fetch:
                result = runner._run(record)

        self.assertEqual(result["source"], "alpaca")
        self.assertEqual(result["processedRowCount"], 1)
        self.assertEqual(result["materializedRowCount"], 1)
        self.assertEqual(client.inserts[0][1][0]["source"], "alpaca.bars")
        fetch.assert_called_once()

    def test_backfill_runner_raw_s3_audit_does_not_skip_canonical_alpaca_backfill(self):
        s3 = S3ObjectStore()
        upload_raw_page_to_s3(
            s3,
            "bucket",
            "market-data/rebuild-20260702-lazy-v1/raw/alpaca",
            "bars",
            "sip",
            "2026-06-25T13:30:00.000Z",
            "2026-06-25T13:31:00.000Z",
            1,
            {"AAPL": [alpaca_raw_bar("2026-06-25T13:30:00.000Z", open_price=10)]},
            manifest_prefix="market-data/rebuild-20260702-lazy-v1/manifest",
            price_adjustment="split",
            canonical_version="v2",
        )
        raw_key = next(key for key in s3.objects if key.startswith("market-data/rebuild-20260702-lazy-v1/raw/alpaca/"))
        replay_path = f"s3://bucket/{raw_key}..1-raw-objects"
        record = {
            "requestId": "backfill:AAPL:1m:gapfill-raw-repeat",
            "symbol": "AAPL",
            "interval": "1m",
            "range": {"start": "2026-06-25T13:30:00.000Z", "end": "2026-06-25T13:31:00.000Z"},
            "jobType": "gapfill",
            "sourcePreference": "coverage-first",
        }
        client = AuditAwareClickHouseClient(materialized_paths={replay_path})
        runner = BackfillRunner(
            s3=s3,
            clickhouse_client=client,
            coverage_provider=StaticCoverageProvider({"rowCount": 0, "availableFrom": None, "availableTo": None}),
        )

        with mock.patch.dict(os.environ, {
            "S3_BUCKET": "bucket",
            "S3_RAW_PREFIX": "market-data/rebuild-20260702-lazy-v1/raw/alpaca",
            "S3_MANIFEST_PREFIX": "market-data/rebuild-20260702-lazy-v1/manifest",
        }):
            with mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", return_value=[
                alpaca_raw_bar("2026-06-25T13:30:00.000Z", open_price=10)
            ]) as fetch:
                result = runner._run(record)

        self.assertEqual(result["source"], "alpaca")
        self.assertEqual(result["processedRowCount"], 1)
        self.assertEqual(result["materializedRowCount"], 1)
        fetch.assert_called_once()

    def test_backfill_runner_replay_repair_ignores_live_raw_archive_s3(self):
        s3 = S3ObjectStore()
        envelope = build_raw_envelope(
            {"T": "b", "S": "AAPL", "t": "2026-06-25T13:30:00.000Z", "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1000},
            "sip",
        )
        archive_row = raw_archive_row(envelope)
        archive_row.update(canonical_candle_fields())
        object_key = flush_raw_buffer(
            s3,
            "bucket",
            raw_envelope_partition_key("market-data/rebuild-20260702-lazy-v1/raw/alpaca", archive_row),
            [archive_row],
            manifest_prefix="market-data/rebuild-20260702-lazy-v1/manifest",
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

        with mock.patch.dict(os.environ, {"S3_BUCKET": "bucket", "S3_RAW_PREFIX": "market-data/rebuild-20260702-lazy-v1/raw/alpaca", "S3_MANIFEST_PREFIX": "market-data/rebuild-20260702-lazy-v1/manifest"}):
            with self.assertRaisesRegex(BackfillUnavailable, "No S3 final candle objects"):
                runner._run(record)
        self.assertEqual(client.inserts, [])

    def test_backfill_runner_correction_replay_ignores_updated_bars_raw_s3(self):
        s3 = S3ObjectStore()
        upload_raw_page_to_s3(
            s3,
            "bucket",
            "market-data/rebuild-20260702-lazy-v1/raw/alpaca",
            "updated-bars",
            "sip",
            "2026-06-25T13:30:00.000Z",
            "2026-06-25T13:31:00.000Z",
            1,
            {"AAPL": [alpaca_raw_bar("2026-06-25T13:30:00.000Z", open_price=12)]},
            manifest_prefix="market-data/rebuild-20260702-lazy-v1/manifest",
            price_adjustment="split",
            canonical_version="v2",
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

        with mock.patch.dict(os.environ, {"S3_BUCKET": "bucket", "S3_RAW_PREFIX": "market-data/rebuild-20260702-lazy-v1/raw/alpaca", "S3_MANIFEST_PREFIX": "market-data/rebuild-20260702-lazy-v1/manifest"}):
            with self.assertRaisesRegex(BackfillUnavailable, "No S3 final candle objects"):
                runner._run(record)
        self.assertEqual(client.inserts, [])

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

        with mock.patch.dict(os.environ, {"S3_BUCKET": "bucket", "S3_FINAL_PREFIX": "market-data/rebuild-20260702-lazy-v1/final"}):
            with self.assertRaisesRegex(BackfillUnavailable, "No S3 final candle objects"):
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

        with mock.patch.dict(os.environ, {"S3_BUCKET": "bucket", "S3_FINAL_PREFIX": "market-data/rebuild-20260702-lazy-v1/final", "S3_PROCESSED_FORMAT": "jsonl"}):
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

    def test_backfill_runner_fetches_direct_hourly_bars_for_hourly_interval(self):
        record = {
            "requestId": "backfill:AAPL:1h:test",
            "symbol": "AAPL",
            "interval": "1h",
            "range": {"start": "2026-06-25T13:00:00.000Z", "end": "2026-06-25T16:00:00.000Z"},
            "jobType": "gapfill",
            "sourcePreference": "coverage-first",
        }
        calls = []

        def fake_fetch(symbol, start, end, feed, timeframe):
            calls.append({"symbol": symbol, "start": start, "end": end, "timeframe": timeframe})
            return [
                alpaca_raw_bar("2026-06-25T13:00:00.000Z", open_price=10, index=1),
                alpaca_raw_bar("2026-06-25T14:00:00.000Z", open_price=11, index=2),
            ]

        runner = BackfillRunner(
            s3=S3ObjectStore(),
            clickhouse_client=RecordingClickHouseClient(),
            coverage_provider=StaticCoverageProvider({"rowCount": 0, "availableFrom": None, "availableTo": None}),
        )

        with mock.patch.dict(os.environ, {"S3_BUCKET": "bucket", "S3_FINAL_PREFIX": "market-data/rebuild-20260702-lazy-v1/final", "S3_PROCESSED_FORMAT": "jsonl"}):
            with mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", side_effect=fake_fetch):
                result = runner._run(record)

        self.assertEqual(calls[0]["timeframe"], "1Hour")
        self.assertEqual(result["source"], "alpaca")
        self.assertIn("/interval=1h/", result["processedObjects"][0])

    def test_fetch_alpaca_bars_forces_split_adjustment_and_retries_rate_limits(self):
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
        self.assertEqual(calls[-1]["params"]["adjustment"], "split")
        self.assertEqual(calls[-1]["params"]["timeframe"], "1Min")

    def test_fetch_alpaca_bars_uses_crypto_endpoint_for_btcusd(self):
        """BTCUSD historical 요청이 주식 endpoint가 아니라 crypto bars endpoint로 나가는지 검증한다."""
        calls = []

        def fake_get(endpoint, headers, params, timeout):
            """requests.get mock으로 Alpaca crypto bars 응답 형태를 흉내 낸다."""
            calls.append({"endpoint": endpoint, "headers": headers, "params": dict(params), "timeout": timeout})
            return FakeHttpResponse(
                status_code=200,
                payload={"bars": {"BTC/USD": [alpaca_raw_bar("2026-06-28T13:30:00.000Z", open_price=61500)]}},
            )

        with mock.patch.dict(os.environ, {
            "ALPACA_CRYPTO_LOCATION": "us",
            "HISTORICAL_MAX_RETRIES": "1",
        }):
            with mock.patch("alfaka.common.secrets.load_alpaca_credentials", return_value=("key", "secret")):
                with mock.patch("requests.get", side_effect=fake_get):
                    rows = fetch_alpaca_bars(
                        "BTCUSD",
                        "2026-06-28T13:30:00.000Z",
                        "2026-06-28T13:31:00.000Z",
                        "crypto-us",
                        "1Min",
                    )

        self.assertEqual(len(rows), 1)
        self.assertEqual(calls[0]["endpoint"], "https://data.alpaca.markets/v1beta3/crypto/us/bars")
        self.assertEqual(calls[0]["params"]["symbols"], "BTC/USD")
        self.assertEqual(calls[0]["params"]["timeframe"], "1Min")
        self.assertNotIn("feed", calls[0]["params"])
        self.assertNotIn("adjustment", calls[0]["params"])

    def test_daily_backfill_repairs_split_day_daily_bar_outlier_from_1m(self):
        daily_bar = {
            "t": "2024-06-10T04:00:00Z",
            "o": 120.37,
            "h": 195.95,
            "l": 117.01,
            "c": 121.79,
            "v": 314162666,
            "n": 2798766,
            "vw": 121.141317,
        }
        minute_rows = [
            {"t": "2024-06-10T08:00:00Z", "o": 120.8, "h": 122.19, "l": 119.53, "c": 120.2, "v": 100, "n": 1, "vw": 120.5},
            {"t": "2024-06-10T16:54:00Z", "o": 122.9, "h": 123.1, "l": 122.71, "c": 122.92, "v": 200, "n": 2, "vw": 122.92},
            {"t": "2024-06-11T03:59:00Z", "o": 121.7, "h": 121.9, "l": 121.4, "c": 121.79, "v": 300, "n": 3, "vw": 121.75},
        ]

        with mock.patch("alfaka.backfill.runner.fetch_alpaca_bars", return_value=minute_rows) as fetch:
            repaired = repair_daily_bar_outliers("NVDA", [daily_bar], "sip")

        self.assertEqual(fetch.call_args.args[4], "1Min")
        self.assertEqual(repaired[0]["o"], 120.37)
        self.assertEqual(repaired[0]["h"], 123.1)
        self.assertEqual(repaired[0]["l"], 117.01)
        self.assertEqual(repaired[0]["c"], 121.79)
        self.assertEqual(repaired[0]["v"], 314162666)
        self.assertEqual(repaired[0]["_repairSource"], "alpaca.1m-bars")

        candle = raw_bar_to_processed_candle("NVDA", repaired[0], feed="sip", interval="1D")
        self.assertEqual(candle["source"], "alpaca.dailyBars.repairedFrom1m")
        self.assertEqual(candle["correctionType"], "REPAIRED")
        self.assertIn("/repair=1m", candle["sourceEventId"])

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
        self.assertIn("canonical_version = 'v2'", query)
        self.assertIn("price_adjustment IN ('split', 'live')", query)
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

    def test_gapfill_ranges_include_weekend_minutes_for_crypto_calendar(self):
        """crypto 24/7 캘린더에서는 주말 분봉도 gapfill 대상에 포함되는지 검증한다."""
        ranges = detect_gapfill_ranges(
            "2026-06-28T13:30:00.000Z",
            "2026-06-28T13:33:00.000Z",
            "1m",
            actual_timestamps=["2026-06-28T13:31:00.000Z"],
            calendar=TradingCalendar.crypto_24x7(),
        )

        self.assertEqual(len(ranges), 2)
        self.assertEqual(ranges[0].start, "2026-06-28T13:30:00.000Z")
        self.assertEqual(ranges[0].end, "2026-06-28T13:31:00.000Z")
        self.assertEqual(ranges[0].missingCount, 1)
        self.assertEqual(ranges[1].start, "2026-06-28T13:32:00.000Z")
        self.assertEqual(ranges[1].end, "2026-06-28T13:33:00.000Z")
        self.assertEqual(ranges[1].missingCount, 1)

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
        self.assertEqual(first.start, "2026-06-11T14:30:00.000Z")
        self.assertEqual(first.end, "2026-06-25T14:30:00.000Z")

    def test_default_backfill_range_uses_interval_groups(self):
        intraday = default_backfill_range(now="2026-06-25T14:30:11.123Z", interval="1m")
        derived_intraday = default_backfill_range(now="2026-06-25T14:30:11.123Z", interval="5m")
        daily = default_backfill_range(now="2026-06-25T14:30:11.123Z", interval="1D")

        self.assertEqual(intraday.start, "2026-06-11T14:30:00.000Z")
        self.assertEqual(derived_intraday.start, "2026-06-11T14:30:00.000Z")
        self.assertEqual(daily.start, "2020-06-26T14:30:00.000Z")

    def test_explicit_backfill_lookback_hours_is_still_supported_for_direct_calls(self):
        value = default_backfill_range(now="2026-06-25T14:30:11.123Z", interval="1D", lookback_hours=24)

        self.assertEqual(value.start, "2026-06-24T14:30:00.000Z")

    def test_chart_candle_limit_defaults_to_interval_visible_bars(self):
        self.assertEqual(candle_count_for_24h("1m"), 120)
        self.assertEqual(candle_count_for_24h("5m"), 120)
        self.assertEqual(candle_count_for_24h("10m"), 120)
        self.assertEqual(candle_count_for_24h("1h"), 120)
        self.assertEqual(candle_count_for_24h("4h"), 120)
        self.assertEqual(candle_count_for_24h("1d"), 120)
        self.assertEqual(candle_count_for_24h("1W"), 104)
        self.assertEqual(candle_count_for_24h("1M"), 36)
        self.assertEqual(historical_target_bars("1m"), 589680)
        self.assertEqual(historical_target_bars("1h"), 9828)
        self.assertEqual(historical_target_bars("4h"), 2457)
        self.assertEqual(historical_target_bars("1D"), 1512)
        self.assertEqual(historical_target_bars("1M"), 72)
        self.assertEqual(candle_count_for_1y("1m"), 589680)
        self.assertEqual(resolve_candle_limit("1m", None), 120)
        self.assertEqual(resolve_candle_limit("1m", 9999), 9999)
        self.assertEqual(resolve_candle_limit("1m", 999999), 589680)
        self.assertEqual(resolve_candle_limit("1M", 999999), 72)
        self.assertEqual(redis_closed_candle_cap("1m"), 780)
        self.assertEqual(redis_closed_candle_cap("5m"), 156)
        self.assertEqual(redis_closed_candle_cap("10m"), 78)
        self.assertEqual(redis_closed_candle_cap("1h"), 13)
        self.assertEqual(redis_closed_candle_cap("4h"), 4)
        self.assertEqual(redis_closed_candle_cap("1D"), 1512)
        self.assertEqual(redis_closed_candle_cap("1W"), 312)
        self.assertEqual(redis_closed_candle_cap("1M"), 72)

    def test_clickhouse_provider_uses_database_override(self):
        provider = ClickHouseMarketDataProvider(database="custom_market_data")

        self.assertEqual(provider.table("symbols"), "custom_market_data.symbols")
        with self.assertRaises(ValueError):
            provider.table("bad-table-name")

    def test_clickhouse_provider_query_uses_short_serving_timeout(self):
        calls = []

        class FakeResponse:
            status_code = 200
            text = '{"ok":1}\n'

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse()

        provider = ClickHouseMarketDataProvider(
            url="http://clickhouse:8123",
            database="market_data",
            user="alfaka",
            password="secret",
        )
        with mock.patch.dict(os.environ, {"CLICKHOUSE_PROVIDER_TIMEOUT_SECONDS": "0.45"}):
            with mock.patch("requests.post", side_effect=fake_post):
                rows = provider.query_json_each_row("SELECT 1", {"symbol": "AAPL"})

        self.assertEqual(rows, [{"ok": 1}])
        self.assertEqual(calls[0][0], "http://clickhouse:8123")
        self.assertEqual(calls[0][1]["timeout"], 0.45)
        self.assertEqual(calls[0][1]["params"]["param_symbol"], "AAPL")

    def test_storage_clickhouse_client_query_params_use_typed_http_values(self):
        calls = []

        class FakeResponse:
            status_code = 200
            text = '{"ok":1}\n'

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse()

        client = ClickHouseHttpClient(
            url="http://clickhouse:8123",
            database="market_data",
            user="alfaka",
            password="secret",
        )
        with mock.patch("requests.post", side_effect=fake_post):
            rows = client.query_json_each_row(
                "SELECT 1",
                {
                    "symbol": "AAPL",
                    "date": "2026-07-05",
                    "locale": "ko-KR",
                    "symbols": ["AAPL", "O'Reilly"],
                    "limit": 50,
                },
            )

        self.assertEqual(rows, [{"ok": 1}])
        params = calls[0][1]["params"]
        self.assertEqual(params["param_symbol"], "AAPL")
        self.assertEqual(params["param_date"], "2026-07-05")
        self.assertEqual(params["param_locale"], "ko-KR")
        self.assertEqual(params["param_symbols"], "['AAPL','O\\'Reilly']")
        self.assertEqual(params["param_limit"], "50")

    def test_storage_clickhouse_client_execute_params_use_typed_http_values(self):
        calls = []

        class FakeResponse:
            status_code = 200
            text = ""

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse()

        client = ClickHouseHttpClient(
            url="http://clickhouse:8123",
            database="market_data",
            user="alfaka",
            password="secret",
        )
        with mock.patch("requests.post", side_effect=fake_post):
            client.execute("ALTER TABLE market_data.news_intelligence DELETE WHERE locale = {locale:String}", {"locale": "ko-KR"})

        self.assertEqual(calls[0][1]["params"]["param_locale"], "ko-KR")

    def test_clickhouse_provider_retries_transient_timeout(self):
        import requests

        calls = []

        class FakeResponse:
            status_code = 200
            text = '{"ok":1}\n'

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            if len(calls) == 1:
                raise requests.exceptions.Timeout("cold query")
            return FakeResponse()

        provider = ClickHouseMarketDataProvider(
            url="http://clickhouse:8123",
            database="market_data",
            user="alfaka",
            password="secret",
        )
        with mock.patch.dict(os.environ, {
            "CLICKHOUSE_PROVIDER_TIMEOUT_SECONDS": "0.45",
            "CLICKHOUSE_PROVIDER_RETRY_ATTEMPTS": "2",
        }):
            with mock.patch("requests.post", side_effect=fake_post):
                with mock.patch("alfaka.serving.clickhouse_provider.time.sleep") as sleep:
                    rows = provider.query_json_each_row("SELECT 1", {"symbol": "AAPL"})

        self.assertEqual(rows, [{"ok": 1}])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1]["timeout"], 0.45)
        self.assertEqual(calls[1][1]["params"]["param_symbol"], "AAPL")
        sleep.assert_called_once()

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

        query = provider.queries[-1][0]
        self.assertIn("row_number() OVER", query)
        self.assertIn("PARTITION BY symbol, if(interval = '1d', '1D', interval), event_time", query)
        self.assertIn("multiIf(price_adjustment = 'split', 1, 0) DESC", query)
        self.assertIn("inserted_at DESC", query)
        self.assertIn("ifNull(source_event_id, '') DESC", query)
        self.assertIn("canonical_version = 'v2'", query)
        self.assertIn("price_adjustment IN ('split', 'live')", query)
        self.assertEqual(candles[-1]["timestamp"], "2026-06-25T13:30:00.000Z")

    def test_clickhouse_daily_direct_candles_keep_historical_canonical_filter(self):
        provider = RecordingClickHouseProviderForAggregation([])

        provider.candles("AAPL", "1D", 5)

        query = provider.queries[-1][0]
        self.assertIn("AND interval IN ('1D', '1d')", query)
        self.assertIn("price_adjustment IN ('split')", query)
        self.assertNotIn("'live'", query)

    def test_clickhouse_intraday_aggregation_includes_live_minute_source(self):
        provider = RecordingClickHouseProviderForAggregation([])

        provider.candles("AAPL", "5m", 5)

        direct_query = provider.queries[0][0]
        aggregate_query = provider.queries[1][0]
        self.assertIn("interval = {interval:String}", direct_query)
        self.assertIn("price_adjustment IN ('split')", direct_query)
        self.assertIn("AND interval = '1m'", aggregate_query)
        self.assertIn("price_adjustment IN ('split', 'live')", aggregate_query)

    def test_clickhouse_provider_does_not_weekday_filter_crypto_candles(self):
        """BTCUSD 조회 SQL에는 주식용 평일 필터가 들어가지 않는지 검증한다."""
        rows = [{
            "timestamp": "2026-06-28T13:30:00.000Z",
            "open": 61500,
            "high": 61600,
            "low": 61400,
            "close": 61550,
            "volume": 0.25,
            "isClosed": 1,
            "source": "alpaca.bars",
            "feed": "crypto",
        }]
        provider = RecordingClickHouseProviderForAggregation(rows)

        candles = provider.candles("BTCUSD", "1m", 5)

        query = provider.queries[-1][0]
        self.assertNotIn("toDayOfWeek(event_time) BETWEEN 1 AND 5", query)
        self.assertEqual(candles[-1]["timestamp"], "2026-06-28T13:30:00.000Z")

    def test_stock_clickhouse_query_filters_historical_extended_hours(self):
        provider = RecordingClickHouseProviderForAggregation([])

        with mock.patch(
            "alfaka.serving.clickhouse_provider.visible_extended_session_windows",
            return_value=[
                (
                    "after",
                    datetime(2026, 7, 5, 20, 0, tzinfo=timezone.utc),
                    datetime(2026, 7, 6, 0, 0, tzinfo=timezone.utc),
                ),
                (
                    "overnight",
                    datetime(2026, 7, 6, 0, 0, tzinfo=timezone.utc),
                    datetime(2026, 7, 6, 8, 0, tzinfo=timezone.utc),
                ),
            ],
        ):
            provider.candles("AAPL", "1m", 5)

        query = provider.queries[0][0]
        self.assertIn("market_session = 'regular'", query)
        self.assertIn("market_session = 'after'", query)
        self.assertIn("market_session = 'overnight'", query)
        self.assertIn("2026-07-05T20:00:00.000Z", query)
        self.assertIn("2026-07-06T00:00:00.000Z", query)
        self.assertIn("2026-07-06T08:00:00.000Z", query)

    def test_stock_chart_visibility_keeps_adjacent_extended_session(self):
        candles = [
            {"timestamp": "2026-07-08T19:30:00.000Z", "marketSession": "regular"},
            {"timestamp": "2026-07-08T21:30:00.000Z", "marketSession": "after"},
            {"timestamp": "2026-07-09T02:30:00.000Z", "marketSession": "overnight"},
            {"timestamp": "2026-07-07T21:30:00.000Z", "marketSession": "after"},
        ]

        visible = filter_stock_chart_candles(
            candles,
            now=datetime(2026, 7, 9, 5, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(
            [candle["timestamp"] for candle in visible],
            [
                "2026-07-08T19:30:00.000Z",
                "2026-07-08T21:30:00.000Z",
                "2026-07-09T02:30:00.000Z",
            ],
        )

    def test_crypto_weekend_candle_is_valid_and_keeps_decimal_sizes(self):
        """crypto 주말 캔들과 소수 단위 거래량/수량이 적재 변환에서 유지되는지 검증한다."""
        self.assertIsNone(invalid_candle_reason({
            "symbol": "BTCUSD",
            "interval": "1m",
            "timestamp": "2026-06-28T13:30:00.000Z",
        }))
        self.assertIn("weekday market sessions", invalid_candle_reason({
            "symbol": "AAPL",
            "interval": "1m",
            "timestamp": "2026-06-28T13:30:00.000Z",
        }))

        trade_row = trade_to_clickhouse_row({
            "eventType": "TRADE",
            "symbol": "BTCUSD",
            "tradeId": 1,
            "price": 61500,
            "size": 0.013,
            "timestamp": "2026-06-28T13:30:00.000Z",
        })
        candle_row = candle_to_clickhouse_row({
            "eventType": "CANDLE",
            "symbol": "BTCUSD",
            "interval": "1m",
            "timestamp": "2026-06-28T13:30:00.000Z",
            "open": 61500,
            "high": 61600,
            "low": 61400,
            "close": 61550,
            "volume": 0.013,
        })

        self.assertEqual(trade_row["size"], 0.013)
        self.assertEqual(candle_row["volume"], 0.013)
        self.assertEqual(candle_row["market_session"], "crypto")

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

        candles = provider.aggregated_minute_candles("AAPL", "5m", 5)

        self.assertIn("AND interval = '1m'", provider.queries[0][0])
        self.assertIn("row_number() OVER", provider.queries[0][0])
        self.assertEqual(candles[-1]["interval"], "5m")
        self.assertEqual(candles[-1]["ma5"], 3.0)

        provider.aggregated_minute_candles("AAPL", "1h", 5)
        self.assertIn("INTERVAL 60 minute", provider.queries[-1][0])

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

        weekly = provider.aggregated_daily_candles("AAPL", "1W", 5)
        monthly = provider.aggregated_daily_candles("AAPL", "1M", 5)

        self.assertIn("AND interval IN ('1D', '1d')", provider.queries[0][0])
        self.assertIn("AND interval IN ('1D', '1d')", provider.queries[1][0])
        self.assertIn("row_number() OVER", provider.queries[0][0])
        self.assertIn("row_number() OVER", provider.queries[1][0])
        self.assertEqual(weekly[-1]["interval"], "1W")
        self.assertEqual(monthly[-1]["interval"], "1M")
        self.assertEqual(weekly[-1]["ma5"], 3.0)

    def test_clickhouse_prefers_direct_interval_rows_before_source_aggregation(self):
        rows = [
            {
                "timestamp": f"2026-06-25T{13 + index:02d}:00:00.000Z",
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

        candles = provider.candles("AAPL", "1h", 5)

        self.assertIn("interval = {interval:String}", provider.queries[0][0])
        self.assertEqual(provider.queries[0][1]["interval"], "1h")
        self.assertEqual(len(provider.queries), 1)
        self.assertEqual(candles[-1]["interval"], "1h")

    def test_clickhouse_recomputes_stored_direct_interval_moving_averages(self):
        rows = [
            {
                "timestamp": f"2026-06-25T{13 + index:02d}:00:00.000Z",
                "open": index + 1,
                "high": index + 1,
                "low": index + 1,
                "close": index + 1,
                "volume": 100 + index,
                "isClosed": 1,
                "ma5": 999,
                "source": "alpaca.bars",
                "feed": "sip",
            }
            for index in range(5)
        ]
        provider = RecordingClickHouseProviderForAggregation(rows)

        candles = provider.candles("AAPL", "1h", 5)

        self.assertEqual(candles[-1]["interval"], "1h")
        self.assertEqual(candles[-1]["ma5"], 3.0)

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

        with mock.patch("alfaka.serving.provider.target_range_from_for_interval", return_value="2020-07-01T00:00:00.000Z"):
            payload = provider.candle_snapshot("AAPL", "1M", 80)

        self.assertEqual(clickhouse.calls[-1]["from_time"], "2020-07-01T00:00:00.000Z")
        self.assertEqual([candle["timestamp"] for candle in payload["candles"]], [
            "2022-01-01T00:00:00.000Z",
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

        with mock.patch("alfaka.serving.provider.target_range_from_for_interval", return_value="2020-07-01T00:00:00.000Z"):
            payload = provider.candle_snapshot(
                "AAPL",
                "1M",
                80,
                before="2023-01-01T00:00:00.000Z",
                from_time="2020-01-01T00:00:00.000Z",
            )

        self.assertEqual(clickhouse.calls[-1]["from_time"], "2020-07-01T00:00:00.000Z")
        self.assertEqual([candle["timestamp"] for candle in payload["candles"]], [
            "2022-01-01T00:00:00.000Z",
        ])

    def test_chart_snapshot_before_pagination_can_read_before_target_floor(self):
        rows = [
            {
                "timestamp": "2018-01-01T00:00:00.000Z",
                "interval": "1M",
                "open": 1,
                "high": 2,
                "low": 1,
                "close": 2,
                "volume": 100,
                "isClosed": True,
            },
            {
                "timestamp": "2019-01-01T00:00:00.000Z",
                "interval": "1M",
                "open": 2,
                "high": 3,
                "low": 2,
                "close": 3,
                "volume": 110,
                "isClosed": True,
            },
            {
                "timestamp": "2024-01-01T00:00:00.000Z",
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

        with mock.patch("alfaka.serving.provider.target_range_from_for_interval", return_value="2020-07-01T00:00:00.000Z"):
            payload = provider.candle_snapshot(
                "AAPL",
                "1M",
                80,
                before="2020-07-01T00:00:00.000Z",
            )

        self.assertIsNone(clickhouse.calls[-1]["from_time"])
        self.assertEqual([candle["timestamp"] for candle in payload["candles"]], [
            "2018-01-01T00:00:00.000Z",
            "2019-01-01T00:00:00.000Z",
        ])

    def test_chart_snapshot_before_pagination_rejects_stale_gap_jump(self):
        rows = [
            {
                "timestamp": "2025-09-15T04:00:00.000Z",
                "interval": "1D",
                "open": 175,
                "high": 178,
                "low": 174,
                "close": 177,
                "volume": 147_000_000,
                "isClosed": True,
                "marketSession": "regular",
            },
            {
                "timestamp": "2025-10-10T04:00:00.000Z",
                "interval": "1D",
                "open": 193,
                "high": 195,
                "low": 182,
                "close": 183,
                "volume": 268_000_000,
                "isClosed": True,
                "marketSession": "regular",
            },
        ]
        clickhouse = RecordingRangeClickHouseProvider(rows)
        provider = MarketDataProvider(
            redis_provider=FakeRedisProvider(),
            clickhouse_provider=clickhouse,
        )

        with mock.patch("alfaka.serving.provider.target_range_from_for_interval", return_value="2026-05-23T04:00:00.000Z"):
            payload = provider.candle_snapshot(
                "NVDA",
                "1D",
                20,
                before="2026-07-02T04:00:00.000Z",
                ma_windows=(),
            )

        self.assertEqual(clickhouse.calls[-1]["from_time"], "2026-05-23T04:00:00.000Z")
        self.assertEqual(payload["targetRangeFrom"], "2026-05-23T04:00:00.000Z")
        self.assertEqual(payload["targetRangeTo"], "2026-07-02T04:00:00.000Z")
        self.assertEqual(payload["candles"], [])

    def test_chart_snapshot_explicit_range_can_read_before_target_floor(self):
        rows = [
            {
                "timestamp": "2020-01-02T14:30:00.000Z",
                "interval": "1m",
                "open": 1,
                "high": 2,
                "low": 1,
                "close": 2,
                "volume": 100,
                "isClosed": True,
            },
            {
                "timestamp": "2020-01-02T14:31:00.000Z",
                "interval": "1m",
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

        with mock.patch("alfaka.serving.provider.target_range_from_for_interval", return_value="2020-07-01T00:00:00.000Z"):
            payload = provider.candle_snapshot(
                "AAPL",
                "1m",
                2,
                from_time="2020-01-02T00:00:00.000Z",
                to_time="2020-01-03T00:00:00.000Z",
            )

        self.assertLess(clickhouse.calls[-1]["from_time"], "2020-01-02T00:00:00.000Z")
        self.assertEqual([candle["timestamp"] for candle in payload["candles"]], [
            "2020-01-02T14:30:00.000Z",
            "2020-01-02T14:31:00.000Z",
        ])

    def test_has_more_before_uses_available_history_before_target_floor(self):
        self.assertFalse(has_more_before_target(
            "2020-07-01T00:00:00.000Z",
            "2021-06-01T00:00:00.000Z",
            "2020-07-01T05:00:00.000Z",
        ))
        self.assertTrue(has_more_before_target(
            "2020-07-01T00:00:00.000Z",
            "2016-01-04T05:00:00.000Z",
            "2020-07-03T13:19:31.000Z",
        ))
        self.assertTrue(has_more_before_target(
            "2023-08-01T00:00:00.000Z",
            "2021-06-01T00:00:00.000Z",
            "2020-07-01T05:00:00.000Z",
        ))

    def test_has_more_before_treats_target_boundary_gap_as_terminal(self):
        self.assertFalse(has_more_before_target(
            "2020-07-03T13:30:00.000Z",
            "2021-06-01T00:00:00.000Z",
            "2020-07-01T00:00:00.000Z",
        ))

    def test_has_more_before_keeps_real_old_history_gap_repairable(self):
        self.assertTrue(has_more_before_target(
            "2020-07-07T13:30:00.000Z",
            "2021-06-01T00:00:00.000Z",
            "2020-07-01T00:00:00.000Z",
        ))

    def test_has_more_before_keeps_late_available_source_repairable(self):
        self.assertTrue(has_more_before_target(
            "2024-06-03T00:00:00.000Z",
            "2024-06-03T04:00:00.000Z",
            "2020-07-01T00:00:00.000Z",
        ))

    def test_has_more_before_keeps_intraday_pagination_open_until_historical_floor(self):
        self.assertTrue(has_more_before_target(
            "2026-07-09T08:50:00.000Z",
            "2026-07-09T08:50:00.000Z",
            "2026-07-09T02:49:53.000Z",
            interval="1m",
        ))
        self.assertFalse(has_more_before_target(
            "2020-07-01T00:00:00.000Z",
            "2020-07-01T00:00:00.000Z",
            "2020-07-01T00:00:00.000Z",
            interval="1m",
        ))

    def test_intraday_target_floor_uses_requested_visible_window(self):
        self.assertEqual(
            target_range_from_for_interval("1m", "2026-06-30T11:15:09.000Z"),
            "2026-06-30T03:15:09.000Z",
        )
        self.assertEqual(
            target_range_from_for_interval("5m", "2026-06-30T11:15:09.000Z"),
            "2026-06-28T19:15:09.000Z",
        )

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
        self.assertNotIn("market_session = 'regular'", provider.queries[0][0])
        self.assertIn("'regular' AS marketSession", provider.queries[0][0])
        self.assertIn("row_number() OVER", provider.queries[0][0])
        self.assertEqual(daily[-1]["interval"], "1D")
        self.assertEqual(daily[-1]["ma5"], 3.0)

    def test_clickhouse_weekly_monthly_aggregation_does_not_filter_daily_sessions(self):
        provider = RecordingClickHouseProviderForAggregation([])

        provider.candles("AAPL", "1W", 5)

        query = provider.queries[-1][0]
        self.assertIn("AND interval IN ('1D', '1d')", query)
        self.assertNotIn("market_session = 'regular'", query)
        self.assertIn("'regular' AS marketSession", query)

    def test_clickhouse_hot_ranking_uses_deduped_minute_source(self):
        provider = RecordingClickHouseProviderForAggregation([])

        provider.hot_symbols_by_dollar_volume(["AAPL", "MSFT"])

        query = provider.queries[0][0]
        self.assertIn("row_number() OVER", query)
        self.assertIn("AND interval = '1m'", query)
        self.assertIn("price_adjustment IN ('split', 'live')", query)
        self.assertIn("latest_session_date", query)
        self.assertIn("event_time >= subtractDays(now(), {lookbackDays:UInt32})", query)
        self.assertEqual(provider.queries[0][1]["lookbackDays"], 14)

    def test_clickhouse_daily_hot_fallback_keeps_historical_canonical_filter(self):
        provider = RecordingClickHouseProviderForAggregation([])

        provider._hot_symbols_by_interval_dollar_volume(["AAPL", "MSFT"], interval="1D")

        query = provider.queries[0][0]
        self.assertIn("AND interval IN ('1D', '1d')", query)
        self.assertIn("price_adjustment IN ('split')", query)
        self.assertNotIn("'live'", query)

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

    def test_clickhouse_loader_routes_market_status_to_event_and_status_tables(self):
        client = RecordingClickHouseClient()
        payload = {
            "eventType": "MARKET_STATUS",
            "layer": "events",
            "eventTime": "2026-06-25T13:30:00.000Z",
            "symbol": "_MARKET",
            "statusType": "trading",
            "status": "active",
            "source": "alpaca",
            "feed": "sip",
            "feedProfile": "sip",
            "marketSession": "regular",
            "sourceEventId": "status-1",
            "raw": {"T": "s"},
        }

        event_row = market_event_to_clickhouse_row(payload)
        load_payload(client, payload)

        self.assertIsNone(event_row["symbol"])
        self.assertEqual(event_row["event_type"], "MARKET_STATUS")
        self.assertEqual(client.inserts[0][0], "market_events")
        self.assertEqual(client.inserts[0][1][0]["event_type"], "MARKET_STATUS")
        self.assertEqual(client.inserts[1][0], "market_status_events")
        self.assertEqual(client.inserts[1][1][0]["status"], "active")

    def test_s3_partition_keys_and_storage_rows_follow_contract(self):
        final_prefix = "market-data/rebuild-20260702-lazy-v1/final"
        candle_key = s3_partition_key(final_prefix, {
            "eventType": "CANDLE",
            "timestamp": "2026-01-02T10:15:00.000Z",
            "symbol": "AAPL",
            "interval": "1m",
        })
        tick_key = s3_partition_key(final_prefix, {
            "eventType": "TRADE",
            "timestamp": "2026-01-02T10:15:00.000Z",
            "symbol": "AAPL",
        })
        quote_key = s3_partition_key(final_prefix, {
            "eventType": "QUOTE",
            "timestamp": "2026-01-02T10:15:00.000Z",
            "symbol": "AAPL",
        })
        profile_key = s3_partition_key(final_prefix, {
            "eventType": "VOLUME_PROFILE_BIN",
            "eventMinute": "2026-01-02T10:15:00.000Z",
            "symbol": "AAPL",
        })
        storage_row = normalize_storage_row({"ma": {"ma5": 1.0}, "raw": {"T": "s"}})

        self.assertEqual(candle_key, "market-data/rebuild-20260702-lazy-v1/final/candles/feed=unknown/interval=1m/symbol=AAPL/year=2026/month=01/day=02")
        self.assertEqual(tick_key, "market-data/rebuild-20260702-lazy-v1/final/trades/symbol=AAPL/year=2026/month=01/day=02/feed=unknown")
        self.assertEqual(quote_key, "market-data/rebuild-20260702-lazy-v1/final/quotes/symbol=AAPL/year=2026/month=01/day=02/feed=unknown")
        self.assertEqual(profile_key, "market-data/rebuild-20260702-lazy-v1/final/volume-profile-bins/timeBucket=1m/symbol=AAPL/year=2026/month=01/day=02")
        self.assertEqual(storage_row["ma5"], 1.0)
        self.assertIsNone(storage_row["ma20"])
        self.assertEqual(storage_row["raw"], "{\"T\":\"s\"}")

    def test_s3_sink_default_topics_exclude_live_candle_topic(self):
        topics = processed_topics_from_env({})

        self.assertEqual(topics, [
            "market.layer.candles.1m.closed.v1",
            "market.layer.candles.5m.closed.v1",
            "market.layer.candles.10m.closed.v1",
            "market.layer.candles.1h.closed.v1",
            "market.layer.candles.4h.closed.v1",
            "market.layer.candles.1d.closed.v1",
            "market.layer.candles.1w.closed.v1",
            "market.layer.candles.1mo.closed.v1",
            "market.layer.events.v1",
        ])
        self.assertNotIn("market.layer.candles.live.v1", topics)

    def test_s3_sink_commits_after_successful_flush(self):
        class OneBatchConsumer:
            def __init__(self):
                self.polls = 0
                self.commits = 0

            def poll(self, timeout_ms=1000):
                self.polls += 1
                if self.polls > 1:
                    raise KeyboardInterrupt()
                return {None: [types.SimpleNamespace(value={
                    "eventType": "TRADE",
                    "timestamp": "2026-01-02T10:15:00.000Z",
                    "symbol": "AAPL",
                    "price": 1,
                })]}

            def commit(self):
                self.commits += 1

        consumer = OneBatchConsumer()
        s3 = RecordingS3()

        run_processed_s3_sink(
            consumer,
            s3,
            "bucket",
            "market-data/rebuild-20260702-lazy-v1/final",
            "jsonl",
            flush_count=1,
            flush_interval_seconds=60,
            enable_auto_commit=False,
        )

        self.assertGreaterEqual(consumer.commits, 1)
        self.assertEqual(len(s3.objects), 1)

    def test_local_and_aws_topic_lists_cover_same_market_contract(self):
        root = Path(__file__).resolve().parents[3]
        required_topics = {
            "market.input.realtime.trades.v1",
            "market.input.realtime.quotes.v1",
            "market.input.realtime.events.v1",
            "market.input.realtime.bars.1m.v1",
            "market.input.realtime.updated-bars.1m.v1",
            "market.input.realtime.daily-bars.v1",
            "market.realtime.ticks.to.1m.v1",
            "market.realtime.ticks.to.5m.v1",
            "market.realtime.ticks.to.10m.v1",
            "market.realtime.ticks.to.1d.v1",
            "market.realtime.ticks.to.1w.v1",
            "market.realtime.ticks.to.1mo.v1",
            "market.layer.candles.closed.v1",
            "market.layer.candles.1m.closed.v1",
            "market.layer.candles.5m.closed.v1",
            "market.layer.candles.10m.closed.v1",
            "market.layer.candles.1h.closed.v1",
            "market.layer.candles.4h.closed.v1",
            "market.layer.candles.1d.closed.v1",
            "market.layer.candles.1w.closed.v1",
            "market.layer.candles.1mo.closed.v1",
            "market.layer.trades.v1",
            "market.layer.events.v1",
            "market.news.alpaca.v1",
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
        docker_compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
        kafka_init_job = (root / "infra/k8s/base/platform/kafka-topic-init-job.yaml").read_text(encoding="utf-8")

        self.assertTrue(required_topics.issubset(aws_topics))
        for topic in required_topics:
            self.assertIn(topic, local_script)
        self.assertIn("hot_topics", local_script)
        self.assertIn("--topic \"${topic}\"", local_script)
        self.assertIn('create_topic "${topic}" 12', local_script)
        self.assertIn("hot_topics", docker_compose)
        self.assertIn("--partitions 12", docker_compose)
        self.assertIn("--partitions 12", kafka_init_job)

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

    def test_symbol_registry_includes_configured_btcusd_metadata(self):
        """기본 on-demand 설정에서도 extraSymbols의 BTCUSD 메타데이터가 노출되는지 검증한다."""
        registry = SymbolRegistry(
            clickhouse_provider=FakeClickHouseProvider(symbols={}),
            redis_provider=FakeRedisProvider(symbol_metadata={}),
        )

        detail = registry.detail("BTCUSD")
        search_results = registry.search("btc", 5)

        self.assertEqual(detail["symbol"], "BTCUSD")
        self.assertEqual(detail["assetClass"], "crypto")
        self.assertEqual(detail["market"], "CRYPTO")
        self.assertFalse(detail["tradable"])
        self.assertEqual([item["symbol"] for item in search_results], ["BTCUSD"])

    def test_on_demand_config_has_no_default_symbols_but_accepts_explicit_seed(self):
        """on-demand 수집은 기본 구독을 비우되 UI 검색용 extraSymbols는 유지하는지 검증한다."""
        previous_universe = os.environ.get("ALPACA_UNIVERSE")
        previous = os.environ.get("ALPACA_SYMBOLS")
        previous_collection_source = os.environ.get("ALPACA_COLLECTION_SYMBOL_SOURCE")
        os.environ["ALPACA_UNIVERSE"] = ""
        os.environ["ALPACA_SYMBOLS"] = "IBM,ORCL"
        os.environ["ALPACA_COLLECTION_SYMBOL_SOURCE"] = "defaultSymbols"
        try:
            self.assertEqual(configured_seed_symbols(), ["IBM", "ORCL"])
            self.assertEqual(configured_collection_symbols(), [])

            registry = SymbolRegistry(
                clickhouse_provider=FakeClickHouseProvider(symbols={}),
                redis_provider=FakeRedisProvider(symbol_metadata={}),
            )

            with self.assertRaises(LookupError):
                registry.detail("IBM")
            self.assertEqual(registry.search("ibm", 5), [])
            self.assertEqual([item["symbol"] for item in registry.search("", 5)], ["BTCUSD", "XLV"])
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

    def test_symbol_search_uses_provider_results_when_no_default_universe_exists(self):
        class SearchClickHouseProvider(FakeClickHouseProvider):
            def search_symbols(self, query, limit):
                records = [
                    {"symbol": "IBM", "name": "International Business Machines Corporation"},
                    {"symbol": "ORCL", "name": "Oracle Corporation"},
                ]
                normalized = query.upper()
                return [
                    record
                    for record in records
                    if normalized in f"{record['symbol']} {record['name']}".upper()
                ][:limit]

        previous_universe = os.environ.get("ALPACA_UNIVERSE")
        os.environ["ALPACA_UNIVERSE"] = ""
        try:
            registry = SymbolRegistry(
                clickhouse_provider=SearchClickHouseProvider(symbols={}),
                redis_provider=FakeRedisProvider(symbol_metadata={}),
            )

            results = registry.search("ib", 10)
        finally:
            if previous_universe is None:
                os.environ.pop("ALPACA_UNIVERSE", None)
            else:
                os.environ["ALPACA_UNIVERSE"] = previous_universe

        self.assertEqual([item["symbol"] for item in results], ["IBM"])

    def test_trade_subscription_plan_prioritizes_active_watchlist_then_hot(self):
        plan = resolve_trade_subscription_plan(
            active_symbols=["ibm", "ORCL"],
            watchlist_symbols=["INTC", "ORCL"],
            hot_symbols=["F", "INTC", "BAD!"],
            max_symbols=4,
        )

        self.assertEqual(plan["symbols"], ["IBM", "ORCL", "INTC", "F"])
        self.assertEqual(plan["tiersBySymbol"]["ORCL"], ["active", "watchlist"])
        self.assertEqual(plan["tiersBySymbol"]["INTC"], ["watchlist", "hot"])
        self.assertEqual(plan["counts"]["resolved"], 4)

    def test_ingestor_reads_trade_symbols_from_subscription_control_plane(self):
        redis = MemoryRedis()
        keys = RedisKeyBuilder()
        redis.sadd(keys.subscription_symbols(), "IBM", "ORCL")
        redis.hset(keys.subscription_symbol("IBM"), {"layers": "trades,quotes"})
        redis.hset(keys.subscription_symbol("ORCL"), {"layers": "candles"})

        self.assertEqual(read_trade_subscription_symbols(redis), {"IBM"})

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
            self.assertEqual(config["universeRegistryPath"], "sp500-universe.json")
        finally:
            os.chdir(previous_cwd)
            if previous_config is None:
                os.environ.pop("ALFAKA_REQUEST_CONFIG", None)
            else:
                os.environ["ALFAKA_REQUEST_CONFIG"] = previous_config

    def test_market_data_images_copy_config_to_env_contract_path(self):
        dockerfiles = [
            "Dockerfile.gops-backend",
            "Dockerfile.gops-market-ingestor",
            "Dockerfile.gops-market-processor",
            "Dockerfile.gops-market-storage",
            "Dockerfile.worker",
        ]
        for dockerfile in dockerfiles:
            content = (REPO_ROOT / "infra" / "docker" / dockerfile).read_text(encoding="utf-8")
            self.assertIn("COPY systems/market-data/config ./systems/market-data/config", content)
        storage = (REPO_ROOT / "infra/docker/Dockerfile.gops-market-storage").read_text(encoding="utf-8")
        self.assertIn("COPY systems/market-data/jobs/news-backfill ./systems/market-data/jobs/news-backfill", storage)

    def test_clickhouse_array_query_parameter_serializes_symbols(self):
        self.assertEqual(clickhouse_param_value(["AAPL", "BRK.B", "O'Reilly"]), "['AAPL','BRK.B','O\\'Reilly']")

    def test_storage_clickhouse_query_parameter_serializes_typed_http_values(self):
        self.assertEqual(storage_clickhouse_param_value("2026-07-05"), "2026-07-05")
        self.assertEqual(storage_clickhouse_param_value("AAPL"), "AAPL")
        self.assertEqual(storage_clickhouse_param_value("ko-KR"), "ko-KR")
        self.assertEqual(storage_clickhouse_param_value(["AAPL", "BRK.B", "O'Reilly"]), "['AAPL','BRK.B','O\\'Reilly']")

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
            clickhouse_provider=FailingClickHouseProvider(candles=[{
                "timestamp": "2026-06-25T10:16:00.000Z",
                "open": 1,
                "high": 2,
                "low": 1,
                "close": 2,
                "volume": 100,
                "source": "clickhouse",
                "feed": "sip",
            }]),
        )

        with self.assertLogs("alfaka.serving.provider", level="WARNING") as logs:
            candles = provider.candles_since_cursor("AAPL", "1m", "v1:AAPL:1m:2026-06-25T10:15:00.000Z:abc")
            profile = provider.volume_profile_bins("AAPL", "2026-06-25T10:15:00.000Z", "2026-06-25T10:17:00.000Z", "auto")

        self.assertEqual(candles[0]["sourceEventId"], "event-later")
        self.assertEqual(profile["sideClassification"], "estimated")
        self.assertEqual(profile["totalVolume"], 100)
        self.assertIn("ClickHouse candles_since failed", "\n".join(logs.output))

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

    def test_provider_uses_lookback_rows_for_explicit_range_moving_averages(self):
        start = datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc)
        candles = [
            {
                "timestamp": (start + timedelta(hours=hour)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "interval": "1h",
                "open": hour + 1,
                "high": hour + 2,
                "low": hour,
                "close": hour + 1,
                "volume": 100,
                "isClosed": True,
                "marketSession": "regular",
            }
            for hour in range(65)
        ]
        provider = MarketDataProvider(
            redis_provider=FakeRedisProvider(),
            clickhouse_provider=RecordingRangeClickHouseProvider(candles=candles),
        )
        requested_from = (start + timedelta(hours=60)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        requested_to = (start + timedelta(hours=64)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        payload = provider.candle_snapshot("AAPL", "1h", 10, from_time=requested_from, to_time=requested_to)

        self.assertLess(provider.clickhouse_provider.calls[-1]["from_time"], requested_from)
        self.assertEqual(len(payload["candles"]), 5)
        self.assertEqual(payload["candles"][0]["timestamp"], requested_from)
        self.assertEqual(payload["candles"][-1]["timestamp"], requested_to)
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

    def test_provider_retries_latest_candles_when_default_window_is_after_available_data(self):
        start = datetime(2026, 7, 2, 14, 0, tzinfo=timezone.utc)
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
        clickhouse = RecordingRangeClickHouseProvider(candles=candles)
        provider = MarketDataProvider(
            redis_provider=FakeRedisProvider(),
            clickhouse_provider=clickhouse,
        )

        with mock.patch("alfaka.serving.provider.datetime") as fake_datetime:
            fake_datetime.now.return_value = datetime(2026, 7, 4, 14, 0, tzinfo=timezone.utc)
            fake_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            payload = provider.candle_snapshot("AAPL", "1m", 20)

        self.assertEqual(len(payload["candles"]), 20)
        self.assertEqual(payload["dataStatus"], "ready")
        self.assertEqual(payload["candles"][-1]["timestamp"], "2026-07-02T15:04:00.000Z")
        self.assertEqual(len(clickhouse.calls), 2)
        self.assertIsNotNone(clickhouse.calls[0]["from_time"])
        self.assertIsNone(clickhouse.calls[1]["from_time"])

    def test_provider_does_not_mix_previous_session_when_default_window_has_current_rows(self):
        previous_start = datetime(2026, 7, 2, 14, 0, tzinfo=timezone.utc)
        current_start = datetime(2026, 7, 6, 13, 30, tzinfo=timezone.utc)
        previous_candles = [
            {
                "timestamp": (previous_start + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "open": minute + 1,
                "high": minute + 2,
                "low": minute,
                "close": minute + 1,
                "volume": 100,
                "isClosed": True,
            }
            for minute in range(65)
        ]
        current_candles = [
            {
                "timestamp": (current_start + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "open": 100 + minute,
                "high": 101 + minute,
                "low": 99 + minute,
                "close": 100 + minute,
                "volume": 100,
                "isClosed": True,
            }
            for minute in range(10)
        ]
        clickhouse = RecordingRangeClickHouseProvider(candles=[*previous_candles, *current_candles])
        provider = MarketDataProvider(
            redis_provider=FakeRedisProvider(),
            clickhouse_provider=clickhouse,
        )

        with mock.patch("alfaka.serving.provider.datetime") as fake_datetime:
            fake_datetime.now.return_value = datetime(2026, 7, 6, 14, 47, tzinfo=timezone.utc)
            fake_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            payload = provider.candle_snapshot("AAPL", "1m", 20)

        self.assertEqual(len(payload["candles"]), 10)
        self.assertEqual(payload["candles"][0]["timestamp"], "2026-07-06T13:30:00.000Z")
        self.assertEqual(payload["candles"][-1]["timestamp"], "2026-07-06T13:39:00.000Z")
        self.assertEqual(len(clickhouse.calls), 1)
        self.assertIsNotNone(clickhouse.calls[0]["from_time"])

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
        self.assertEqual(payload["candles"][0]["close"], 100.5)
        self.assertTrue(payload["candles"][0]["isClosed"])

    def test_provider_merges_newer_daily_live_candle_for_same_closed_bucket(self):
        closed = {
            "interval": "1D",
            "timestamp": "2026-07-07T04:00:00.000Z",
            "open": 190,
            "high": 198,
            "low": 189,
            "close": 196,
            "volume": 1000,
            "isClosed": True,
            "createdAt": "2026-07-07T20:05:00.000Z",
        }
        live = {
            **closed,
            "close": 197.64,
            "volume": 1250,
            "isClosed": False,
            "sourceInterval": "1m",
            "updatedAt": "2026-07-08T01:26:50.000Z",
        }
        provider = MarketDataProvider(
            redis_provider=FakeRedisProvider(candles=[closed], live_candle=live),
            clickhouse_provider=FakeClickHouseProvider(candles=[closed]),
        )

        payload = provider.candle_snapshot("NVDA", "1D", 5)

        self.assertEqual(len(payload["candles"]), 1)
        self.assertEqual(payload["candles"][0]["timestamp"], "2026-07-07T04:00:00.000Z")
        self.assertEqual(payload["candles"][0]["close"], 197.64)
        self.assertFalse(payload["candles"][0]["isClosed"])

    def test_provider_includes_redis_live_candles_in_from_to_snapshot(self):
        start = datetime(2026, 7, 6, 13, 30, tzinfo=timezone.utc)
        clickhouse_candles = [
            {
                "timestamp": (start + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "open": 100 + minute,
                "high": 101 + minute,
                "low": 99 + minute,
                "close": 100 + minute,
                "volume": 1000 + minute,
                "isClosed": True,
                "marketSession": "regular",
            }
            for minute in range(10)
        ]
        redis_candles = [
            {
                "timestamp": (start + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "open": 100 + minute,
                "high": 101 + minute,
                "low": 99 + minute,
                "close": 100 + minute,
                "volume": 1000 + minute,
                "isClosed": True,
                "marketSession": "regular",
            }
            for minute in range(10, 29)
        ]
        live_candle = {
            "timestamp": (start + timedelta(minutes=29)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "open": 129,
            "high": 130,
            "low": 128,
            "close": 129.5,
            "volume": 2000,
            "isClosed": False,
            "marketSession": "regular",
        }
        provider = MarketDataProvider(
            redis_provider=FakeRedisProvider(candles=redis_candles, live_candle=live_candle),
            clickhouse_provider=RecordingRangeClickHouseProvider(candles=clickhouse_candles),
        )

        payload = provider.candle_snapshot(
            "AAPL",
            "1m",
            30,
            from_time="2026-07-06T13:30:00.000Z",
            to_time="2026-07-06T13:59:00.000Z",
            ma_windows=(),
        )

        self.assertEqual(len(payload["candles"]), 30)
        self.assertEqual(payload["candles"][0]["timestamp"], "2026-07-06T13:30:00.000Z")
        self.assertEqual(payload["candles"][-1]["timestamp"], "2026-07-06T13:59:00.000Z")
        self.assertEqual(payload["candles"][-1]["close"], 129.5)
        self.assertTrue(payload["_sourceTrace"]["redis"]["checked"])
        self.assertEqual(payload["_sourceTrace"]["redis"]["rowCount"], 20)

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

    def test_stock_chart_filter_hides_historical_extended_and_keeps_active_extended(self):
        candles = [
            {
                "timestamp": "2026-07-02T22:00:00.000Z",
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 100,
                "isClosed": True,
                "marketSession": "after",
            },
            {
                "timestamp": "2026-07-02T14:30:00.000Z",
                "open": 101,
                "high": 102,
                "low": 100,
                "close": 101,
                "volume": 100,
                "isClosed": True,
                "marketSession": "regular",
            },
            {
                "timestamp": "2026-07-06T02:15:00.000Z",
                "open": 102,
                "high": 103,
                "low": 101,
                "close": 102,
                "volume": 100,
                "isClosed": False,
                "marketSession": "overnight",
            },
        ]

        visible = filter_stock_chart_candles(candles, now=datetime(2026, 7, 6, 2, 30, tzinfo=timezone.utc))

        self.assertEqual(
            [candle["timestamp"] for candle in visible],
            ["2026-07-02T14:30:00.000Z", "2026-07-06T02:15:00.000Z"],
        )

    def test_daily_candles_are_visible_even_when_stored_with_non_regular_session(self):
        candles = [
            {
                "interval": "1D",
                "timestamp": "2026-07-02T04:00:00.000Z",
                "open": 101,
                "high": 102,
                "low": 100,
                "close": 101,
                "volume": 100,
                "isClosed": True,
                "marketSession": "overnight",
            }
        ]

        visible = filter_stock_chart_candles(candles, now=datetime(2026, 7, 6, 2, 30, tzinfo=timezone.utc))

        self.assertEqual(visible, candles)

    def test_daily_live_candle_merges_on_market_day_timestamp(self):
        merged = merge_candles(
            [{
                "interval": "1D",
                "timestamp": "2026-07-06T04:00:00.000Z",
                "close": 196.22,
                "isClosed": True,
                "state": "closed",
                "source": "alpaca.dailyBars",
            }],
            [{
                "interval": "1D",
                "timestamp": "2026-07-06T00:00:00.000Z",
                "close": 196.43,
                "isClosed": False,
                "state": "live",
                "source": "derived.live",
            }],
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["timestamp"], "2026-07-06T04:00:00.000Z")
        self.assertEqual(merged[0]["state"], "live")
        self.assertEqual(merged[0]["close"], 196.43)

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
        self.assertEqual(candle["priceAdjustment"], "split")
        self.assertEqual(candle["canonicalVersion"], "v2")
        self.assertIn("sourceEventId", candle)
        self.assertIn("/adjustment=split", candle["sourceEventId"])
        self.assertEqual(row["symbol"], "INTC")
        self.assertEqual(row["interval"], "1m")
        self.assertEqual(row["price_adjustment"], "split")
        self.assertEqual(row["canonical_version"], "v2")

    def test_daily_backfill_reuses_processed_candle_and_clickhouse_contract(self):
        raw_bar = alpaca_raw_bar("2026-06-25T00:00:00.000Z")
        candle = raw_bar_to_processed_candle("INTC", raw_bar, feed="sip", interval="1D")
        row = candle_to_clickhouse_row(candle)

        self.assertEqual(candle["interval"], "1D")
        self.assertEqual(candle["source"], "alpaca.dailyBars")
        self.assertEqual(candle["priceAdjustment"], "split")
        self.assertEqual(row["interval"], "1D")
        self.assertEqual(row["price_adjustment"], "split")

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
        canonical_row = {**source_row, **canonical_candle_fields()}
        updated_duplicate = {**canonical_row, "close": 10.8, "sourceEventId": "event-2"}
        result = materialize_processed_rows(client, "s3://bucket/market-data/rebuild-20260702-lazy-v1/final/candles/part-1.jsonl", [canonical_row, updated_duplicate])

        self.assertEqual(normalized["ma"]["ma5"], 10.1)
        self.assertEqual(normalized["feedProfile"], "sip")
        self.assertEqual(normalized["marketSession"], "regular")
        self.assertEqual(normalized["priceAdjustment"], "unknown")
        self.assertEqual(normalized["canonicalVersion"], "legacy")
        self.assertEqual(result["rowCount"], 1)
        self.assertEqual(client.inserts[0][0], "chart_candles")
        self.assertEqual(client.inserts[0][1][0]["source_event_id"], "event-2")
        self.assertEqual(client.inserts[0][1][0]["feed_profile"], "sip")
        self.assertEqual(client.inserts[0][1][0]["market_session"], "regular")
        self.assertEqual(client.inserts[0][1][0]["price_adjustment"], "split")
        self.assertEqual(client.inserts[0][1][0]["canonical_version"], "v2")
        self.assertEqual(client.inserts[0][1][0]["close"], 10.8)
        self.assertEqual(client.inserts[1][0], "storage_object_audit")
        self.assertEqual(client.inserts[1][1][0]["object_path"], "s3://bucket/market-data/rebuild-20260702-lazy-v1/final/candles/part-1.jsonl")
        self.assertEqual(client.inserts[1][1][0]["bucket"], "bucket")
        self.assertEqual(client.inserts[1][1][0]["dataset"], "candles")
        self.assertEqual(client.inserts[1][1][0]["interval"], "1m")
        self.assertEqual(client.inserts[2][0], "load_audit")
        self.assertEqual(client.inserts[2][1][0]["object_path"], "s3://bucket/market-data/rebuild-20260702-lazy-v1/final/candles/part-1.jsonl")

    def test_s3_materializer_accepts_direct_intraday_intervals(self):
        client = RecordingClickHouseClient()
        rows = []
        for interval, timestamp in (
            ("1h", "2026-06-25T13:00:00.000Z"),
            ("4h", "2026-06-25T16:00:00.000Z"),
        ):
            rows.append({
                **canonical_candle_fields(),
                "eventType": "CANDLE",
                "symbol": "BAC",
                "interval": interval,
                "timestamp": timestamp,
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "volume": 100,
                "isClosed": True,
                "source": "alpaca.bars",
                "feed": "sip",
                "feedProfile": "sip",
                "marketSession": "regular",
                "sourceEventId": f"event-{interval}",
            })

        result = materialize_processed_rows(
            client,
            "s3://bucket/market-data/rebuild-20260702-lazy-v1/final/candles/direct-intraday.jsonl",
            rows,
        )

        self.assertEqual(result["rowCount"], 2)
        self.assertEqual(result["skippedInvalidRowCount"], 0)
        self.assertEqual(client.inserts[0][0], "chart_candles")
        self.assertEqual([row["interval"] for row in client.inserts[0][1]], ["1h", "4h"])

    def test_s3_materializer_prefers_canonical_duplicate_over_legacy_row(self):
        client = RecordingClickHouseClient()
        legacy_row = {
            "eventType": "CANDLE",
            "symbol": "NVDA",
            "interval": "1D",
            "timestamp": "2024-06-10T00:00:00.000Z",
            "open": 1200,
            "high": 1220,
            "low": 1180,
            "close": 1210,
            "volume": 100,
            "sourceEventId": "legacy",
        }
        canonical_row = {
            **legacy_row,
            "open": 120,
            "high": 122,
            "low": 118,
            "close": 121,
            "sourceEventId": "split",
            **canonical_candle_fields(),
        }

        result = materialize_processed_rows(client, "s3://bucket/market-data/rebuild-20260702-lazy-v1/final/candles/nvda-split.jsonl", [canonical_row, legacy_row])

        self.assertEqual(result["rowCount"], 1)
        row = client.inserts[0][1][0]
        self.assertEqual(row["close"], 121)
        self.assertEqual(row["source_event_id"], "split")
        self.assertEqual(row["price_adjustment"], "split")
        self.assertEqual(row["canonical_version"], "v2")

    def test_canonical_candle_audit_query_reports_duplicate_and_noncanonical_rows(self):
        query = canonical_candle_audit_query(database="market_data", symbol="nvda", interval="1m", limit=25)

        self.assertIn("FROM market_data.chart_candles", query)
        self.assertIn("symbol = 'NVDA'", query)
        self.assertIn("interval = '1m'", query)
        self.assertIn("canonical_version = 'v2'", query)
        self.assertIn("price_adjustment IN ('split')", query)
        self.assertIn("HAVING rowCount > 1 OR nonCanonicalRowCount > 0 OR invalidOhlcCount > 0", query)
        self.assertIn("LIMIT 25", query)

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
            **canonical_candle_fields(),
        }
        object_path = "s3://bucket/market-data/rebuild-20260702-lazy-v1/final/candles/part-retry.jsonl"

        with self.assertRaisesRegex(RuntimeError, "load audit unavailable"):
            materialize_processed_rows(client, object_path, [source_row])
        result = materialize_processed_rows(client, object_path, [source_row])

        candle_inserts = [rows for table, rows in client.inserts if table == "chart_candles"]
        storage_audit_inserts = [rows for table, rows in client.inserts if table == "storage_object_audit"]
        audit_inserts = [rows for table, rows in client.inserts if table == "load_audit"]
        self.assertEqual(result["rowCount"], 1)
        self.assertEqual(len(candle_inserts), 2)
        self.assertEqual(candle_inserts[0], candle_inserts[1])
        self.assertEqual(candle_inserts[1][0]["source_event_id"], "event-1")
        self.assertEqual(len(storage_audit_inserts), 2)
        first_storage_audit = [{key: value for key, value in row.items() if key != "created_at"} for row in storage_audit_inserts[0]]
        second_storage_audit = [{key: value for key, value in row.items() if key != "created_at"} for row in storage_audit_inserts[1]]
        self.assertEqual(first_storage_audit, second_storage_audit)
        self.assertEqual(len(audit_inserts), 1)
        self.assertEqual(audit_inserts[0][0]["object_path"], object_path)

    def test_s3_materializer_skips_object_when_load_audit_already_records_it(self):
        object_key = "market-data/rebuild-20260702-lazy-v1/final/candles/interval=1D/symbol=AAPL/year=2026/month=06/day=25/part-1.jsonl"
        object_path = f"s3://bucket/{object_key}"
        source_row = {
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
        s3 = S3ObjectStore({
            object_key: json.dumps(source_row, ensure_ascii=False),
        })
        client = AuditAwareClickHouseClient(materialized_paths={object_path})

        result = materialize_s3_processed_objects(client, s3, "bucket", [object_key], source_name="smoke")

        self.assertEqual(client.audit_checks, [object_path])
        self.assertEqual(result["rowCount"], 0)
        self.assertEqual(result["objects"], [{
            "objectPath": object_path,
            "rowCount": 0,
            "skippedAlreadyMaterialized": True,
        }])
        self.assertEqual(client.inserts, [])

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

    def test_news_intelligence_row_keeps_ko_fields_for_clickhouse(self):
        record = build_news_intelligence_record(
            {
                "eventType": "NEWS_ARTICLE",
                "symbol": "AAPL",
                "articleId": "news-ko-1",
                "headline": "Apple supplier expands",
                "summary": "Apple supplier plans expansion.",
                "publishedAt": "2026-06-29T01:02:03.000Z",
                "url": "https://example.com/aapl",
                "source": "alpaca",
                "symbols": ["AAPL"],
            },
            {
                "localizedHeadline": "애플 공급사, 사업 확장",
                "localizedSummary": "애플 공급사가 사업 확장을 추진합니다.",
                "keyPoints": ["사업 확장 추진"],
                "positivePoints": [],
                "concerns": ["애플 의존도 완화 필요"],
                "eventType": "corporate-action",
                "sentiment": "neutral",
                "impactDirection": "neutral",
                "whyItMatters": "애플 공급망과 관련된 뉴스입니다.",
            },
            model="unit-model",
            localized_at="2026-06-29T01:03:00.000Z",
        )

        row = news_intelligence_to_clickhouse_row(record)

        self.assertEqual(row["article_id"], "news-ko-1")
        self.assertEqual(row["localized_headline"], "애플 공급사, 사업 확장")
        self.assertEqual(row["localized_summary"], "애플 공급사가 사업 확장을 추진합니다.")
        self.assertEqual(row["key_points"], ["사업 확장 추진"])
        self.assertEqual(row["concerns"], ["애플 의존도 완화 필요"])
        self.assertEqual(row["event_type"], "corporate-action")
        self.assertEqual(row["model"], "unit-model")
        self.assertEqual(row["target_symbol"], "AAPL")
        self.assertIn(row["subject_relevance"], {"primary", "secondary"})
        self.assertGreater(row["relevance_score_v2"], 0.7)
        self.assertIn("apple", [item.lower() for item in row["direct_signals"]])

    def test_news_intelligence_worker_writes_clickhouse_and_redis_hot_cache(self):
        worker = load_news_intelligence_worker_module()
        client = RecordingClickHouseClient()
        redis_client = MemoryRedis()
        event = {
            "eventType": "NEWS_ARTICLE",
            "symbol": "AAPL",
            "articleId": "worker-news-1",
            "headline": "Apple supplier expands",
            "summary": "Apple supplier plans expansion.",
            "publishedAt": "2026-06-29T01:02:03.000Z",
            "url": "https://example.com/worker-aapl",
            "source": "alpaca",
            "symbols": ["AAPL"],
        }

        record = worker.process_news_event(
            event,
            clickhouse_client=client,
            redis_client=redis_client,
            enrich_fn=lambda _event: {
                "localizedHeadline": "애플 공급사, 사업 확장 추진",
                "localizedSummary": "애플 공급사가 사업 확장을 추진한다는 내용입니다.",
                "keyPoints": ["사업 확장 추진"],
                "positivePoints": [],
                "concerns": [],
                "eventType": "corporate-action",
                "sentiment": "neutral",
                "impactDirection": "neutral",
                "whyItMatters": "애플 공급망 관련 뉴스입니다.",
            },
            locale="ko-KR",
            model="unit-model",
            ttl_seconds=1800,
            max_items=20,
        )
        duplicate = worker.process_news_event(
            event,
            clickhouse_client=client,
            redis_client=redis_client,
            enrich_fn=lambda _event: {
                "localizedHeadline": "중복",
                "localizedSummary": "중복",
            },
            locale="ko-KR",
            model="unit-model",
            ttl_seconds=1800,
            max_items=20,
        )

        self.assertEqual(client.inserts[0][0], "news_article_localizations")
        self.assertEqual(client.inserts[0][1][0]["localized_headline"], "애플 공급사, 사업 확장 추진")
        self.assertEqual(record["localizedSummary"], "애플 공급사가 사업 확장을 추진한다는 내용입니다.")
        self.assertEqual(record["keyPoints"], ["사업 확장 추진"])
        self.assertIsNone(duplicate)
        self.assertEqual(len(client.inserts), 1)
        cached = read_localized_news_from_redis(redis_client, "AAPL", limit=5, locale="ko-KR")
        self.assertEqual(cached[0]["localizedHeadline"], "애플 공급사, 사업 확장 추진")
        self.assertEqual(cached[0]["keyPoints"], ["사업 확장 추진"])
        self.assertEqual(cached[0]["articleId"], "worker-news-1")
        self.assertEqual(cached[0]["targetSymbol"], "AAPL")
        self.assertIn(cached[0]["subjectRelevance"], {"primary", "secondary"})
        self.assertNotIn("content", cached[0])
        self.assertNotIn("raw", cached[0])
        self.assertIn(RedisKeyBuilder().news_latest_v2("ko-KR", "AAPL"), redis_client.zsets)
        self.assertNotIn(RedisKeyBuilder().news_latest("ko-KR", "AAPL"), redis_client.zsets)

    def test_localized_news_redis_cache_defaults_to_thirty_day_retention(self):
        redis_client = MemoryRedis()
        now = datetime.now(timezone.utc)
        recent_published_at = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        old_published_at = (now - timedelta(days=31)).isoformat().replace("+00:00", "Z")

        with mock.patch.dict(os.environ, {
            "NEWS_REDIS_TTL_SECONDS": "2592000",
            "NEWS_REDIS_MAX_ITEMS": "1000",
            "NEWS_REDIS_RETENTION_DAYS": "30",
        }, clear=False):
            write_localized_news_to_redis(
                redis_client,
                {
                    "articleId": "old-news",
                    "symbol": "AAPL",
                    "targetSymbol": "AAPL",
                    "symbols": ["AAPL"],
                    "localizedHeadline": "오래된 뉴스",
                    "localizedSummary": "31일 전 뉴스입니다.",
                    "publishedAt": old_published_at,
                },
                ttl_seconds=int(os.environ["NEWS_REDIS_TTL_SECONDS"]),
                max_items=int(os.environ["NEWS_REDIS_MAX_ITEMS"]),
                retention_days=int(os.environ["NEWS_REDIS_RETENTION_DAYS"]),
                locale="ko-KR",
            )
            write_localized_news_to_redis(
                redis_client,
                {
                    "articleId": "recent-news",
                    "symbol": "AAPL",
                    "targetSymbol": "AAPL",
                    "symbols": ["AAPL"],
                    "localizedHeadline": "최근 뉴스",
                    "localizedSummary": "최근 뉴스입니다.",
                    "publishedAt": recent_published_at,
                },
                ttl_seconds=int(os.environ["NEWS_REDIS_TTL_SECONDS"]),
                max_items=int(os.environ["NEWS_REDIS_MAX_ITEMS"]),
                retention_days=int(os.environ["NEWS_REDIS_RETENTION_DAYS"]),
                locale="ko-KR",
            )

        cached = read_localized_news_from_redis(redis_client, "AAPL", limit=10, locale="ko-KR")

        self.assertEqual([row["articleId"] for row in cached], ["recent-news"])
        self.assertEqual(redis_client.expirations[RedisKeyBuilder().news_latest_v2("ko-KR", "AAPL")], 2592000)

    def test_news_intelligence_worker_publishes_daily_summary_dirty_event(self):
        worker = load_news_intelligence_worker_module()
        client = RecordingClickHouseClient()
        redis_client = MemoryRedis()

        class RecordingProducer:
            def __init__(self):
                self.sent = []

            def send(self, topic, key, value):
                self.sent.append((topic, key, value))

        producer = RecordingProducer()
        worker.process_news_event(
            {
                "eventType": "NEWS_ARTICLE",
                "symbol": "AAPL",
                "articleId": "daily-dirty-1",
                "headline": "Apple services revenue grows",
                "summary": "Apple services revenue improved.",
                "publishedAt": "2026-07-01T14:02:03.000Z",
                "url": "https://example.com/aapl-dirty",
                "source": "alpaca",
                "symbols": ["AAPL"],
            },
            clickhouse_client=client,
            redis_client=redis_client,
            enrich_fn=lambda _event: {
                "localizedHeadline": "애플 서비스 매출 성장",
                "localizedSummary": "애플 서비스 매출이 개선됐습니다.",
                "keyPoints": ["서비스 매출 개선"],
                "positivePoints": ["서비스 성장"],
                "concerns": [],
                "eventType": "earnings",
                "sentiment": "positive",
                "impactDirection": "positive",
                "whyItMatters": "애플 수익성에 긍정적입니다.",
            },
            locale="ko-KR",
            daily_summary_producer=producer,
        )

        self.assertEqual(producer.sent[0][0], "market.news.daily-summary-dirty.v1")
        self.assertEqual(producer.sent[0][1], "AAPL:2026-07-01")
        self.assertEqual(producer.sent[0][2]["eventType"], "NEWS_DAILY_SUMMARY_DIRTY")
        self.assertEqual(producer.sent[0][2]["symbol"], "AAPL")

    def test_daily_summary_row_and_redis_cache_keep_lightweight_brief(self):
        record = build_daily_summary_record(
            symbol="AAPL",
            date="2026-07-01",
            rows=[
                {
                    "articleId": "aapl-daily-1",
                    "localizedHeadline": "애플 서비스 성장",
                    "localizedSummary": "서비스 매출이 개선됐습니다.",
                    "impactDirection": "positive",
                    "sentiment": "positive",
                    "keyPoints": ["서비스 매출 개선"],
                    "url": "https://example.com/aapl-services",
                    "source": "Example News",
                    "publishedAt": "2026-07-01T12:00:00.000Z",
                }
            ],
            locale="ko-KR",
            model="unit-model",
            generated_at="2026-07-01T22:00:00.000Z",
            status="final",
            mention_count=2,
        )
        row = daily_summary_to_clickhouse_row(record)
        redis_client = MemoryRedis()
        from alfaka.serving.news_hot_cache import write_company_daily_summary_to_redis

        write_company_daily_summary_to_redis(redis_client, record, locale="ko-KR", ttl_seconds=86400, max_items=30)
        cached = read_company_daily_summaries_from_redis(redis_client, "AAPL", locale="ko-KR")

        self.assertEqual(row["symbol"], "AAPL")
        self.assertEqual(row["article_count"], 1)
        self.assertEqual(row["mention_count"], 2)
        self.assertEqual(record["sources"][0]["url"], "https://example.com/aapl-services")
        self.assertEqual(record["sources"][0]["name"], "Example News")
        self.assertEqual(clickhouse_row_to_daily_summary(row)["sources"][0]["title"], "애플 서비스 성장")
        self.assertEqual(cached[0]["summary"], record["summary"])
        self.assertEqual(cached[0]["articleCount"], 1)
        self.assertEqual(cached[0]["sources"][0]["url"], "https://example.com/aapl-services")
        self.assertIn(RedisKeyBuilder().news_daily_v2("ko-KR", "AAPL"), redis_client.zsets)

    def test_daily_summary_redis_warmup_records_coverage_and_dedupes_dates(self):
        redis_client = MemoryRedis()
        rows = [
            {
                "date": "2026-07-01",
                "symbol": "AAPL",
                "summary": "오래된 요약입니다.",
                "generatedAt": "2026-07-01T20:00:00.000Z",
                "articleIds": ["old"],
                "articleCount": 1,
            },
            {
                "date": "2026-07-01",
                "symbol": "AAPL",
                "summary": "최신 요약입니다.",
                "generatedAt": "2026-07-01T22:00:00.000Z",
                "articleIds": ["new"],
                "articleCount": 1,
            },
            {
                "date": "2026-06-30",
                "symbol": "AAPL",
                "summary": "전일 요약입니다.",
                "generatedAt": "2026-06-30T22:00:00.000Z",
                "articleIds": ["previous"],
                "articleCount": 1,
            },
        ]

        write_company_daily_summaries_to_redis(
            redis_client,
            rows,
            symbol="AAPL",
            days=30,
            limit=30,
            ttl_seconds=604800,
            coverage_ttl_seconds=604800,
            locale="ko-KR",
        )
        cached = read_company_daily_summaries_from_redis(redis_client, "AAPL", limit=30, locale="ko-KR")
        coverage = read_company_daily_summary_coverage_from_redis(redis_client, "AAPL", locale="ko-KR")

        self.assertEqual([row["date"] for row in cached], ["2026-07-01", "2026-06-30"])
        self.assertEqual(cached[0]["summary"], "최신 요약입니다.")
        self.assertEqual(coverage["rowCount"], 2)
        self.assertTrue(company_daily_summary_coverage_valid(coverage, symbol="AAPL", days=30, limit=30, locale="ko-KR", rows=cached))
        self.assertIn(RedisKeyBuilder().news_daily_coverage_v2("ko-KR", "AAPL"), redis_client.values)

    def test_daily_summary_price_change_uses_previous_trading_day_close(self):
        summaries = [{"date": "2026-07-01", "symbol": "AAPL", "summary": "브리프"}]
        enriched = attach_price_changes_to_daily_summaries(summaries, [
            {"timestamp": "2026-06-29T00:00:00.000Z", "close": 199.25},
            {"timestamp": "2026-06-30T00:00:00.000Z", "close": 199.85},
            {"timestamp": "2026-07-01T00:00:00.000Z", "close": 200.00},
        ])

        self.assertEqual(enriched[0]["priceChange"]["previousClose"], 199.85)
        self.assertEqual(enriched[0]["priceChange"]["close"], 200.0)
        self.assertEqual(enriched[0]["priceChange"]["change"], 0.15)

    def test_news_daily_summary_worker_inserts_summary_and_skips_same_article_hash(self):
        worker = load_news_daily_summary_worker_module()
        redis_client = MemoryRedis()
        rows = [
            {
                "articleId": "aapl-daily-worker-1",
                "symbol": "AAPL",
                "targetSymbol": "AAPL",
                "subjectRelevance": "primary",
                "headline": "Apple services revenue grows",
                "summary": "Apple services revenue improved.",
                "localizedHeadline": "애플 서비스 매출 성장",
                "localizedSummary": "애플 서비스 매출이 개선됐습니다.",
                "keyPoints": ["서비스 매출 개선"],
                "positivePoints": ["서비스 성장"],
                "concerns": [],
                "impactDirection": "positive",
                "sentiment": "positive",
                "url": "https://example.com/aapl-worker",
                "source": "Example News",
                "publishedAt": "2026-07-01T12:00:00.000Z",
            },
            {
                "articleId": "aapl-mention-worker-1",
                "symbol": "NVDA",
                "targetSymbol": "AAPL",
                "subjectRelevance": "mention",
                "headline": "Broad tech roundup mentions Apple",
                "summary": "Apple appears in a broad list.",
                "localizedHeadline": "기술주 라운드업",
                "localizedSummary": "애플이 넓은 목록에 언급됐습니다.",
                "impactDirection": "neutral",
                "sentiment": "neutral",
            },
        ]
        client = SequentialQueryClickHouseClient([rows, []])

        record = worker.process_dirty_event(
            {"eventType": "NEWS_DAILY_SUMMARY_DIRTY", "symbol": "AAPL", "date": "2026-07-01", "locale": "ko-KR"},
            clickhouse_client=client,
            redis_client=redis_client,
            summarize_fn=lambda **_kwargs: {
                "summary": "애플 일일 브리프입니다.",
                "keyPoints": ["서비스 매출 개선"],
                "positivePoints": ["서비스 성장"],
                "concerns": [],
                "impactDirection": "positive",
                "sentiment": "positive",
            },
            model="unit-model",
        )

        self.assertEqual(client.queries[0][1]["symbol"], "AAPL")
        self.assertEqual(client.queries[0][1]["date"], "2026-07-01")
        self.assertEqual(client.queries[0][1]["locale"], "ko-KR")
        self.assertIsInstance(client.queries[0][1]["limit"], int)
        existing_query, existing_params = client.queries[1]
        self.assertIn("FROM market_data.news_company_daily_summaries AS summaries", existing_query)
        self.assertIn("summaries.date = {date:Date}", existing_query)
        self.assertNotIn("AND date =", existing_query)
        self.assertEqual(existing_params["date"], "2026-07-01")
        self.assertEqual(record["articleIds"], ["aapl-daily-worker-1"])
        self.assertEqual(record["mentionCount"], 1)
        self.assertEqual(record["sources"][0]["url"], "https://example.com/aapl-worker")
        self.assertEqual(client.inserts[0][0], "news_company_daily_summaries")
        cached = read_company_daily_summaries_from_redis(redis_client, "AAPL", locale="ko-KR")
        self.assertEqual(cached[0]["summary"], "애플 일일 브리프입니다.")
        self.assertEqual(cached[0]["sources"][0]["name"], "Example News")

        skip_client = SequentialQueryClickHouseClient([rows, [{"articleIdsHash": record["articleIdsHash"]}]])
        skipped = worker.process_dirty_event(
            {"eventType": "NEWS_DAILY_SUMMARY_DIRTY", "symbol": "AAPL", "date": "2026-07-01", "locale": "ko-KR"},
            clickhouse_client=skip_client,
            redis_client=redis_client,
            summarize_fn=lambda **_kwargs: {"summary": "재생성되면 안 됩니다."},
            model="unit-model",
        )
        self.assertIsNone(skipped)
        self.assertEqual(skip_client.inserts, [])

        existing_record = {
            "date": "2026-07-01",
            "symbol": "AAPL",
            "locale": "ko-KR",
            "summary": "ClickHouse 기존 일일 브리프입니다.",
            "keyPoints": ["서비스 매출 개선"],
            "positivePoints": ["서비스 성장"],
            "concerns": [],
            "impactDirection": "positive",
            "sentiment": "positive",
            "articleIds": ["aapl-daily-worker-1"],
            "articleIdsHash": record["articleIdsHash"],
            "articleCount": 1,
            "mentionCount": 1,
            "status": "final",
            "model": "unit-model",
            "generatedAt": "2026-07-01T23:00:00.000Z",
            "version": "v1",
            "sources": [{"title": "애플 서비스 매출 성장", "url": "https://example.com/aapl-worker"}],
        }
        warm_client = SequentialQueryClickHouseClient([rows, [existing_record]])
        warm_redis = MemoryRedis()
        warmed = worker.process_dirty_event(
            {"eventType": "NEWS_DAILY_SUMMARY_DIRTY", "symbol": "AAPL", "date": "2026-07-01", "locale": "ko-KR"},
            clickhouse_client=warm_client,
            redis_client=warm_redis,
            summarize_fn=lambda **_kwargs: self.fail("unchanged ClickHouse summary should not be regenerated"),
            model="unit-model",
        )
        self.assertIsNone(warmed)
        self.assertEqual(warm_client.inserts, [])
        warmed_cache = read_company_daily_summaries_from_redis(warm_redis, "AAPL", locale="ko-KR")
        self.assertEqual(warmed_cache[0]["summary"], "ClickHouse 기존 일일 브리프입니다.")
        self.assertEqual(warmed_cache[0]["sources"][0]["url"], "https://example.com/aapl-worker")

    def test_news_hot_cache_v2_does_not_fan_out_multi_symbol_rows_to_other_companies(self):
        worker = load_news_intelligence_worker_module()
        client = RecordingClickHouseClient()
        redis_client = MemoryRedis()
        event = {
            "eventType": "NEWS_ARTICLE",
            "symbol": "NVDA",
            "articleId": "nvda-apple-mention",
            "headline": "Jim Cramer says Nvidia recommendation made a fortune",
            "summary": "Apple is only present in provider metadata.",
            "publishedAt": "2026-06-29T01:02:03.000Z",
            "url": "https://example.com/nvda",
            "source": "alpaca",
            "symbols": ["AAPL", "NVDA", "MSFT", "META"],
        }

        worker.process_news_event(
            event,
            clickhouse_client=client,
            redis_client=redis_client,
            enrich_fn=lambda _event: {
                "localizedHeadline": "엔비디아 추천 사례",
                "localizedSummary": "엔비디아 추천 사례를 다룬 기사입니다.",
                "keyPoints": ["엔비디아 직접 관련"],
                "positivePoints": [],
                "concerns": [],
                "eventType": "commentary",
                "sentiment": "neutral",
                "impactDirection": "neutral",
                "whyItMatters": "엔비디아 관련 기사입니다.",
            },
            locale="ko-KR",
        )

        self.assertEqual(read_localized_news_from_redis(redis_client, "AAPL", limit=5, locale="ko-KR"), [])
        self.assertEqual(read_localized_news_from_redis(redis_client, "NVDA", limit=5, locale="ko-KR")[0]["articleId"], "nvda-apple-mention")

    def test_news_intelligence_rebuild_reinserts_recent_rows_with_relevance_v2(self):
        rebuild = load_news_intelligence_rebuild_module()
        client = QueryRecordingClickHouseClient([
            {
                "publishedAt": "2026-06-29T01:02:03.000Z",
                "symbol": "AAPL",
                "articleId": "rebuild-aapl-1",
                "locale": "ko-KR",
                "symbols": ["AAPL"],
                "headline": "Apple CEO Tim Cook flags memory chip shortage",
                "summary": "Apple CEO Tim Cook discussed an extreme memory chip shortage.",
                "localizedHeadline": "팀 쿡, 메모리칩 부족 언급",
                "localizedSummary": "애플 CEO 팀 쿡이 메모리칩 부족을 언급했습니다.",
                "keyPoints": ["애플 CEO 발언"],
                "positivePoints": [],
                "concerns": ["공급 부족"],
                "eventType": "supply-shortage",
                "sentiment": "mixed",
                "impactDirection": "mixed",
                "whyItMatters": "애플 공급망 이슈입니다.",
                "url": "https://example.com/aapl-rebuild",
                "source": "benzinga",
                "model": "old-model",
                "raw": "{}",
            },
            {
                "publishedAt": "2026-06-29T01:02:03.000Z",
                "symbol": "AAPL",
                "articleId": "rebuild-aapl-1",
                "locale": "ko-KR",
                "symbols": ["AAPL", "NVDA"],
                "headline": "Jim Cramer says Nvidia recommendation made a fortune",
                "summary": "AAPL appears only in provider metadata.",
                "localizedHeadline": "짐 크레이머, 엔비디아 추천 사례 언급",
                "localizedSummary": "엔비디아 추천 사례를 다룬 기사입니다.",
                "keyPoints": [],
                "positivePoints": [],
                "concerns": [],
                "eventType": "market-commentary",
                "sentiment": "neutral",
                "impactDirection": "neutral",
                "whyItMatters": "",
                "url": "https://example.com/aapl-rebuild-duplicate",
                "source": "benzinga",
                "model": "old-model",
                "raw": "{}",
            }
        ])

        rebuilt = rebuild.rebuild_recent_localizations(client, days=30, batch_size=10, max_rows=10, rewrite_clickhouse=True)

        self.assertEqual(rebuilt, 1)
        self.assertEqual(len(client.executions), 1)
        self.assertIn("DELETE WHERE article_id IN", client.executions[0][0])
        self.assertEqual(client.executions[0][1]["articleIds"], ["rebuild-aapl-1"])
        self.assertEqual(client.inserts[0][0], "news_article_localizations")
        row = client.inserts[0][1][0]
        self.assertEqual(row["target_symbol"], "AAPL")
        self.assertIn(row["subject_relevance"], {"primary", "secondary"})
        self.assertGreater(row["relevance_score_v2"], 0.7)

    def test_news_intelligence_rebuild_warms_redis_with_recent_localizations(self):
        rebuild = load_news_intelligence_rebuild_module()
        client = QueryRecordingClickHouseClient([
            {
                "publishedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "symbol": "AAPL",
                "articleId": "redis-warm-aapl-1",
                "locale": "ko-KR",
                "symbols": ["AAPL"],
                "headline": "Apple expands AI features",
                "summary": "Apple expanded AI features.",
                "localizedHeadline": "애플, AI 기능 확대",
                "localizedSummary": "애플이 AI 기능을 확대했습니다.",
                "keyPoints": ["AI 기능 확대"],
                "positivePoints": [],
                "concerns": [],
                "eventType": "product-market",
                "sentiment": "positive",
                "impactDirection": "positive",
                "whyItMatters": "제품 경쟁력과 관련됩니다.",
                "url": "https://example.com/aapl-ai",
                "source": "benzinga",
                "model": "old-model",
                "raw": "{}",
            }
        ])
        redis_client = MemoryRedis()

        with mock.patch.dict(os.environ, {
            "NEWS_REDIS_TTL_SECONDS": "2592000",
            "NEWS_REDIS_MAX_ITEMS": "1000",
            "NEWS_REDIS_RETENTION_DAYS": "30",
        }, clear=False):
            rebuilt = rebuild.rebuild_recent_localizations(client, days=30, batch_size=10, max_rows=10, redis_client=redis_client)
        cached = read_localized_news_from_redis(redis_client, "AAPL", limit=10, locale="ko-KR")

        self.assertEqual(rebuilt, 1)
        self.assertEqual(cached[0]["articleId"], "redis-warm-aapl-1")
        self.assertEqual(cached[0]["localizedHeadline"], "애플, AI 기능 확대")
        self.assertEqual(redis_client.expirations[RedisKeyBuilder().news_latest_v2("ko-KR", "AAPL")], 2592000)
        self.assertEqual(client.executions, [])
        self.assertEqual(client.inserts, [])

    def test_news_intelligence_rebuild_selects_schema_compatible_legacy_rows(self):
        rebuild = load_news_intelligence_rebuild_module()
        redis_client = MemoryRedis()
        client = NewsRebuildSchemaAwareClickHouseClient(
            columns=[
                "published_at",
                "symbol",
                "article_id",
                "locale",
                "headline",
                "summary",
                "url",
                "source",
                "localized_headline",
                "localized_summary",
                "event_type",
                "sentiment",
                "impact_direction",
                "why_it_matters",
                "model",
                "localized_at",
                "raw",
            ],
            partitions=[{"symbol": "AAPL", "locale": "ko-KR"}],
            rows_by_partition={
                ("AAPL", "ko-KR"): [
                    {
                        "publishedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        "symbol": "AAPL",
                        "articleId": "legacy-aapl-1",
                        "locale": "ko-KR",
                        "symbols": ["AAPL"],
                        "headline": "Apple expands AI features",
                        "summary": "Apple expanded AI features.",
                        "localizedHeadline": "애플, AI 기능 확대",
                        "localizedSummary": "애플이 AI 기능을 확대했습니다.",
                        "eventType": "product-market",
                        "sentiment": "positive",
                        "impactDirection": "positive",
                        "whyItMatters": "제품 경쟁력과 관련됩니다.",
                        "url": "https://example.com/aapl-ai",
                        "source": "benzinga",
                        "model": "old-model",
                        "raw": "{}",
                    }
                ]
            },
        )

        rebuilt = rebuild.rebuild_recent_localizations(
            client,
            days=30,
            batch_size=10,
            max_rows=10,
            redis_client=redis_client,
        )

        self.assertEqual(rebuilt, 1)
        select_query = client.queries[-1][0]
        self.assertIn("symbol AS targetSymbol", select_query)
        self.assertIn("'mention' AS subjectRelevance", select_query)
        self.assertIn("toFloat32(0) AS relevanceScoreV2", select_query)
        self.assertIn("CAST([], 'Array(String)') AS directSignals", select_query)
        self.assertNotIn("OFFSET", select_query)
        self.assertEqual(client.queries[-1][1]["symbol"], "AAPL")
        self.assertEqual(client.queries[-1][1]["locale"], "ko-KR")
        cached = read_localized_news_from_redis(redis_client, "AAPL", limit=5, locale="ko-KR")
        self.assertEqual(cached[0]["articleId"], "legacy-aapl-1")
        self.assertEqual(cached[0]["targetSymbol"], "AAPL")

    def test_news_intelligence_worker_falls_back_when_openai_fails(self):
        worker = load_news_intelligence_worker_module()
        client = RecordingClickHouseClient()
        redis_client = MemoryRedis()
        event = {
            "eventType": "NEWS_ARTICLE",
            "symbol": "NVDA",
            "articleId": "openai-fail-1",
            "headline": "NVIDIA shares rise",
            "summary": "NVIDIA shares rise after product news.",
            "publishedAt": "2026-06-29T01:02:03.000Z",
            "symbols": ["NVDA"],
        }

        with mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-key",
            "NEWS_INTELLIGENCE_PROVIDER": "openai",
            "NEWS_INTELLIGENCE_DETERMINISTIC_FALLBACK": "true",
        }, clear=False):
            with mock.patch.object(worker, "enrich_news_with_openai", side_effect=RuntimeError("timeout")):
                record = worker.process_news_event(event, clickhouse_client=client, redis_client=redis_client)

        self.assertEqual(record["model"], "deterministic-fallback")
        self.assertEqual(client.inserts[0][0], "news_article_localizations")
        self.assertEqual(client.inserts[0][1][0]["localized_headline"], "NVIDIA shares rise")

    def test_clickhouse_news_articles_table_has_30_day_ttl(self):
        schema = (REPO_ROOT / "infra" / "clickhouse" / "initdb" / "01-market-data.sql").read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE IF NOT EXISTS market_data.news_articles", schema)
        self.assertIn("TTL toDateTime(published_at) + INTERVAL 30 DAY DELETE", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS market_data.news_article_localizations", schema)
        self.assertIn("key_points Array(String)", schema)
        self.assertIn("positive_points Array(String)", schema)
        self.assertIn("concerns Array(String)", schema)
        self.assertIn("target_symbol LowCardinality(String)", schema)
        self.assertIn("subject_relevance LowCardinality(String)", schema)
        self.assertIn("relevance_score_v2 Float32", schema)
        self.assertIn("direct_signals Array(String)", schema)
        self.assertIn("ORDER BY (symbol, locale, published_at, article_id)", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS market_data.news_company_daily_summaries", schema)
        self.assertIn("article_ids_hash String", schema)
        self.assertIn("TTL toDate(date) + INTERVAL 366 DAY DELETE", schema)

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
        result = materialize_processed_rows(client, "s3://bucket/market-data/rebuild-20260702-lazy-v1/final/candles/weekend.jsonl", [weekend_row])

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
        result = materialize_processed_rows(client, "s3://bucket/market-data/rebuild-20260702-lazy-v1/final/candles/monthly.jsonl", [monthly_row])

        self.assertEqual(result["rowCount"], 0)
        self.assertEqual(result["skippedInvalidRowCount"], 1)
        self.assertTrue(any(table == "chart_candles" for table, _rows in client.inserts))

    def test_s3_materializer_detects_jsonl_and_parquet(self):
        self.assertEqual(detect_s3_object_format("market-data/rebuild-20260702-lazy-v1/final/candles/part-1.jsonl"), "jsonl")
        self.assertEqual(detect_s3_object_format("market-data/rebuild-20260702-lazy-v1/final/candles/part-1.ndjson"), "jsonl")
        self.assertEqual(detect_s3_object_format("market-data/rebuild-20260702-lazy-v1/final/candles/part-1.parquet"), "parquet")

    def test_s3_materializer_lists_processed_final_objects(self):
        keys = list_s3_objects(FakeS3WithPaginator(), "bucket", "market-data/rebuild-20260702-lazy-v1/final/candles")

        self.assertEqual(keys, [
            "market-data/rebuild-20260702-lazy-v1/final/candles/part-1.jsonl",
            "market-data/rebuild-20260702-lazy-v1/final/candles/part-2.parquet",
        ])

    def test_s3_materializer_can_target_explicit_keys_for_smoke(self):
        s3 = S3ObjectStore({
            "market-data/rebuild-20260702-lazy-v1/final/candles/part-a.jsonl": "",
            "market-data/rebuild-20260702-lazy-v1/final/candles/part-b.jsonl": "",
        })

        with mock.patch.dict(os.environ, {"S3_MATERIALIZE_KEYS": "market-data/rebuild-20260702-lazy-v1/final/candles/part-b.jsonl"}, clear=False):
            keys = materialize_keys_from_env(s3, "bucket", "market-data/rebuild-20260702-lazy-v1/final/candles")

        self.assertEqual(keys, ["market-data/rebuild-20260702-lazy-v1/final/candles/part-b.jsonl"])

    def test_s3_materializer_can_target_manifest_range_for_bootstrap_smoke(self):
        s3 = S3ObjectStore()
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
            **canonical_candle_fields(),
        }
        object_key = flush_buffer(
            s3,
            "bucket",
            "market-data/rebuild-20260702-lazy-v1/final/candles/interval=1D/symbol=AAPL/backfill_request=bootstrap",
            [row],
            "jsonl",
            manifest_prefix="market-data/rebuild-20260702-lazy-v1/manifest",
            manifest_layout="compact",
        )

        with mock.patch.dict(os.environ, {
            "S3_MATERIALIZE_SYMBOL": "AAPL",
            "S3_MATERIALIZE_INTERVAL": "1D",
            "S3_MATERIALIZE_START": "2026-06-25T00:00:00.000Z",
            "S3_MATERIALIZE_END": "2026-06-26T00:00:00.000Z",
            "S3_MANIFEST_PREFIX": "market-data/rebuild-20260702-lazy-v1/manifest",
        }, clear=True):
            keys = materialize_keys_from_env(s3, "bucket", "market-data/rebuild-20260702-lazy-v1/final")

        self.assertEqual(keys, [object_key])

    def test_s3_materializer_range_without_manifest_does_not_scan_entire_prefix(self):
        s3 = S3ObjectStore({
            "market-data/rebuild-20260702-lazy-v1/final/candles/unrelated.jsonl": "",
        })

        with mock.patch.dict(os.environ, {
            "S3_MATERIALIZE_SYMBOL": "AAPL",
            "S3_MATERIALIZE_INTERVAL": "1D",
            "S3_MATERIALIZE_START": "2026-06-25T00:00:00.000Z",
            "S3_MATERIALIZE_END": "2026-06-26T00:00:00.000Z",
            "S3_MANIFEST_PREFIX": "market-data/rebuild-20260702-lazy-v1/manifest",
        }, clear=True):
            keys = materialize_keys_from_env(s3, "bucket", "market-data/rebuild-20260702-lazy-v1/final")

        self.assertEqual(keys, [])

    def test_s3_materializer_rejects_pre_cutoff_1m_bootstrap_range(self):
        s3 = S3ObjectStore()

        with mock.patch.dict(os.environ, {
            "S3_MATERIALIZE_SYMBOL": "AAPL",
            "S3_MATERIALIZE_INTERVAL": "1m",
            "S3_MATERIALIZE_START": "2020-06-01T00:00:00.000Z",
            "S3_MATERIALIZE_END": "2020-07-01T00:00:00.000Z",
            "BACKFILL_INITIAL_LOAD_1M_MIN_START": "2020-07-01T00:00:00Z",
            "S3_MANIFEST_PREFIX": "market-data/rebuild-20260702-lazy-v1/manifest",
        }, clear=True):
            with self.assertRaisesRegex(ValueError, "BACKFILL_INITIAL_LOAD_1M_MIN_START"):
                materialize_keys_from_env(s3, "bucket", "market-data/rebuild-20260702-lazy-v1/final")

    def test_s3_client_can_use_s3_specific_credentials_for_minio(self):
        captured = {}

        def fake_client(service, **kwargs):
            captured["service"] = service
            captured["kwargs"] = kwargs
            return object()

        with mock.patch.dict(os.environ, {
            "S3_ENDPOINT_URL": "http://minio:9000",
            "S3_ACCESS_KEY_ID": "minioadmin",
            "S3_SECRET_ACCESS_KEY": "minioadmin",
            "S3_SESSION_TOKEN": "",
            "AWS_REGION": "ap-northeast-2",
        }, clear=False):
            with mock.patch("alfaka.common.s3_client.boto3.client", side_effect=fake_client):
                create_s3_client()

        self.assertEqual(captured["service"], "s3")
        self.assertEqual(captured["kwargs"]["endpoint_url"], "http://minio:9000")
        self.assertEqual(captured["kwargs"]["aws_access_key_id"], "minioadmin")
        self.assertEqual(captured["kwargs"]["aws_secret_access_key"], "minioadmin")
        self.assertNotIn("aws_session_token", captured["kwargs"])

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
            **canonical_candle_fields(),
        }

        object_key = flush_buffer(
            s3,
            "bucket",
            "market-data/rebuild-20260702-lazy-v1/final/candles/interval=1D/symbol=AAPL/year=2026/month=06/day=25",
            [row],
            "parquet",
            manifest_prefix="market-data/rebuild-20260702-lazy-v1/manifest",
        )
        rows = read_s3_rows(s3, "bucket", object_key)
        result = materialize_s3_processed_objects(client, s3, "bucket", [object_key], source_name="smoke")

        self.assertEqual(rows[0]["symbol"], "AAPL")
        self.assertEqual(result["rowCount"], 1)
        self.assertEqual(client.inserts[0][0], "chart_candles")
        self.assertEqual(client.inserts[1][0], "storage_object_audit")
        self.assertEqual(client.inserts[1][1][0]["source"], "smoke")
        self.assertEqual(client.inserts[2][1][0]["source_name"], "smoke")


class RealtimeChartSubscriptionContractTest(unittest.TestCase):
    def test_empty_collection_symbols_do_not_create_empty_alpaca_channel_payloads(self):
        self.assertEqual(build_subscription_request([], ["bars", "updatedBars"]), {"action": "subscribe"})

    def test_active_chart_session_writes_user_state_until_controller_reconciles(self):
        redis_client = MemoryRedis()
        manager = ActiveSymbolManager(redis_client, ttl_seconds=90, refresh_seconds=1)
        manager.refresh("user-a", "session-1", "aapl")

        keys = RedisKeyBuilder()
        self.assertEqual(redis_client.smembers(keys.user_active_chart_sessions("user-a")), {"session-1"})
        session = redis_client.hgetall(keys.user_active_chart_session("user-a", "session-1"))
        self.assertEqual(session["symbol"], "AAPL")
        self.assertEqual(redis_client.expirations[keys.user_active_chart_session("user-a", "session-1")], 90)
        self.assertNotIn(keys.subscription_symbols(), redis_client.sets)

        RealtimeSubscriptionCohortService(redis_client).reconcile()
        self.assertEqual(redis_client.smembers(keys.subscription_symbols()), {"AAPL"})
        record = redis_client.hgetall(keys.subscription_symbol("AAPL"))
        self.assertEqual(record["reason"], "active-chart-session")
        self.assertEqual(record["enabled"], "true")
        self.assertEqual(record["source"], "subscription-controller")
        self.assertEqual(set(record["layers"].split(",")), {"candles", "trades", "quotes"})

        desired = read_realtime_subscription_symbols_by_channel(
            redis_client,
            ["bars", "updatedBars", "dailyBars", "trades", "quotes"],
        )
        self.assertEqual(desired["bars"], {"AAPL"})
        self.assertEqual(desired["updatedBars"], {"AAPL"})
        self.assertEqual(desired["dailyBars"], {"AAPL"})
        self.assertEqual(desired["trades"], {"AAPL"})
        self.assertEqual(desired["quotes"], {"AAPL"})

    def test_multi_user_watchlists_aggregate_without_overwriting_each_other(self):
        redis_client = MemoryRedis()
        controller = RealtimeSubscriptionCohortService(redis_client)
        keys = RedisKeyBuilder()

        controller.replace_user_watchlist("user-a", ["NVDA", "AAPL"])
        controller.replace_user_watchlist("user-b", ["TSLA"])

        self.assertEqual(redis_client.smembers(keys.subscription_symbols()), {"NVDA", "AAPL", "TSLA"})
        self.assertEqual(redis_client.hgetall(keys.subscription_symbol("NVDA"))["watchlistUserCount"], "1")

        controller.replace_user_watchlist("user-b", ["MSFT"])

        self.assertEqual(redis_client.smembers(keys.subscription_symbols()), {"NVDA", "AAPL", "MSFT"})
        self.assertEqual(redis_client.hgetall(keys.subscription_symbol("NVDA"))["sources"], "watchlist")
        self.assertNotIn(keys.subscription_symbol("TSLA"), redis_client.hashes)

    def test_active_chart_close_preserves_watchlist_subscription_source(self):
        redis_client = MemoryRedis()
        controller = RealtimeSubscriptionCohortService(redis_client)
        keys = RedisKeyBuilder()

        controller.replace_user_watchlist("user-a", ["NVDA"])
        controller.refresh_active_chart("user-a", "session-1", "NVDA", 90)
        self.assertEqual(redis_client.hgetall(keys.subscription_symbol("NVDA"))["sources"], "active-chart,watchlist")

        controller.remove_active_chart("user-a", "session-1")

        record = redis_client.hgetall(keys.subscription_symbol("NVDA"))
        self.assertEqual(record["sources"], "watchlist")
        self.assertEqual(redis_client.smembers(keys.subscription_symbols()), {"NVDA"})

    def test_realtime_symbol_cap_prioritizes_active_chart_symbols(self):
        redis_client = MemoryRedis()
        controller = RealtimeSubscriptionCohortService(redis_client)
        keys = RedisKeyBuilder()

        controller.replace_user_watchlist("user-a", ["AAPL", "MSFT", "NVDA"])
        controller.refresh_active_chart("user-a", "session-1", "MSFT", 90)
        controller.refresh_active_chart("user-a", "session-2", "TSLA", 90)

        with mock.patch.dict(os.environ, {"ALPACA_MAX_TRADE_SYMBOLS": "2"}, clear=False):
            desired = read_realtime_subscription_symbols_by_channel(redis_client, ["trades", "quotes"])

        self.assertEqual(desired["trades"], {"MSFT", "TSLA"})
        self.assertEqual(desired["quotes"], {"MSFT", "TSLA"})
        self.assertEqual(redis_client.hgetall(keys.subscription_symbol("MSFT"))["sources"], "active-chart,watchlist")
        self.assertEqual(redis_client.hgetall(keys.subscription_symbol("TSLA"))["sources"], "active-chart")


if __name__ == "__main__":
    unittest.main()
