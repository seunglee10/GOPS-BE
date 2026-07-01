# 역할: GOPS Chart API가 Redis와 ClickHouse를 함께 읽을 수 있게 묶는 provider입니다.
# 사용: 최신 snapshot은 Redis 최근 캔들을 보강하고, 명시적 범위 요청은 ClickHouse를 조회합니다.
# 결과: GOPS CandleSnapshot 형식으로 반환합니다.
import logging
from datetime import datetime, time, timedelta, timezone

from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider
from alfaka.serving.cursors import timestamp_from_cursor
from alfaka.serving.dto import cursor_for, snapshot
from alfaka.serving.history_window import history_floor_iso, later_iso
from alfaka.serving.intervals import (
    normalize_chart_interval,
    resolve_candle_limit,
    source_interval_for,
)
from alfaka.serving.moving_average import MA_WINDOWS, attach_moving_averages
from alfaka.serving.redis_provider import RedisMarketDataProvider
from alfaka.serving.symbol_registry import SymbolRegistry
from alfaka.serving.time_utils import canonical_utc_timestamp, parse_utc_time


logger = logging.getLogger(__name__)


class MarketDataProvider:
    def __init__(self, redis_provider=None, clickhouse_provider=None):
        self.redis_provider = redis_provider or RedisMarketDataProvider()
        self.clickhouse_provider = clickhouse_provider or ClickHouseMarketDataProvider()
        self.symbol_registry = SymbolRegistry(self.clickhouse_provider, self.redis_provider)

    def candle_snapshot(self, symbol, interval, limit=None, before=None, from_time=None, to_time=None):
        interval = normalize_chart_interval(interval)
        limit = resolve_candle_limit(interval, limit)
        query_limit = moving_average_query_limit(interval, limit)
        range_query = bool(before or from_time or to_time)
        redis_candles = [] if range_query else filter_stock_weekdays(self.redis_provider.recent_candles(symbol, interval, query_limit))
        live_candle = None if range_query else self._live_candle(symbol, interval)
        coverage = None
        requested_range = requested_range_payload(before=before, from_time=from_time, to_time=to_time)
        if len(redis_candles) >= query_limit:
            merged_redis = merge_candles(redis_candles, [live_candle] if live_candle else [])
            coverage = self._coverage(symbol, interval)
            if not candles_are_behind_coverage(merged_redis, coverage):
                payload = snapshot(symbol=symbol, interval=interval, candles=attach_moving_averages(merged_redis)[-limit:])
                return with_coverage_metadata(
                    payload,
                    coverage,
                    limit,
                    requested_range=requested_range,
                    no_data_before=self._no_data_before(symbol, interval),
                )

        clickhouse_candles = filter_stock_weekdays(self.clickhouse_provider.candles(
            symbol,
            interval,
            query_limit,
            before=before,
            from_time=from_time,
            to_time=to_time,
        ))
        live_group = [live_candle] if live_candle else []
        merged = merge_candles(clickhouse_candles, redis_candles, live_group)
        candles = attach_moving_averages(merged)[-limit:]
        feed = first_value(candles, "feed", "sip")
        source = first_value(candles, "source", "alpaca")
        payload = snapshot(symbol=symbol, interval=interval, candles=candles, source=source, feed=feed)
        return with_coverage_metadata(
            payload,
            coverage or self._coverage(symbol, interval, candles),
            limit,
            requested_range=requested_range,
            no_data_before=self._no_data_before(symbol, interval),
        )

    def candles_since_cursor(self, symbol, interval, cursor, limit=500):
        interval = normalize_chart_interval(interval)
        timestamp = timestamp_from_cursor(cursor)
        redis_candles = [
            candle for candle in self.redis_provider.recent_candles(symbol, interval, limit)
            if candle_after_cursor(symbol, interval, candle, cursor, timestamp)
        ]
        redis_candles = filter_stock_weekdays(redis_candles)
        live_candle = self._live_candle(symbol, interval)
        live_candles = [live_candle] if live_candle and candle_after_cursor(symbol, interval, live_candle, cursor, timestamp) else []
        try:
            clickhouse_candles = self._clickhouse_candles_since_cursor(symbol, interval, timestamp, limit)
        except Exception:
            logger.warning("ClickHouse candles_since failed; falling back to Redis recent candles.", exc_info=True)
            clickhouse_candles = []
        filtered_clickhouse = filter_stock_weekdays([
            candle for candle in clickhouse_candles
            if candle_after_cursor(symbol, interval, candle, cursor, timestamp)
        ])
        return merge_candles(filtered_clickhouse, redis_candles, live_candles)[-limit:]

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

    def _live_candle(self, symbol, interval):
        method = getattr(self.redis_provider, "live_candle", None)
        if not callable(method):
            return None
        try:
            return method(symbol, interval)
        except Exception:
            logger.warning("Redis live candle lookup failed for %s %s.", symbol, interval, exc_info=True)
            return None

    def _no_data_before(self, symbol, interval):
        history_floor = history_floor_iso(interval)
        method = getattr(self.redis_provider, "backfill_no_data_before", None)
        if not callable(method):
            return history_floor
        try:
            return later_iso(method(symbol, source_interval_for(interval)), history_floor)
        except Exception:
            logger.warning("Redis no-data boundary lookup failed for %s %s.", symbol, interval, exc_info=True)
            return history_floor

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
    priority_by_timestamp = {}
    sequence = 0
    for group in groups:
        for candle in group:
            if not candle:
                continue
            timestamp = canonical_candle_timestamp(candle)
            if timestamp:
                priority = candle_merge_priority(candle, timestamp, sequence)
                if timestamp not in by_timestamp or priority >= priority_by_timestamp[timestamp]:
                    by_timestamp[timestamp] = {**candle, "timestamp": timestamp}
                    priority_by_timestamp[timestamp] = priority
            sequence += 1
    return [by_timestamp[key] for key in sorted(by_timestamp)]


