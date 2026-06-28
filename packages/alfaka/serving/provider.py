# 역할: GOPS Chart API가 Redis와 ClickHouse를 함께 읽을 수 있게 묶는 provider입니다.
# 사용: 먼저 Redis 최근 캔들을 보고, 부족하면 ClickHouse 과거 캔들을 조회합니다.
# 결과: GOPS CandleSnapshot 형식으로 반환합니다.
import logging
from datetime import datetime, timedelta, timezone

from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider
from alfaka.serving.cursors import timestamp_from_cursor
from alfaka.serving.dto import cursor_for, snapshot
from alfaka.serving.intervals import backfill_target_days, candle_count_for_1y, normalize_chart_interval, resolve_candle_limit, source_interval_for
from alfaka.serving.moving_average import attach_moving_averages
from alfaka.serving.redis_provider import RedisMarketDataProvider
from alfaka.serving.symbol_registry import SymbolRegistry


logger = logging.getLogger(__name__)


class MarketDataProvider:
    def __init__(self, redis_provider=None, clickhouse_provider=None):
        self.redis_provider = redis_provider or RedisMarketDataProvider()
        self.clickhouse_provider = clickhouse_provider or ClickHouseMarketDataProvider()
        self.symbol_registry = SymbolRegistry(self.clickhouse_provider, self.redis_provider)

    def candle_snapshot(self, symbol, interval, limit=None, before=None, from_time=None, to_time=None):
        interval = normalize_chart_interval(interval)
        limit = resolve_candle_limit(interval, limit)
        range_query = bool(before or from_time or to_time)
        redis_candles = [] if range_query else filter_stock_weekdays(self.redis_provider.recent_candles(symbol, interval, limit))
        if len(redis_candles) >= limit:
            payload = snapshot(symbol=symbol, interval=interval, candles=attach_moving_averages(redis_candles[-limit:]))
            return with_coverage_metadata(payload, self._coverage(symbol, interval), limit)

        clickhouse_candles = filter_stock_weekdays(self.clickhouse_provider.candles(
            symbol,
            interval,
            limit,
            before=before,
            from_time=from_time,
            to_time=to_time,
        ))
        merged = merge_candles(clickhouse_candles, redis_candles)
        candles = attach_moving_averages(merged[-limit:])
        feed = first_value(candles, "feed", "sip")
        source = first_value(candles, "source", "alpaca")
        payload = snapshot(symbol=symbol, interval=interval, candles=candles, source=source, feed=feed)
        return with_coverage_metadata(payload, self._coverage(symbol, interval, candles), limit)

    def candles_since_cursor(self, symbol, interval, cursor, limit=500):
        interval = normalize_chart_interval(interval)
        timestamp = timestamp_from_cursor(cursor)
        redis_candles = [
            candle for candle in self.redis_provider.recent_candles(symbol, interval, limit)
            if candle_after_cursor(symbol, interval, candle, cursor, timestamp)
        ]
        redis_candles = filter_stock_weekdays(redis_candles)
        try:
            clickhouse_candles = self._clickhouse_candles_since_cursor(symbol, interval, timestamp, limit)
        except Exception:
            logger.warning("ClickHouse candles_since failed; falling back to Redis recent candles.", exc_info=True)
            clickhouse_candles = []
        filtered_clickhouse = filter_stock_weekdays([
            candle for candle in clickhouse_candles
            if candle_after_cursor(symbol, interval, candle, cursor, timestamp)
        ])
        return merge_candles(filtered_clickhouse, redis_candles)[-limit:]

    def _clickhouse_candles_since_cursor(self, symbol, interval, timestamp, limit):
        if not timestamp:
            return []
        try:
            return self.clickhouse_provider.candles_since(symbol, interval, timestamp, limit, include_from=True)
        except TypeError:
            return self.clickhouse_provider.candles_since(symbol, interval, timestamp, limit)

    def _coverage(self, symbol, interval, fallback_candles=None):
        interval = normalize_chart_interval(interval)
        try:
            return self.clickhouse_provider.candle_coverage(symbol, interval)
        except Exception:
            logger.warning("ClickHouse candle coverage failed; falling back to loaded candles.", exc_info=True)
            candles = fallback_candles or []
            if not candles:
                return {"rowCount": 0, "availableFrom": None, "availableTo": None}
            return {
                "rowCount": len(candles),
                "availableFrom": candles[0].get("timestamp"),
                "availableTo": candles[-1].get("timestamp"),
            }

    def search_symbols(self, query, limit=20):
        return self.symbol_registry.search(query, limit)

    def symbol_detail(self, symbol):
        return self.symbol_registry.detail(symbol)

    def latest_status(self, symbol=None):
        return self.redis_provider.latest_status(symbol) or self.clickhouse_provider.latest_status(symbol)

    def volume_profile_bins(self, symbol, from_time, to_time, price_bin_size=None):
        try:
            bins = self.clickhouse_provider.volume_profile_bins(symbol, from_time, to_time, None if price_bin_size == "auto" else price_bin_size)
        except Exception:
            logger.warning("ClickHouse volume_profile_bins failed; falling back to Redis live bins.", exc_info=True)
            bins = []
        if not bins:
            bins = self.redis_provider.volume_profile_bins(symbol)
        resolved_size = first_value(bins, "priceBinSize", 0.05)
        return {
            "symbol": symbol,
            "from": from_time,
            "to": to_time,
            "timeBucket": "1m",
            "priceBinSize": resolved_size,
            "source": first_value(bins, "source", "clickhouse"),
            "feed": first_value(bins, "feed", "unknown"),
            "bins": bins,
        }

    def agent_chart_context(self, symbol, interval, from_time, to_time, include):
        interval = normalize_chart_interval(interval)
        candles = self.clickhouse_provider.candles_since(symbol, interval, from_time, 500)
        daily = self.clickhouse_provider.candles(symbol, "1D", 2)
        return {
            "symbol": symbol,
            "interval": interval,
            "visibleRange": {"from": from_time, "to": to_time},
            "candles": candles,
            "latestDailyCandle": daily[-1] if daily else None,
            "previousDailyCandle": daily[-2] if len(daily) > 1 else None,
            "marketStatus": self.latest_status(symbol) if "status" in include else None,
            "volumeProfile": self.volume_profile_bins(symbol, from_time, to_time, "auto") if "volumeProfile" in include else None,
            "comparisonCandidates": self.search_symbols(symbol[:2], 5),
        }


