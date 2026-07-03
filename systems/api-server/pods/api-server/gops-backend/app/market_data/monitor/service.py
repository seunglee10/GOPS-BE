from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.market_data.realtime.subscription_cohorts import RealtimeSubscriptionCohortService
from app.services.alfaka_market_data import get_market_data_provider, normalize_market_symbol
from alfaka.common.redis_keys import RedisKeyBuilder


ALLOWED_LAYERS = {"trades", "quotes", "events", "candles"}
DEFAULT_SUBSCRIPTION_TTL_SECONDS = 3600


class MarketDataMonitorService:
    def __init__(self, provider=None, redis_client=None, keys=None):
        self.provider = provider or get_market_data_provider()
        self.redis = redis_client or getattr(getattr(self.provider, "redis_provider", None), "redis", None)
        self.keys = keys or RedisKeyBuilder()

    def overview(self) -> dict[str, Any]:
        return {
            "sourceOfTruth": "docs/CHART_DATA_REBUILD_PLAN.md",
            "mode": "on-demand",
            "redisCandleCacheLimit": 120,
            "quotesPersistence": "redis-websocket-s3-clickhouse",
            "rawS3Role": "backup-only",
            "feedPolicy": "sip-04:00-20:00-ET-boats-20:00-04:00-ET-exclusive",
            "subscriptions": self.subscriptions(),
            "feed": self.feed_state(),
            "backfill": self.backfill(),
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
                "market-data/rebuild-20260702-lazy-v1/final/trades/symbol={symbol}/year=YYYY/month=MM/day=DD/feed={feed}/*.parquet",
                "market-data/rebuild-20260702-lazy-v1/final/quotes/symbol={symbol}/year=YYYY/month=MM/day=DD/feed={feed}/*.parquet",
                "market-data/rebuild-20260702-lazy-v1/final/events/event_type={type}/symbol={symbol}/year=YYYY/month=MM/day=DD/*.parquet",
            ],
            "manifestPrefixes": [
                "market-data/rebuild-20260702-lazy-v1/manifest/candles/interval={interval}/symbol={symbol}/objects/{digest}.json",
                "market-data/rebuild-20260702-lazy-v1/manifest/backfill/request={requestId}.json",
            ],
            "rawBackupPrefixes": [
                "market-data/rebuild-20260702-lazy-v1/raw/alpaca/source=alpaca/channel={channel}/symbol={symbol}/year=YYYY/month=MM/day=DD/*.jsonl",
                "market-data/rebuild-20260702-lazy-v1/raw/alpaca/source=alpaca/channel={bars|daily-bars}/symbol={symbol}/request={requestId}/*.jsonl",
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
                "market_data.backfill_jobs",
                "market_data.storage_object_audit",
                "market_data.load_audit",
            ],
            "excluded": ["market_data.market_quotes"],
            "quotesPersistence": "stored-in-market_data.quote_ticks",
        }

    def backfill(self) -> dict[str, Any]:
        stream = self.keys.backfill_stream()
        dead_letter = self.keys.backfill_dead_letter_stream()
        return {
            "stream": stream,
            "deadLetter": dead_letter,
            "queue": self._stream_length(stream),
            "deadLetterQueue": self._stream_length(dead_letter),
            "sourceIntervals": {"1m": ["1m", "5m", "10m"], "1D": ["1D", "1W", "1M"]},
            "coverageOrder": ["redis", "clickhouse", "s3-manifest", "alpaca-backfill"],
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
        value = method(key)
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
        raw = method(key) or {}
        return {decode(k): decode(v) for k, v in raw.items()}

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

    def _stream_length(self, key: str) -> int | None:
        method = getattr(self.redis, "xlen", None) if self.redis else None
        if callable(method):
            return int(method(key))
        return None


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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def get_monitor_service() -> MarketDataMonitorService:
    return MarketDataMonitorService()
