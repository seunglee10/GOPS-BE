# 역할: GOPS API/WebSocket이 Redis에서 최신/최근 캔들을 읽는 adapter입니다.
# 사용: 과거 API는 최근 구간 보강에, WebSocket은 live candle push에 사용합니다.
# 계약: CHART_DATA_REBUILD_PLAN.md의 Redis recent-window/live state key를 읽습니다.
import json
import os
from datetime import datetime, timezone

import redis

from alfaka.common.env import load_dotenv
from alfaka.common.redis_keys import RedisKeyBuilder
from alfaka.serving.closed_watermark import candle_watermark_value, live_candle_at_or_before_watermark
from alfaka.serving.dto import snapshot, websocket_event
from alfaka.serving.intervals import normalize_chart_interval, resolve_candle_limit
from alfaka.serving.moving_average import attach_moving_averages
from alfaka.serving.news_hot_cache import (
    DEFAULT_NEWS_MAX_ITEMS,
    DEFAULT_NEWS_RETENTION_DAYS,
    DEFAULT_NEWS_TTL_SECONDS,
    read_company_daily_summaries_from_redis,
    read_company_daily_summary_coverage_from_redis,
    read_localized_news_from_redis,
    write_company_daily_summaries_to_redis,
    write_localized_news_to_redis,
)
from alfaka.serving.time_utils import parse_utc_time