def merge_candles(*groups):
    by_timestamp = {}
    for group in groups:
        for candle in group:
            timestamp = candle.get("timestamp")
            if timestamp:
                by_timestamp[timestamp] = candle
    return [by_timestamp[key] for key in sorted(by_timestamp)]


def with_coverage_metadata(payload, coverage, requested_limit):
    interval = normalize_chart_interval(payload.get("interval"))
    source_interval = source_interval_for(interval)
    candles = payload.get("candles") or []
    oldest = candles[0].get("timestamp") if candles else None
    newest = candles[-1].get("timestamp") if candles else None
    available_from = coverage.get("availableFrom") if coverage else None
    available_to = coverage.get("availableTo") if coverage else None
    row_count = coverage.get("rowCount") if coverage else None
    invalid_row_count = coverage.get("invalidRowCount") if coverage else None
    stored_count = int(row_count) if row_count is not None else len(candles)
    target_stored_count = candle_count_for_1y(source_interval)
    target_range_from = target_range_from_for_interval(source_interval)
    payload.update({
        "requestedLimit": requested_limit,
        "returnedCount": len(candles),
        "targetStoredCount": target_stored_count,
        "targetRangeFrom": target_range_from,
        "sourceInterval": source_interval,
        "availableFrom": available_from,
        "availableTo": available_to,
        "oldestTimestamp": oldest,
        "newestTimestamp": newest,
        "hasMoreBefore": bool(oldest and available_from and available_from < oldest),
        "hasMoreAfter": bool(newest and available_to and available_to > newest),
        "storedCandleCount": stored_count,
        "invalidRowCount": int(invalid_row_count or 0),
    })
    return payload


def one_year_target_from(reference_timestamp=None):
    return target_range_from_for_interval("1m", reference_timestamp)


def target_range_from_for_interval(interval, reference_timestamp=None):
    reference = parse_iso_time(reference_timestamp) or datetime.now(timezone.utc)
    target = reference - timedelta(days=backfill_target_days(interval))
    return target.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_iso_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def candle_after_cursor(symbol, interval, candle, cursor, cursor_timestamp=None):
    if not cursor:
        return True
    cursor_timestamp = cursor_timestamp if cursor_timestamp is not None else timestamp_from_cursor(cursor)
    candle_timestamp = candle.get("timestamp") or candle.get("eventTime")
    if not candle_timestamp or not cursor_timestamp:
        return True
    if candle_timestamp > cursor_timestamp:
        return True
    if candle_timestamp < cursor_timestamp:
        return False
    return cursor_for(symbol, interval, candle) != cursor


def first_value(rows, key, fallback):
    for row in rows:
        value = row.get(key)
        if value:
            return value
    return fallback


def filter_stock_weekdays(candles):
    return [candle for candle in candles if is_stock_weekday_candle(candle)]


def is_stock_weekday_candle(candle):
    try:
        interval = normalize_chart_interval(candle.get("interval", "1m"))
    except ValueError:
        interval = "1m"
    if interval in {"1W", "1M"}:
        return True
    timestamp = candle.get("timestamp") or candle.get("eventTime")
    parsed = parse_iso_time(timestamp)
    if not parsed:
        return True
    return parsed.weekday() < 5
