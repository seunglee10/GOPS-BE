# 역할: GOPS Chart API가 Redis와 ClickHouse를 함께 읽을 수 있게 묶는 provider입니다.
# 사용: 먼저 Redis 최근 캔들을 보고, 부족하면 ClickHouse 과거 캔들을 조회합니다.
# 결과: GOPS CandleSnapshot 형식으로 반환합니다.
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from alfaka.alpaca.feed_profiles import active_extended_session_window, market_session_for_timestamp
from alfaka.serving.closed_watermark import live_candle_after_latest_closed
from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider
from alfaka.serving.cursors import timestamp_from_cursor
from alfaka.serving.dto import cursor_for, snapshot
from alfaka.serving.intervals import (
    INTRADAY_DERIVED_INTERVALS,
    INTRADAY_INTERVAL_MINUTES,
    normalize_chart_interval,
    resolve_candle_limit,
    source_interval_for,
)
from alfaka.serving.moving_average import MA_WINDOWS, attach_moving_averages
from alfaka.serving.redis_provider import RedisMarketDataProvider
from alfaka.serving.symbol_registry import SymbolRegistry
from alfaka.serving.time_utils import canonical_utc_timestamp, parse_utc_time
from alfaka.serving.volume_profile import compute_volume_profile_payload


logger = logging.getLogger(__name__)
TARGET_FLOOR_TOLERANCE = timedelta(days=3)
MARKET_TIMEZONE = ZoneInfo("America/New_York")


