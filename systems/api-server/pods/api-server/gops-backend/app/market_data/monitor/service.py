from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.market_data.fill.service import BACKGROUND_FILL_TIMEOUT_SECONDS
from app.market_data.realtime.subscription_cohorts import RealtimeSubscriptionCohortService
from app.services.alfaka_market_data import get_market_data_provider, normalize_market_symbol
from alfaka.common.env import parse_csv
from alfaka.common.redis_keys import RedisKeyBuilder
from alfaka.serving.intervals import DEFAULT_VISIBLE_BARS


ALLOWED_LAYERS = {"trades", "quotes", "events", "candles"}
DEFAULT_SUBSCRIPTION_TTL_SECONDS = 3600


def on_demand_fill_timeout_seconds() -> float:
    try:
        value = float(os.getenv(
            "ON_DEMAND_FILL_BACKGROUND_TIMEOUT_SECONDS",
            os.getenv("ON_DEMAND_FILL_TIMEOUT_SECONDS", BACKGROUND_FILL_TIMEOUT_SECONDS),
        ))
    except (TypeError, ValueError):
        return BACKGROUND_FILL_TIMEOUT_SECONDS
    return value if value > 0 else BACKGROUND_FILL_TIMEOUT_SECONDS


class MarketDataMonitorService:
    def __init__(self, provider=None, redis_client=None, keys=None):
        self.provider = provider or get_market_data_provider()
        self.redis = redis_client or getattr(getattr(self.provider, "redis_provider", None), "redis", None)
        self.keys = keys or RedisKeyBuilder()

    def overview(self) -> dict[str, Any]:
        return {
            "sourceOfTruth": "docs/CHART_DATA_REBUILD_PLAN.md",
            "mode": "on-demand",
            "redisCandleCacheLimit": dict(DEFAULT_VISIBLE_BARS),
            "quotesPersistence": "redis-websocket-s3-clickhouse",
            "rawS3Role": "backup-only",
            "feedPolicy": "sip-04:00-20:00-ET-boats-20:00-04:00-ET-exclusive",
            "subscriptions": self.subscriptions(),
            "feed": self.feed_state(),
            "fill": self.fill(),
        }

    def redis_state(self) -> dict[str, Any]:
        return {
            "keyPrefix": self.keys.prefix,
            "candleKeys": [
                "cache:candles:{symbol}:{timeframe}",
                "live:candle:{symbol}:{timeframe}",
                "latest:closed:candle:{symbol}:{timeframe}",
                "pending:replace:{symbol}:{timeframe}:{timestamp}",
                "state:candle-window:{symbol}:{timeframe}:{bucket}",
            ],
            "liveKeys": [
                "live:trade:{symbol}",
                "live:quote:{symbol}",
                "live:event:{symbol}",
            ],
            "subscriptionKeys": [
                "subscription:symbols",
                "subscription:symbol:{symbol}",
                "subscription:version",
                "subscription:events",
            ],
            "feedKeys": [
                "feed:active",
                "feed:lease:sip",
                "feed:lease:boats",
                "feed:switch:state",
                "feed:quarantine:{date}",
            ],
            "compatFeedKeys": [
                "feed:active:profile",
                "feed:active:epoch",
                "feed:switch:lock",
            ],
            "subscriptions": self.subscriptions(),
        }

    def s3_state(self) -> dict[str, Any]:
        return {
            "finalPrefixes": [
                "market-data/rebuild-20260702-lazy-v1/final/candles/feed={feed}/interval={interval}/symbol={symbol}/year=YYYY/month=MM/day=DD/*.parquet",
                "market-data/rebuild-20260702-lazy-v1/final/events/event_type={type}/symbol={symbol}/year=YYYY/month=MM/day=DD/*.parquet",
            ],
            "manifestPrefixes": [
                "market-data/rebuild-20260702-lazy-v1/manifest/candles/interval={interval}/symbol={symbol}/objects/{digest}.json",
            ],
            "rawBackupPrefixes": [
                "market-data/rebuild-20260702-lazy-v1/raw/alpaca/source=alpaca/channel={channel}/symbol={symbol}/year=YYYY/month=MM/day=DD/*.jsonl",
            ],
            "rawBackupParticipatesInReadPath": False,
        }

    def clickhouse_state(self) -> dict[str, Any]:
        return {
            "tables": [
                "market_data.chart_candles",
                "market_data.trade_ticks",
                "market_data.quote_ticks",
                "market_data.market_events",
                "market_data.market_status_events",
                "market_data.storage_object_audit",
                "market_data.load_audit",
            ],
            "excluded": ["market_data.market_quotes"],
            "quotesPersistence": "stored-in-market_data.quote_ticks",
        }

    def fill(self) -> dict[str, Any]:
        return {
            "mode": "fast-response-background-fill",
            "timeoutSeconds": on_demand_fill_timeout_seconds(),
            "sourceIntervals": {"1m": ["1m", "5m", "10m", "1h", "4h"], "1D": ["1D", "1W", "1M"]},
            "foregroundOrder": ["redis", "clickhouse"],
            "backgroundOrder": ["s3-final-manifest", "alpaca-historical"],
            "deprecatedEndpoints": [
                "POST /api/charts/backfill",
                "GET /api/charts/backfill/status",
                "GET /api/charts/backfill/queue",
            ],
        }

    def feed_state(self) -> dict[str, Any]:
        active = self._read_json_or_hash(self.keys.feed_active())
        active_profile = self._get(self.keys.feed_active_profile())
        active_epoch = self._get(self.keys.feed_active_epoch())
        if not active and (active_profile or active_epoch):
            active = {"activeFeedProfile": active_profile, "epoch": active_epoch}
        return {
            "active": active or {},
            "leases": {
                "sip": self._get(self.keys.feed_lease("sip")),
                "boats": self._get(self.keys.feed_lease("boats")),
            },
            "switchState": self._read_json_or_hash(self.keys.feed_switch_state()),
            "policy": {"sip": "04:00-20:00 ET", "boats": "20:00-04:00 ET", "mutualExclusion": True},
        }

    def subscriptions(self) -> dict[str, Any]:
        symbols = self._set_members(self.keys.subscription_symbols())
        records = [self._subscription_record(symbol) for symbol in symbols]
        return {
            "version": self._get(self.keys.subscription_version()) or "0",
            "symbols": records,
        }

    def realtime(self, symbol: str | None = None, interval: str = "1m") -> dict[str, Any]:
        normalized_symbol = normalize_market_symbol(symbol) if symbol else None
        subscription_symbols = self._set_members(self.keys.subscription_symbols())
        active_chart_symbols = self._set_members(self.keys.active_symbols())
        global_processor_health = self._component_health("market-processor")
        symbol_health = self._component_health(f"market-processor:symbol:{normalized_symbol}") if normalized_symbol else None
        feed_profile = (
            (symbol_health or {}).get("lastFeedProfile")
            or (global_processor_health or {}).get("lastFeedProfile")
        )
        feed_health = self._component_health(f"market-processor:feed:{feed_profile}") if feed_profile else None
        payload: dict[str, Any] = {
            "checkedAt": now_iso(),
            "subscriptionVersion": self._get(self.keys.subscription_version()) or "0",
            "subscriptionSymbolCount": len(subscription_symbols),
            "activeChartSymbolCount": len(active_chart_symbols),
            "globalProcessorHealth": global_processor_health,
            "symbolProcessorHealth": symbol_health,
            "feedProcessorHealth": feed_health,
            "kafka": self._kafka_lag(),
        }
        if normalized_symbol:
            live_trade_key = self.keys.live_trade(normalized_symbol)
            live_quote_key = self.keys.live_quote(normalized_symbol)
            live_candle_key = self.keys.live_candle(normalized_symbol, interval or "1m")
            subscription_record = self._subscription_record(normalized_symbol)
            live_trade = self._read_json_or_hash(live_trade_key)
            live_quote = self._read_json_or_hash(live_quote_key)
            live_candle = self._read_json_or_hash(live_candle_key)
            payload["symbol"] = {
                "symbol": normalized_symbol,
                "interval": interval or "1m",
                "subscribed": normalized_symbol in subscription_symbols,
                "activeChart": normalized_symbol in active_chart_symbols,
                "subscription": subscription_record,
                "liveTradeKey": live_trade_key,
                "liveQuoteKey": live_quote_key,
                "liveCandleKey": live_candle_key,
                "liveTradePresent": bool(live_trade),
                "liveQuotePresent": bool(live_quote),
                "liveCandlePresent": bool(live_candle),
                "liveTradeAgeSeconds": event_age_seconds(live_trade),
                "liveQuoteAgeSeconds": event_age_seconds(live_quote),
                "liveCandleAgeSeconds": event_age_seconds(live_candle),
                "liveTradeTtlSeconds": self._ttl(live_trade_key),
                "liveQuoteTtlSeconds": self._ttl(live_quote_key),
                "liveCandleTtlSeconds": self._ttl(live_candle_key),
                "liveTradeTimestamp": first_present(live_trade, ("updatedAt", "receivedAt", "timestamp", "eventTime")),
                "liveQuoteTimestamp": first_present(live_quote, ("updatedAt", "receivedAt", "timestamp", "eventTime")),
                "liveCandleTimestamp": first_present(live_candle, ("updatedAt", "timestamp", "eventTime")),
            }
        return payload

    def add_subscription(self, symbol: str, layers: list[str], reason: str | None = None, ttl_seconds: int | None = None) -> dict[str, Any]:
        symbol = normalize_market_symbol(symbol)
        requested_layers = normalize_layers(layers)
        existing_layers = set(self._subscription_record(symbol).get("layers") or [])
        if "quotes" in requested_layers and "trades" not in requested_layers and "trades" not in existing_layers:
            raise HTTPException(status_code=400, detail="quotes layer requires an existing or requested trades realtime subscription for the same symbol")
        record = RealtimeSubscriptionCohortService(self.redis, self.keys, auto_reconcile=False).add_manual_source(symbol, requested_layers)
        return {"subscription": self._subscription_record(symbol) or record, "version": self._get(self.keys.subscription_version()) or "0", "pendingReconcile": True}

    def remove_subscription(self, symbol: str) -> dict[str, Any]:
        symbol = normalize_market_symbol(symbol)
        RealtimeSubscriptionCohortService(self.redis, self.keys, auto_reconcile=False).remove_manual_source(symbol)
        return {"symbol": symbol, "removed": True, "version": self._get(self.keys.subscription_version()) or "0", "pendingReconcile": True}

    def _subscription_record(self, symbol: str) -> dict[str, Any]:
        raw = self._hgetall(self.keys.subscription_symbol(symbol))
        layers = raw.get("layers", [])
        if isinstance(layers, str):
            layers = [item for item in layers.split(",") if item]
        return {
            "symbol": symbol,
            "layers": sorted(set(layers)),
            "sources": sorted(set([item for item in str(raw.get("sources") or "").split(",") if item])),
            "reason": raw.get("reason"),
            "ttlSeconds": int(raw.get("ttlSeconds") or 0),
            "enabled": raw.get("enabled") in {True, "true", "True", "1", 1},
            "updatedAt": raw.get("updatedAt"),
        }

    def _get(self, key: str) -> Any:
        if not self.redis:
            return None
        method = getattr(self.redis, "get", None)
        if not callable(method):
            return None
        try:
            value = method(key)
        except Exception:
            return None
        return value.decode("utf-8") if isinstance(value, bytes) else value

    def _read_json_or_hash(self, key: str) -> dict[str, Any]:
        value = self._get(key)
        if value:
            try:
                return json.loads(value)
            except Exception:
                return {"value": value}
        return self._hgetall(key)

    def _hgetall(self, key: str) -> dict[str, Any]:
        if not self.redis:
            return {}
        method = getattr(self.redis, "hgetall", None)
        if not callable(method):
            return {}
        try:
            raw = method(key) or {}
        except Exception:
            return {}
        return {decode(k): decode(v) for k, v in raw.items()}

    def _ttl(self, key: str) -> int | None:
        if not self.redis:
            return None
        method = getattr(self.redis, "ttl", None)
        if not callable(method):
            return None
        try:
            return int(method(key))
        except Exception:
            return None

    def _component_health(self, component: str) -> dict[str, Any] | None:
        value = self._get(self.keys.component_health(component))
        if not value:
            return None
        try:
            parsed = json.loads(value)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _kafka_lag(self) -> dict[str, Any]:
        bootstrap_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS")
        if not bootstrap_servers:
            return {"enabled": False, "reason": "KAFKA_BOOTSTRAP_SERVERS is not configured"}
        processor_group_id = os.getenv("KAFKA_PROCESSOR_GROUP_ID", "alfaka-market-processor")
        raw_topics = {
            "rawTrades": os.getenv("KAFKA_TRADES_TOPIC", "market.input.realtime.trades.v1"),
            "rawQuotes": os.getenv("KAFKA_QUOTES_TOPIC", "market.input.realtime.quotes.v1"),
            "rawEvents": os.getenv("KAFKA_EVENTS_TOPIC", "market.input.realtime.events.v1"),
        }
        layer_topics = {
            "tickFanoutTrades": os.getenv("KAFKA_TRADES_LAYER_TOPIC", "market.layer.trades.v1"),
            "tickFanoutQuotes": os.getenv("KAFKA_QUOTES_LAYER_TOPIC", "market.layer.quotes.v1"),
            "tickFanoutLiveCandles": os.getenv("KAFKA_LIVE_CANDLE_TOPIC", "market.layer.candles.live.v1"),
        }
        try:
            from kafka import KafkaConsumer, TopicPartition

            consumer = KafkaConsumer(
                bootstrap_servers=parse_csv(bootstrap_servers),
                group_id=processor_group_id,
                enable_auto_commit=False,
                consumer_timeout_ms=750,
            )
            topics = {**raw_topics, **layer_topics}
            all_topics = consumer.topics()
            missing = sorted(topic for topic in topics.values() if topic not in all_topics)
            partitions: list[TopicPartition] = []
            labels_by_topic = {topic: label for label, topic in topics.items()}
            for topic in topics.values():
                partitions.extend(TopicPartition(topic, partition) for partition in consumer.partitions_for_topic(topic) or [])
            result = {label: {"topic": topic, "lag": None, "partitions": {}} for label, topic in topics.items()}
            if partitions:
                consumer.assign(partitions)
                end_offsets = consumer.end_offsets(partitions)
                for partition in partitions:
                    committed = consumer.committed(partition)
                    end_offset = end_offsets.get(partition, 0)
                    lag = None if committed is None else max(0, end_offset - committed)
                    label = labels_by_topic.get(partition.topic, partition.topic)
                    result[label]["partitions"][str(partition.partition)] = {
                        "committed": committed,
                        "endOffset": end_offset,
                        "lag": lag,
                    }
                    if lag is not None:
                        current = result[label]["lag"]
                        result[label]["lag"] = int(lag) if current is None else int(current) + int(lag)
            consumer.close()
            return {
                "enabled": True,
                "processorGroupId": processor_group_id,
                "missingTopics": missing,
                "rawTradesLag": result["rawTrades"]["lag"],
                "rawQuotesLag": result["rawQuotes"]["lag"],
                "tickFanoutLag": sum_lag([result["rawTrades"]["lag"], result["rawQuotes"]["lag"]]),
                "topics": result,
            }
        except Exception as exc:
            return {"enabled": False, "error": str(exc), "processorGroupId": processor_group_id}

    def _hset(self, key: str, values: dict[str, Any]) -> None:
        if not self.redis:
            return
        mapping = {k: encode_hash_value(v) for k, v in values.items()}
        method = getattr(self.redis, "hset", None)
        if callable(method):
            method(key, mapping=mapping)

    def _set_members(self, key: str) -> list[str]:
        if not self.redis:
            return []
        method = getattr(self.redis, "smembers", None)
        if not callable(method):
            return []
        return sorted(decode(value) for value in method(key) or [])

    def _sadd(self, key: str, value: str) -> None:
        method = getattr(self.redis, "sadd", None) if self.redis else None
        if callable(method):
            method(key, value)

    def _srem(self, key: str, value: str) -> None:
        method = getattr(self.redis, "srem", None) if self.redis else None
        if callable(method):
            method(key, value)

    def _delete(self, key: str) -> None:
        method = getattr(self.redis, "delete", None) if self.redis else None
        if callable(method):
            method(key)

    def _expire(self, key: str, ttl: int) -> None:
        method = getattr(self.redis, "expire", None) if self.redis else None
        if callable(method):
            method(key, ttl)

    def _incr(self, key: str) -> int:
        method = getattr(self.redis, "incr", None) if self.redis else None
        if callable(method):
            return int(method(key))
        return 0

    def _xadd(self, key: str, fields: dict[str, str]) -> None:
        method = getattr(self.redis, "xadd", None) if self.redis else None
        if callable(method):
            method(key, fields)