def candle_merge_priority(candle, canonical_timestamp, sequence):
    original_timestamp = canonical_utc_timestamp(candle.get("timestamp") or candle.get("eventTime"))
    exact_bucket_timestamp = 1 if original_timestamp == canonical_timestamp else 0
    observed_at = (
        parse_utc_time(candle.get("createdAt"))
        or parse_utc_time(candle.get("receivedAt"))
        or parse_utc_time(candle.get("updatedAt"))
    )
    observed_at_ms = int(observed_at.timestamp() * 1000) if observed_at else 0
    return exact_bucket_timestamp, observed_at_ms, sequence


def canonical_candle_timestamp(candle):
    timestamp = candle.get("timestamp")
    interval = normalize_chart_interval(candle.get("interval", "1m"))
    parsed = parse_utc_time(timestamp)
    if not parsed:
        return canonical_utc_timestamp(timestamp)
    if interval == "1D":
        return utc_bucket_iso(datetime.combine(parsed.date(), time(0, 0), timezone.utc))
    if interval == "1W":
        bucket = parsed.date() - timedelta(days=parsed.weekday())
        return utc_bucket_iso(datetime.combine(bucket, time(0, 0), timezone.utc))
    if interval == "1M":
        bucket = parsed.date().replace(day=1)
        return utc_bucket_iso(datetime.combine(bucket, time(0, 0), timezone.utc))
    return canonical_utc_timestamp(timestamp)


def utc_bucket_iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def with_coverage_metadata(payload, coverage, requested_limit, *, requested_range=None, no_data_before=None):
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
    payload.update({
        "requestedLimit": requested_limit,
        "returnedCount": len(candles),
        "requestedRange": requested_range or {},
        "sourceInterval": source_interval,
        "availableFrom": available_from,
        "availableTo": available_to,
        "oldestTimestamp": oldest,
        "newestTimestamp": newest,
        "hasMoreBefore": has_more_before(
            oldest,
            no_data_before,
            requested_range=requested_range,
            available_from=available_from,
        ),
        "hasMoreAfter": bool(newest and available_to and available_to > newest),
        "storedCandleCount": stored_count,
        "invalidRowCount": int(invalid_row_count or 0),
        "noDataBefore": no_data_before,
    })
    return payload


def requested_range_payload(*, before=None, from_time=None, to_time=None):
    payload = {}
    if before:
        payload["before"] = before
    if from_time:
        payload["from"] = from_time
    if to_time:
        payload["to"] = to_time
    return payload


def has_more_before(oldest, no_data_before=None, *, requested_range=None, available_from=None):
    oldest_time = parse_iso_time(oldest)
    if not oldest_time:
        range_probe = requested_range_probe(requested_range)
        available_from_time = parse_iso_time(available_from)
        if not range_probe or not available_from_time or range_probe > available_from_time:
            return False
        boundary = parse_iso_time(no_data_before)
        if boundary and range_probe <= boundary:
            return False
        return True
    boundary = parse_iso_time(no_data_before)
    if boundary and oldest_time <= boundary:
        return False
    return True


def requested_range_probe(requested_range):
    if not requested_range:
        return None
    before = parse_iso_time(requested_range.get("before"))
    if before:
        return before
    to_time = parse_iso_time(requested_range.get("to"))
    if to_time:
        return to_time
    return parse_iso_time(requested_range.get("from"))


def candles_are_behind_coverage(candles, coverage):
    if not candles or not coverage:
        return False
    available_to = parse_iso_time(coverage.get("availableTo"))
    if not available_to:
        return False
    newest = None
    for candle in reversed(candles):
        newest = parse_iso_time(candle.get("timestamp"))
        if newest:
            break
    return bool(newest and available_to > newest)


def parse_iso_time(value):
    return parse_utc_time(value)


def candle_after_cursor(symbol, interval, candle, cursor, cursor_timestamp=None):
    if not cursor:
        return True
    cursor_timestamp = cursor_timestamp if cursor_timestamp is not None else timestamp_from_cursor(cursor)
    candle_timestamp = candle.get("timestamp") or candle.get("eventTime")
    if not candle_timestamp or not cursor_timestamp:
        return True
    candle_timestamp = canonical_utc_timestamp(candle_timestamp) or candle_timestamp
    cursor_timestamp = canonical_utc_timestamp(cursor_timestamp) or cursor_timestamp
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


def moving_average_query_limit(interval, requested_limit):
    lookback = max(MA_WINDOWS)
    return resolve_candle_limit(interval, int(requested_limit) + lookback)