class MarketDataProvider:
    def __init__(self, redis_provider=None, clickhouse_provider=None):
        self.redis_provider = redis_provider or RedisMarketDataProvider()
        self.clickhouse_provider = clickhouse_provider or ClickHouseMarketDataProvider()
        self.symbol_registry = SymbolRegistry(self.clickhouse_provider, self.redis_provider)

    def candle_snapshot(self, symbol, interval, limit=None, before=None, from_time=None, to_time=None, ma_windows=MA_WINDOWS):
        interval = normalize_chart_interval(interval)
        limit = resolve_candle_limit(interval, limit)
        query_limit = moving_average_query_limit(interval, limit, ma_windows)
        if from_time and to_time:
            clickhouse_from_time = moving_average_query_from_time(interval, from_time, ma_windows)
        elif before and not from_time:
            clickhouse_from_time = None
        else:
            clickhouse_from_time = target_floor_from_time(interval, from_time, limit)
            if from_time and not before:
                clickhouse_from_time = moving_average_query_from_time(interval, clickhouse_from_time, ma_windows)
        range_query = bool(before or from_time or to_time)
        redis_candles = filter_stock_chart_candles(self.redis_provider.recent_candles(symbol, interval, query_limit))
        live_candle = self._live_candle(symbol, interval)
        closed_watermark = self._closed_watermark(symbol, interval)
        if range_query:
            redis_candles = filter_candles_for_requested_window(
                redis_candles,
                before=before,
                from_time=from_time,
                to_time=to_time,
            )
            live_candle = live_candle if candle_in_requested_window(
                live_candle,
                before=before,
                from_time=from_time,
                to_time=to_time,
            ) else None
        coverage = None
        if len(redis_candles) >= query_limit and redis_recent_window_is_current(redis_candles, clickhouse_from_time, range_query):
            live_candle = live_candle_after_latest_closed(live_candle, redis_candles, watermark=closed_watermark)
            merged_redis = merge_candles(redis_candles, [live_candle] if live_candle else [])
            payload = snapshot(symbol=symbol, interval=interval, candles=attach_moving_averages(merged_redis, windows=ma_windows, overwrite=True)[-limit:])
            payload["_sourceTrace"] = {
                "redis": {"checked": True, "hit": len(merged_redis) > 0, "rowCount": len(merged_redis)},
                "clickhouse": {"checked": False, "hit": False, "rowCount": 0},
            }
            return with_coverage_metadata(
                payload,
                coverage_from_loaded_candles(payload.get("candles") or []),
                limit,
                before=before,
                from_time=from_time,
                to_time=to_time,
            )

        clickhouse_candles = filter_stock_chart_candles(self.clickhouse_provider.candles(
            symbol,
            interval,
            query_limit,
            before=before,
            from_time=clickhouse_from_time,
            to_time=to_time,
        ))
        if interval in {"1m", *INTRADAY_DERIVED_INTERVALS} and not range_query and len(clickhouse_candles) < query_limit and clickhouse_from_time and not clickhouse_candles:
            latest_candles = filter_stock_chart_candles(self.clickhouse_provider.candles(
                symbol,
                interval,
                query_limit,
            ))
            if len(latest_candles) > len(clickhouse_candles):
                clickhouse_candles = latest_candles
        live_candle = live_candle_after_latest_closed(live_candle, clickhouse_candles, redis_candles, watermark=closed_watermark)
        live_group = [live_candle] if live_candle else []
        merged = merge_candles(clickhouse_candles, redis_candles, live_group)
        computed = attach_moving_averages(merged, windows=ma_windows, overwrite=True)
        if range_query:
            computed = filter_candles_for_requested_window(
                computed,
                before=before,
                from_time=from_time,
                to_time=to_time,
            )
        candles = computed[-limit:]
        feed = first_value(candles, "feed", "sip")
        source = first_value(candles, "source", "alpaca")
        payload = snapshot(symbol=symbol, interval=interval, candles=candles, source=source, feed=feed)
        payload["_sourceTrace"] = {
            "redis": {"checked": True, "hit": len(redis_candles) > 0 or live_candle is not None, "rowCount": len(redis_candles) + len(live_group)},
            "clickhouse": {"checked": True, "hit": len(clickhouse_candles) > 0, "rowCount": len(clickhouse_candles)},
        }
        return with_coverage_metadata(
            payload,
            coverage or coverage_from_loaded_candles(candles),
            limit,
            before=before,
            from_time=from_time,
            to_time=to_time,
        )

    def candles_since_cursor(self, symbol, interval, cursor, limit=500):
        interval = normalize_chart_interval(interval)
        timestamp = timestamp_from_cursor(cursor)
        redis_candles = [
            candle for candle in self.redis_provider.recent_candles(symbol, interval, limit)
            if candle_after_cursor(symbol, interval, candle, cursor, timestamp)
        ]
        redis_candles = filter_stock_chart_candles(redis_candles)
        live_candle = self._live_candle(symbol, interval)
        closed_watermark = self._closed_watermark(symbol, interval)
        try:
            clickhouse_candles = self._clickhouse_candles_since_cursor(symbol, interval, timestamp, limit)
        except Exception:
            logger.warning("ClickHouse candles_since failed; falling back to Redis recent candles.", exc_info=True)
            clickhouse_candles = []
        filtered_clickhouse = filter_stock_chart_candles([
            candle for candle in clickhouse_candles
            if candle_after_cursor(symbol, interval, candle, cursor, timestamp)
        ])
        live_candle = live_candle_after_latest_closed(live_candle, filtered_clickhouse, redis_candles, watermark=closed_watermark)
        live_candles = [live_candle] if live_candle and candle_after_cursor(symbol, interval, live_candle, cursor, timestamp) else []
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

    def _closed_watermark(self, symbol, interval):
        method = getattr(self.redis_provider, "closed_candle_watermark", None)
        if not callable(method):
            return None
        try:
            return method(symbol, interval)
        except Exception:
            logger.warning("Redis closed candle watermark lookup failed for %s %s.", symbol, interval, exc_info=True)
            return None

    def search_symbols(self, query, limit=20):
        return self.symbol_registry.search(query, limit)

    def symbol_detail(self, symbol):
        return self.symbol_registry.detail(symbol)

    def latest_status(self, symbol=None):
        return self.redis_provider.latest_status(symbol) or self.clickhouse_provider.latest_status(symbol)

    def volume_profile_bins(self, symbol, from_time, to_time, price_bin_size=None, interval="1m"):
        interval = normalize_chart_interval(interval)
        candles = self.candle_snapshot(symbol, interval, resolve_candle_limit(interval), from_time=from_time, to_time=to_time, ma_windows=())
        return compute_volume_profile_payload(
            candles,
            symbol=symbol,
            interval=interval,
            from_time=from_time,
            to_time=to_time,
        )

    def footprint_ticks(self, symbol, from_time, to_time, limit=20000):
        try:
            return self.clickhouse_provider.footprint_ticks(symbol, from_time, to_time, limit=limit)
        except Exception:
            logger.warning("ClickHouse footprint_ticks failed.", exc_info=True)
            return {
                "symbol": symbol,
                "from": from_time,
                "to": to_time,
                "source": "clickhouse",
                "trades": [],
                "quotes": [],
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
            "volumeProfile": self.volume_profile_bins(symbol, from_time, to_time, "auto", interval=interval) if "volumeProfile" in include else None,
            "comparisonCandidates": self.search_symbols(symbol[:2], 5),
        }


def merge_candles(*groups):
    by_timestamp = {}
    for group in groups:
        for candle in group:
            if not candle:
                continue
            timestamp = merge_timestamp_key(candle)
            if timestamp:
                by_timestamp[timestamp] = {**candle, "timestamp": timestamp}
    return [by_timestamp[key] for key in sorted(by_timestamp)]