def normalize_layers(layers: list[str]) -> set[str]:
    normalized = {str(layer).strip().lower() for layer in layers or [] if str(layer).strip()}
    invalid = sorted(normalized - ALLOWED_LAYERS)
    if invalid:
        raise HTTPException(status_code=400, detail=f"Unsupported subscription layers: {', '.join(invalid)}")
    if not normalized:
        raise HTTPException(status_code=400, detail="At least one subscription layer is required")
    return normalized


def normalize_ttl(value: int | None) -> int:
    if value is None:
        return DEFAULT_SUBSCRIPTION_TTL_SECONDS
    return max(60, min(int(value), 86400))


def encode_hash_value(value: Any) -> str:
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def decode(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def first_present(payload: dict[str, Any], names: tuple[str, ...]) -> Any:
    if not isinstance(payload, dict):
        return None
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            return value
    return None


def event_age_seconds(payload: dict[str, Any]) -> float | None:
    value = first_present(payload, ("updatedAt", "receivedAt", "timestamp", "eventTime", "t"))
    parsed = parse_timestamp(value)
    if not parsed:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def sum_lag(values: list[int | None]) -> int | None:
    present = [int(value) for value in values if value is not None]
    if not present:
        return None
    return sum(present)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def get_monitor_service() -> MarketDataMonitorService:
    return MarketDataMonitorService()
