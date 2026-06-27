import os
import sys
import types
import unittest
from unittest import mock
from pathlib import Path

sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda *args, **kwargs: None))
sys.modules.setdefault("botocore", types.SimpleNamespace())
sys.modules.setdefault("botocore.config", types.SimpleNamespace(Config=lambda **kwargs: kwargs))
sys.modules.setdefault("redis", types.SimpleNamespace(from_url=lambda *args, **kwargs: None))

from alfaka.alpaca.subscription import (
    configured_seed_symbols,
    configured_universe_symbols,
    load_request_config,
    load_symbols_and_channels,
    resolve_request_config_path,
)
from alfaka.alpaca.assets import asset_to_symbol_metadata
from alfaka.common.market_messages import build_raw_envelope, raw_topic_name, source_event_id
from alfaka.common.redis_keys import RedisKeyBuilder
from alfaka.backfill.runner import build_sample_raw_bars, raw_bar_to_processed_candle, raw_bars_to_processed_candles
from alfaka.backfill.status import RedisBackfillStore, default_backfill_range
from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider
from alfaka.serving.cursors import timestamp_from_cursor
from alfaka.serving.dto import cursor_for, market_status_event, snapshot, websocket_event
from alfaka.serving.intervals import candle_count_for_1y, candle_count_for_24h, resolve_candle_limit
from alfaka.serving.provider import MarketDataProvider
from alfaka.serving.symbol_registry import SymbolRegistry
from alfaka.storage.clickhouse_loader import candle_to_clickhouse_row, status_to_clickhouse_row, symbol_to_clickhouse_row
from alfaka.storage.s3_materializer import (
    detect_s3_object_format,
    list_s3_objects,
    materialize_processed_rows,
    normalize_processed_candle_row,
)
from alfaka.storage.processed_s3_sink import normalize_storage_row, s3_partition_key
from alfaka.streaming.transforms import (
    CandleAggregator,
    VolumeProfileBinBuilder,
    normalize_bar,
    normalize_status,
    normalize_trade,
)


class FakeRedisProvider:
    def __init__(self, candles=None, symbol_metadata=None, profile_bins=None):
        self._candles = candles or []
        self._symbol_metadata = symbol_metadata or {}
        self._profile_bins = profile_bins or []

    def recent_candles(self, symbol, interval, limit):
        return list(self._candles)[-limit:]

    def symbol_metadata(self, symbol):
        return self._symbol_metadata.get(symbol)

    def volume_profile_bins(self, symbol):
        return list(self._profile_bins)


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


class FailingClickHouseProvider(FakeClickHouseProvider):
    def candles_since(self, symbol, interval, timestamp, limit=500, include_from=False):
        raise RuntimeError("clickhouse down")

    def volume_profile_bins(self, symbol, from_time, to_time, price_bin_size=None):
        raise RuntimeError("clickhouse down")

    def candle_coverage(self, symbol, interval):
        raise RuntimeError("clickhouse down")


class RecordingClickHouseClient:
    def __init__(self):
        self.inserts = []

    def insert_json_each_row(self, table, rows):
        self.inserts.append((table, list(rows)))


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.queues = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def lpush(self, key, value):
        self.queues.setdefault(key, []).insert(0, value)
        return len(self.queues[key])

    def brpop(self, key, timeout=0):
        values = self.queues.get(key) or []
        if not values:
            return None
        return key, values.pop()


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