def merge_timestamp_key(candle):
    timestamp = canonical_utc_timestamp(candle.get("timestamp"))
    if not timestamp:
        return None
    try:
        interval = normalize_chart_interval(candle.get("interval", "1m"))
    except ValueError:
        interval = "1m"
    if interval != "1D":
        return timestamp
    parsed = parse_iso_time(timestamp)
    if not parsed:
        return timestamp
    if parsed.hour == 0 and parsed.minute == 0 and parsed.second == 0 and parsed.microsecond == 0:
        market_day = datetime(parsed.year, parsed.month, parsed.day, tzinfo=MARKET_TIMEZONE)
    else:
        market_day = parsed.astimezone(MARKET_TIMEZONE).replace(hour=0, minute=0, second=0, microsecond=0)
    return market_day.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def filter_candles_for_requested_window(candles, before=None, from_time=None, to_time=None):
    return [
        candle for candle in candles
        if candle_in_requested_window(candle, before=before, from_time=from_time, to_time=to_time)
    ]


def candle_in_requested_window(candle, before=None, from_time=None, to_time=None):
    if not candle:
        return False
    timestamp = parse_iso_time(merge_timestamp_key(candle))
    if not timestamp:
        return False
    before_time = parse_iso_time(before)
    start_time = parse_iso_time(from_time)
    end_time = parse_iso_time(to_time)
    if before_time and timestamp >= before_time:
        return False
    if start_time and timestamp < start_time:
        return False
    if end_time and timestamp > end_time:
        return False
    return True


def with_coverage_metadata(payload, coverage, requested_limit, before=None, from_time=None, to_time=None):
    interval = normalize_chart_interval(payload.get("interval"))
    source_interval = normalize_chart_interval((coverage or {}).get("sourceInterval") or source_interval_for(interval))
    candles = payload.get("candles") or []
    oldest = candles[0].get("timestamp") if candles else None
    newest = candles[-1].get("timestamp") if candles else None
    available_from = coverage.get("availableFrom") if coverage else None
    available_to = coverage.get("availableTo") if coverage else None
    row_count = coverage.get("rowCount") if coverage else None
    invalid_row_count = coverage.get("invalidRowCount") if coverage else None
    stored_count = int(row_count) if row_count is not None else len(candles)
    target_stored_count = requested_source_bar_target(interval, requested_limit, source_interval=source_interval)
    target_range_from, target_range_to = requested_window_for_interval(
        interval,
        requested_limit,
        before=before,
        from_time=from_time,
        to_time=to_time,
    )
    missing_ranges = missing_ranges_for_requested_window(
        target_range_from=target_range_from,
        target_range_to=target_range_to,
        available_from=available_from,
        available_to=available_to,
    )
    payload.update({
        "requestedLimit": requested_limit,
        "returnedCount": len(candles),
        "targetStoredCount": target_stored_count,
        "targetRangeFrom": target_range_from,
        "targetRangeTo": target_range_to,
        "sourceInterval": source_interval,
        "availableFrom": available_from,
        "availableTo": available_to,
        "oldestTimestamp": oldest,
        "newestTimestamp": newest,
        "hasMoreBefore": has_more_before_target(oldest, available_from, target_range_from),
        "hasMoreAfter": bool(newest and available_to and available_to > newest),
        "storedCandleCount": stored_count,
        "invalidRowCount": int(invalid_row_count or 0),
        "missingRanges": missing_ranges,
    })
    return payload


def coverage_from_loaded_candles(candles):
    if not candles:
        return {"rowCount": 0, "availableFrom": None, "availableTo": None}
    return {
        "rowCount": len(candles),
        "availableFrom": candles[0].get("timestamp"),
        "availableTo": candles[-1].get("timestamp"),
    }


def redis_recent_window_is_current(candles, target_from_time, range_query):
    if range_query or not target_from_time:
        return True
    if not candles:
        return False
    newest = parse_iso_time(candles[-1].get("timestamp"))
    target = parse_iso_time(target_from_time)
    if not newest or not target:
        return True
    return newest >= target


def has_more_before_target(oldest, available_from, target_range_from):
    oldest_time = parse_iso_time(oldest)
    if not oldest_time:
        return False
    target_start = parse_iso_time(target_range_from)
    available_start = parse_iso_time(available_from)
    if available_start and oldest_time - available_start > TARGET_FLOOR_TOLERANCE:
        return True
    if target_start and oldest_time - target_start > TARGET_FLOOR_TOLERANCE:
        return True
    return False


def one_year_target_from(reference_timestamp=None):
    return target_range_from_for_interval("1m", reference_timestamp)