class RedisMarketDataProvider:
    def __init__(self, redis_url=None):
        load_dotenv()
        self.redis = redis.from_url(
            redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
            socket_connect_timeout=float(os.getenv("REDIS_CONNECT_TIMEOUT_SECONDS", "0.2")),
            socket_timeout=float(os.getenv("REDIS_SOCKET_TIMEOUT_SECONDS", "0.2")),
            health_check_interval=int(os.getenv("REDIS_HEALTH_CHECK_INTERVAL_SECONDS", "30")),
        )
        self.keys = RedisKeyBuilder()

    def latest_price(self, symbol):
        return self.redis.hgetall(self.keys.live_trade(symbol))

    def live_candle(self, symbol, interval="1m"):
        interval = normalize_chart_interval(interval)
        value = self.redis.get(self.keys.live_candle(symbol, interval))
        if not value:
            return None
        candle = json.loads(value)
        if not live_candle_is_fresh(candle):
            return None
        latest_closed = self.latest_closed_candle(symbol, interval)
        return None if live_candle_at_or_before_watermark(candle, self.closed_candle_watermark(symbol, interval), latest_closed) else candle

    def latest_closed_candle(self, symbol, interval):
        interval = normalize_chart_interval(interval)
        value = self.redis.get(self.keys.latest_closed_candle(symbol, interval))
        if not value:
            return None
        return json.loads(value)

    def closed_candle_watermark(self, symbol, interval):
        interval = normalize_chart_interval(interval)
        value = self.redis.get(self.keys.closed_candle_watermark(symbol, interval))
        if value:
            return value
        return candle_watermark_value(self.latest_closed_candle(symbol, interval))

    def recent_candles(self, symbol, interval, limit=None):
        interval = normalize_chart_interval(interval)
        limit = resolve_candle_limit(interval, limit)
        rows = self.redis.zrevrange(self.keys.recent_candles(symbol, interval), 0, max(0, limit - 1))
        candles = [json.loads(row) for row in reversed(rows)]
        return candles

    def candle_snapshot(self, symbol, interval, limit=None):
        interval = normalize_chart_interval(interval)
        candles = attach_moving_averages(self.recent_candles(symbol, interval, limit), overwrite=True)
        return snapshot(symbol=symbol, interval=interval, candles=candles)

    def live_event(self, symbol, interval="1m"):
        interval = normalize_chart_interval(interval)
        candle = self.live_candle(symbol, interval)
        if not candle:
            return None
        return websocket_event("LIVE_CANDLE_UPDATE", symbol, interval, candle)

    def closed_event(self, symbol, interval):
        interval = normalize_chart_interval(interval)
        value = self.redis.get(self.keys.latest_closed_candle(symbol, interval))
        if not value:
            return None
        candle = json.loads(value)
        event_type = "CANDLE_CORRECTED" if candle.get("correctionType") == "UPDATED" else "CANDLE_CLOSED"
        return websocket_event(event_type, symbol, interval, candle)

    def live_trade(self, symbol):
        values = self.redis.hgetall(self.keys.live_trade(symbol))
        return values or None

    def live_quote(self, symbol):
        value = self.redis.get(self.keys.live_quote(symbol))
        return json.loads(value) if value else None

    def live_market_event(self, symbol):
        value = self.redis.get(self.keys.live_event(symbol))
        return json.loads(value) if value else None

    def latest_status(self, symbol=None):
        key = self.keys.market_status_symbol_latest(symbol) if symbol else self.keys.market_status_latest()
        value = self.redis.get(key)
        return json.loads(value) if value else None

    def volume_profile_bins(self, symbol, from_score="-inf", to_score="+inf", limit=5000):
        rows = self.redis.zrangebyscore(self.keys.volume_profile_live(symbol), from_score, to_score, start=0, num=limit)
        return [json.loads(row) for row in rows]

    def order_flow_live_bins(self, symbol):
        values = self.redis.hgetall(self.keys.order_flow_live(symbol)) or {}
        bins = []
        for value in values.values():
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            try:
                parsed = json.loads(value) if isinstance(value, str) else value
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict):
                bins.append(parsed)
        bins.sort(key=lambda item: (str(item.get("eventMinute") or ""), float(item.get("priceBin") or 0)))
        return bins

    def symbol_metadata(self, symbol):
        value = self.redis.get(self.keys.symbol_metadata(symbol))
        return json.loads(value) if value else None

    def hot_symbols_snapshot(self):
        value = self.redis.get(self.keys.hot_symbols_snapshot())
        return json.loads(value) if value else None

    def localized_news_articles(self, symbol, limit=10, locale="ko-KR"):
        return read_localized_news_from_redis(self.redis, symbol, limit=limit, locale=locale)

    def localized_news_articles_for_symbols(self, symbols, limit=10, locale="ko-KR"):
        rows = []
        seen = set()
        for symbol in symbols or []:
            for row in self.localized_news_articles(symbol, limit=limit, locale=locale):
                key = row.get("articleId") or f"{row.get('symbol')}:{row.get('publishedAt')}:{row.get('headline')}"
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
        return sorted(rows, key=lambda item: str(item.get("publishedAt") or ""), reverse=True)[: int(limit)]

    def warm_localized_news_articles(self, rows, *, locale="ko-KR"):
        ttl_seconds = int(os.getenv("NEWS_REDIS_TTL_SECONDS", str(DEFAULT_NEWS_TTL_SECONDS)))
        max_items = int(os.getenv("NEWS_REDIS_MAX_ITEMS", str(DEFAULT_NEWS_MAX_ITEMS)))
        retention_days = int(os.getenv("NEWS_REDIS_RETENTION_DAYS", str(DEFAULT_NEWS_RETENTION_DAYS)))
        warmed = 0
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            write_localized_news_to_redis(
                self.redis,
                row,
                ttl_seconds=ttl_seconds,
                max_items=max_items,
                retention_days=retention_days,
                locale=locale,
            )
            warmed += 1
        return warmed

    def company_daily_news_summaries(self, symbol, limit=5, locale="ko-KR"):
        return read_company_daily_summaries_from_redis(self.redis, symbol, limit=limit, locale=locale)

    def company_daily_news_coverage(self, symbol, locale="ko-KR"):
        return read_company_daily_summary_coverage_from_redis(self.redis, symbol, locale=locale)

    def warm_company_daily_news_summaries(self, symbol, rows, *, days=30, limit=30, locale="ko-KR"):
        return write_company_daily_summaries_to_redis(
            self.redis,
            rows,
            symbol=symbol,
            days=days,
            limit=limit,
            locale=locale,
        )


def live_candle_is_fresh(candle, *, now=None):
    if not isinstance(candle, dict):
        return False
    max_age_seconds = read_non_negative_int_env("LIVE_CANDLE_STALE_SECONDS", default=180)
    if max_age_seconds == 0:
        return True
    timestamp = candle.get("updatedAt") or candle.get("createdAt") or candle.get("timestamp")
    if not timestamp:
        return True
    parsed = parse_utc_time(timestamp)
    if not parsed:
        return True
    current = now or datetime.now(timezone.utc)
    age_seconds = (current - parsed).total_seconds()
    return age_seconds <= max_age_seconds or age_seconds < 0


def read_non_negative_int_env(name, *, default):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default