class MarketDataHardeningContractTest(unittest.TestCase):
    def test_raw_envelope_has_source_event_id_and_topic(self):
        message = {"T": "t", "S": "AAPL", "i": 123, "p": 195.2, "s": 10, "t": "2026-06-25T10:15:20.100Z"}
        envelope = build_raw_envelope(message, "sip")

        self.assertEqual(envelope["channel"], "trades")
        self.assertEqual(envelope["symbol"], "AAPL")
        self.assertEqual(raw_topic_name("market.raw", "t"), "market.raw.trades")
        self.assertIn("sourceEventId", envelope)
        self.assertEqual(envelope["sourceEventId"], "alpaca/sip/trades/AAPL/123/2026-06-25T10:15:20.100Z")

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

    def test_daily_bar_normalizes_to_1d_candle(self):
        envelope = build_raw_envelope(
            {"T": "d", "S": "AAPL", "t": "2026-06-25T00:00:00.000Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 100},
            "sip",
        )
        candle = normalize_bar(envelope)

        self.assertEqual(candle["interval"], "1d")
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
            "sourceEventId": envelope["sourceEventId"],
        })

        self.assertEqual(profile_bin["eventType"], "VOLUME_PROFILE_BIN")
        self.assertEqual(profile_bin["priceBinSize"], 0.05)
        self.assertIn("eventId", event)
        self.assertIn("cursor", event)

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

    def test_backfill_store_deduplicates_same_symbol_interval_range(self):
        store = RedisBackfillStore(redis_client=MemoryRedis(), ttl_seconds=60)

        first, first_deduped = store.create_request(
            "INTC",
            "1m",
            start="2026-06-25T13:30:00.000Z",
            end="2026-06-25T14:30:00.000Z",
            mode="sample-dev",
        )
        second, second_deduped = store.create_request(
            "INTC",
            "1m",
            start="2026-06-25T13:30:00.000Z",
            end="2026-06-25T14:30:00.000Z",
            mode="sample-dev",
        )

        self.assertFalse(first_deduped)
        self.assertTrue(second_deduped)
        self.assertEqual(first["requestId"], second["requestId"])
        self.assertEqual(store.latest_status("INTC", "1m")["status"], "queued")
        self.assertEqual(store.pop_queued_request_id(), first["requestId"])
        self.assertIsNone(store.pop_queued_request_id())

    def test_default_backfill_range_is_minute_stable_for_auto_requests(self):
        with mock.patch.dict(os.environ, {"BACKFILL_DEFAULT_LOOKBACK_HOURS": "8760"}):
            first = default_backfill_range(now="2026-06-25T14:30:11.123Z")
            second = default_backfill_range(now="2026-06-25T14:30:59.999Z")

        self.assertEqual(first, second)
        self.assertEqual(first.start, "2025-06-25T14:30:00.000Z")
        self.assertEqual(first.end, "2026-06-25T14:30:00.000Z")

    def test_chart_candle_limit_defaults_to_twenty_four_hours(self):
        self.assertEqual(candle_count_for_24h("1m"), 1440)
        self.assertEqual(candle_count_for_24h("5m"), 288)
        self.assertEqual(candle_count_for_24h("10m"), 144)
        self.assertEqual(candle_count_for_24h("1d"), 1)
        self.assertEqual(candle_count_for_1y("1m"), 525600)
        self.assertEqual(resolve_candle_limit("1m", None), 1440)
        self.assertEqual(resolve_candle_limit("1m", 9999), 9999)
        self.assertEqual(resolve_candle_limit("1m", 999999), 525600)

    def test_clickhouse_provider_uses_database_override(self):
        provider = ClickHouseMarketDataProvider(database="custom_market_data")

        self.assertEqual(provider.table("symbols"), "custom_market_data.symbols")
        with self.assertRaises(ValueError):
            provider.table("bad-table-name")

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
        root = Path(__file__).resolve().parents[1]
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
        }
        aws_topics = {
            line.strip()
            for line in (root / "infra" / "aws" / "msk" / "topics.txt").read_text(encoding="utf-8").splitlines()
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

    def test_alpaca_universe_and_symbols_env_are_separated(self):
        previous_universe = os.environ.get("ALPACA_UNIVERSE")
        previous = os.environ.get("ALPACA_SYMBOLS")
        os.environ["ALPACA_UNIVERSE"] = "semiconductor-100"
        os.environ["ALPACA_SYMBOLS"] = "NVDA,AMD,AVGO,TSM,ASML,AMAT,MU"
        try:
            self.assertEqual(configured_seed_symbols(), ["NVDA", "AMD", "AVGO", "TSM", "ASML", "AMAT", "MU"])
            universe = configured_universe_symbols()
            self.assertIn("INTC", universe)
            self.assertIn("ASML", universe)
            self.assertIn("AMAT", universe)

            registry = SymbolRegistry(
                clickhouse_provider=FakeClickHouseProvider(symbols={}),
                redis_provider=FakeRedisProvider(symbol_metadata={}),
            )

            intc_detail = registry.detail("INTC")
            self.assertEqual(intc_detail["symbol"], "INTC")
            self.assertEqual(intc_detail["name"], "Intel Corporation")
            self.assertEqual(intc_detail["market"], "NASDAQ")
            asml_results = registry.search("asml", 5)
            self.assertEqual([item["symbol"] for item in asml_results], ["ASML"])
            self.assertEqual(asml_results[0]["market"], "NASDAQ")
            empty_results = registry.search("", 5)
            self.assertEqual([item["symbol"] for item in empty_results], ["NVDA", "AMD", "AVGO", "INTC", "QCOM"])
        finally:
            if previous_universe is None:
                os.environ.pop("ALPACA_UNIVERSE", None)
            else:
                os.environ["ALPACA_UNIVERSE"] = previous_universe
            if previous is None:
                os.environ.pop("ALPACA_SYMBOLS", None)
            else:
                os.environ["ALPACA_SYMBOLS"] = previous

    def test_request_config_path_resolves_repo_relative_env(self):
        previous_config = os.environ.get("ALFAKA_REQUEST_CONFIG")
        previous_cwd = Path.cwd()
        repo_root = Path(__file__).resolve().parents[1]
        os.environ["ALFAKA_REQUEST_CONFIG"] = "config/market-data-request.json"
        try:
            os.chdir(repo_root / "services" / "07-api-websocket" / "gops-backend")
            self.assertTrue(resolve_request_config_path().exists())
            config = load_request_config()
            self.assertEqual(config["defaultUniverse"], "semiconductor-100")
            self.assertIn("INTC", config["defaultSymbols"])
        finally:
            os.chdir(previous_cwd)
            if previous_config is None:
                os.environ.pop("ALFAKA_REQUEST_CONFIG", None)
            else:
                os.environ["ALFAKA_REQUEST_CONFIG"] = previous_config

    def test_alpaca_symbols_rejects_universe_name(self):
        previous = os.environ.get("ALPACA_SYMBOLS")
        os.environ["ALPACA_SYMBOLS"] = "semiconductor-100"
        try:
            with self.assertRaises(ValueError):
                load_symbols_and_channels()
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

    def test_empty_cursor_does_not_trigger_clickhouse_timestamp_query(self):
        provider = MarketDataProvider(
            redis_provider=FakeRedisProvider(),
            clickhouse_provider=FailingClickHouseProvider(),
        )

        self.assertIsNone(timestamp_from_cursor("v1:INTC:1m:empty:00000000"))
        with self.assertNoLogs("alfaka.serving.provider", level="WARNING"):
            candles = provider.candles_since_cursor("INTC", "1m", "v1:INTC:1m:empty:00000000")

        self.assertEqual(candles, [])

    def test_sample_backfill_reuses_processed_candle_and_clickhouse_contract(self):
        raw_bar = build_sample_raw_bars("INTC", "2026-06-25T13:30:00.000Z", "2026-06-25T13:33:00.000Z", count=1)[0]
        candle = raw_bar_to_processed_candle("INTC", raw_bar, feed="sip")
        row = candle_to_clickhouse_row(candle)

        self.assertEqual(candle["eventType"], "CANDLE")
        self.assertEqual(candle["symbol"], "INTC")
        self.assertEqual(candle["interval"], "1m")
        self.assertEqual(candle["source"], "alpaca.bars")
        self.assertIn("sourceEventId", candle)
        self.assertEqual(row["symbol"], "INTC")
        self.assertEqual(row["interval"], "1m")

    def test_backfill_processed_candles_include_moving_averages(self):
        raw_bars = build_sample_raw_bars("INTC", "2026-06-25T13:30:00.000Z", "2026-06-25T13:35:00.000Z", count=5)
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
            "sourceEventId": "event-1",
        }

        normalized = normalize_processed_candle_row(source_row)
        updated_duplicate = {**source_row, "close": 10.8, "sourceEventId": "event-2"}
        result = materialize_processed_rows(client, "s3://bucket/market-data/final/candles/part-1.jsonl", [source_row, updated_duplicate])

        self.assertEqual(normalized["ma"]["ma5"], 10.1)
        self.assertEqual(result["rowCount"], 1)
        self.assertEqual(client.inserts[0][0], "chart_candles")
        self.assertEqual(client.inserts[0][1][0]["source_event_id"], "event-2")
        self.assertEqual(client.inserts[0][1][0]["close"], 10.8)
        self.assertEqual(client.inserts[1][0], "load_audit")
        self.assertEqual(client.inserts[1][1][0]["object_path"], "s3://bucket/market-data/final/candles/part-1.jsonl")

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


if __name__ == "__main__":
    unittest.main()