def target_floor_from_time(interval, from_time=None, requested_limit=None):
    target_floor = target_range_from_for_interval(interval, requested_limit=requested_limit)
    if not from_time:
        return target_floor
    requested = parse_iso_time(from_time)
    floor = parse_iso_time(target_floor)
    if requested and floor and requested > floor:
        return from_time
    return target_floor


def target_range_from_for_interval(interval, reference_timestamp=None, requested_limit=None):
    reference = parse_iso_time(reference_timestamp) or datetime.now(timezone.utc)
    target = reference - requested_window_delta(interval, requested_limit or resolve_candle_limit(interval, None))
    return target.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def requested_window_for_interval(interval, requested_limit, before=None, from_time=None, to_time=None):
    interval = normalize_chart_interval(interval)
    explicit_start = parse_iso_time(from_time)
    explicit_end = parse_iso_time(to_time)
    cursor_end = parse_iso_time(before)
    end = explicit_end or cursor_end or datetime.now(timezone.utc)
    start = explicit_start or (end - requested_window_delta(interval, requested_limit))
    return (
        start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    )


def requested_window_delta(interval, requested_limit):
    interval = normalize_chart_interval(interval)
    limit = max(1, int(requested_limit or 1))
    if interval in INTRADAY_INTERVAL_MINUTES:
        return timedelta(minutes=limit * INTRADAY_INTERVAL_MINUTES[interval] * 4)
    if interval == "1D":
        return timedelta(days=limit * 2)
    if interval == "1W":
        return timedelta(days=limit * 8)
    if interval == "1M":
        return timedelta(days=limit * 32)
    return timedelta(minutes=limit * 4)


def requested_source_bar_target(interval, requested_limit, source_interval=None):
    interval = normalize_chart_interval(interval)
    limit = max(1, int(requested_limit or 1))
    source_interval = normalize_chart_interval(source_interval or source_interval_for(interval))
    if source_interval == interval:
        return limit
    if interval in INTRADAY_DERIVED_INTERVALS:
        return limit * INTRADAY_INTERVAL_MINUTES[interval]
    if interval == "1W":
        return limit * 5
    if interval == "1M":
        return limit * 22
    return limit


def missing_ranges_for_requested_window(*, target_range_from, target_range_to, available_from, available_to):
    target_start = parse_iso_time(target_range_from)
    target_end = parse_iso_time(target_range_to)
    if not target_start or not target_end:
        return []
    available_start = parse_iso_time(available_from)
    available_end = parse_iso_time(available_to)
    if not available_start or not available_end:
        return [{"start": target_range_from, "end": target_range_to}]

    ranges = []
    if available_start - target_start > TARGET_FLOOR_TOLERANCE:
        ranges.append({
            "start": target_start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "end": min(available_start, target_end).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        })
    if target_end - available_end > TARGET_FLOOR_TOLERANCE:
        ranges.append({
            "start": max(available_end, target_start).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "end": target_end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        })
    return [item for item in ranges if item["start"] < item["end"]]


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
    return filter_stock_chart_candles(candles)


def is_stock_weekday_candle(candle):
    return is_stock_chart_visible_candle(candle)


def filter_stock_chart_candles(candles, now=None):
    return [candle for candle in candles if is_stock_chart_visible_candle(candle, now=now)]


def is_stock_chart_visible_candle(candle, now=None):
    try:
        interval = normalize_chart_interval(candle.get("interval", "1m"))
    except ValueError:
        interval = "1m"
    if interval in {"1D", "1W", "1M"}:
        return True
    timestamp = candle.get("timestamp") or candle.get("eventTime")
    parsed = parse_iso_time(timestamp)
    if not parsed:
        return True
    session = normalized_market_session(candle.get("marketSession"))
    if not session:
        return market_session_for_timestamp(timestamp) != "closed"
    if session == "closed":
        return False
    if session == "regular":
        return True
    window = active_extended_session_window(now)
    if not window:
        return False
    active_session, start, end = window
    return session == active_session and start <= parsed < end


def normalized_market_session(value):
    normalized = str(value or "").strip().lower()
    if normalized in {"pre", "regular", "after", "overnight", "closed"}:
        return normalized
    return None


def moving_average_query_limit(interval, requested_limit, windows=MA_WINDOWS):
    if not windows:
        return resolve_candle_limit(interval, requested_limit)
    lookback = max(windows)
    return resolve_candle_limit(interval, int(requested_limit) + lookback)


def moving_average_query_from_time(interval, from_time, windows=MA_WINDOWS):
    if not windows:
        return from_time
    parsed = parse_iso_time(from_time)
    if not parsed:
        return from_time
    lookback = max(windows)
    start = parsed - requested_window_delta(interval, lookback)
    return start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
