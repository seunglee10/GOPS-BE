# 역할: 내부 market payload를 GOPS 차트 런타임이 기대하는 DTO로 바꿉니다.
# 사용: REST CandleSnapshot과 WebSocket CandleEvent 응답에서 공통으로 씁니다.
# 주의: 프론트는 ma5/ma20/ma60 flat field를 기대합니다.
import hashlib
from datetime import datetime, time, timedelta, timezone

from alfaka.alpaca.feed_profiles import market_session_for_timestamp
from alfaka.serving.time_utils import canonical_utc_timestamp, parse_utc_time


def candle_to_gops(candle):
    ma = candle.get("ma") or {}
    timestamp = canonical_timestamp_for_interval(candle.get("interval"), candle.get("timestamp"))
    result = {
        "timestamp": timestamp,
        "open": number_or_zero(candle.get("open")),
        "high": number_or_zero(candle.get("high")),
        "low": number_or_zero(candle.get("low")),
        "close": number_or_zero(candle.get("close")),
        "volume": number_or_zero(candle.get("volume")),
        "isClosed": bool(candle.get("isClosed", candle.get("is_closed", True))),
    }

    for key in ("ma5", "ma20", "ma60"):
        value = candle.get(key, ma.get(key))
        if value is not None:
            result[key] = number_or_zero(value)
    for key in ("sourceInterval", "updatedAt", "feedProfile", "marketSession"):
        value = candle.get(key)
        if value is not None:
            result[key] = value
    if not result.get("marketSession") or result.get("marketSession") == "unknown":
        result["marketSession"] = fallback_market_session(candle, timestamp)
    return result


def snapshot(
    symbol,
    interval,
    candles,
    source="alpaca",
    feed="sip",
    indicators=None,
    data_status=None,
    backfill_status="not_requested",
    can_backfill=False,
    message=None,
):
    last_candle = candles[-1] if candles else {}
    resolved_data_status = data_status or ("ready" if candles else "empty")
    resolved_message = message
    if resolved_message is None and resolved_data_status == "empty":
        resolved_message = "No candle data is available for this symbol and interval."
    converted_candles = [candle_to_gops(candle) for candle in candles]
    payload = {
        "symbol": symbol,
        "interval": interval,
        "source": source,
        "feed": feed,
        "snapshotCursor": cursor_for(symbol, interval, last_candle) if candles else None,
        "dataStatus": resolved_data_status,
        "backfillStatus": backfill_status,
        "canBackfill": bool(can_backfill),
        "message": resolved_message,
        "indicators": indicators or {"ma": [5, 20, 60], "volume": True},
        "candles": converted_candles,
    }
    if last_candle.get("feedProfile"):
        payload["feedProfile"] = last_candle["feedProfile"]
    if last_candle.get("marketSession") and last_candle.get("marketSession") != "unknown":
        payload["marketSession"] = last_candle["marketSession"]
    elif converted_candles:
        payload["marketSession"] = converted_candles[-1].get("marketSession")
    return payload


def websocket_event(event_type, symbol, interval, candle, source=None, feed=None):
    cursor = cursor_for(symbol, interval, candle)
    event = {
        "type": event_type,
        "eventId": f"delta/{event_type}/{symbol}/{interval}/{cursor}",
        "cursor": cursor,
        "symbol": symbol,
        "interval": interval,
        "source": source or candle.get("source") or "alpaca",
        "feed": feed or candle.get("feed") or "sip",
        "data": candle_to_gops(candle),
    }
    if candle.get("feedProfile"):
        event["feedProfile"] = candle["feedProfile"]
    if candle.get("marketSession") and candle.get("marketSession") != "unknown":
        event["marketSession"] = candle["marketSession"]
    elif event["data"].get("marketSession"):
        event["marketSession"] = event["data"]["marketSession"]
    if candle.get("sourceInterval"):
        event["sourceInterval"] = candle["sourceInterval"]
    return event


def market_status_event(status):
    symbol = status.get("symbol") or "_MARKET"
    interval = "status"
    cursor = cursor_for(symbol, interval, status, time_field="eventTime")
    return {
        "type": "MARKET_STATUS_UPDATE",
        "eventId": f"delta/MARKET_STATUS_UPDATE/{symbol}/{interval}/{cursor}",
        "cursor": cursor,
        "symbol": symbol,
        "interval": interval,
        "source": status.get("source", "alpaca"),
        "feed": status.get("feed") or "unknown",
        "data": status,
    }


def volume_profile_event(symbol, profile_bin):
    interval = "1m"
    cursor = cursor_for(symbol, interval, profile_bin, time_field="eventMinute")
    return {
        "type": "VOLUME_PROFILE_BINS_UPDATE",
        "eventId": f"delta/VOLUME_PROFILE_BINS_UPDATE/{symbol}/{interval}/{cursor}",
        "cursor": cursor,
        "symbol": symbol,
        "interval": interval,
        "source": profile_bin.get("source", "alpaca"),
        "feed": profile_bin.get("feed") or "unknown",
        "data": profile_bin,
    }


def fallback_market_session(candle, timestamp):
    interval = str(candle.get("interval") or "")
    if interval in {"1D", "1d", "1W", "1M"}:
        return "regular"
    return market_session_for_timestamp(timestamp)


def cursor_for(symbol, interval, payload, time_field="timestamp"):
    if not payload:
        return f"v1:{symbol}:{interval}:empty:00000000"
    raw_event_time = payload.get(time_field) or payload.get("timestamp") or payload.get("eventTime") or payload.get("updatedAt") or payload.get("createdAt") or "unknown"
    event_time = canonical_timestamp_for_interval(interval, raw_event_time) or raw_event_time
    source_event_id = payload.get("sourceEventId") or f"{symbol}/{interval}/{event_time}"
    digest = hashlib.sha1(str(source_event_id).encode("utf-8")).hexdigest()[:10]
    return f"v1:{symbol}:{interval}:{event_time}:{digest}"


def number_or_zero(value):
    return float(value or 0)


def canonical_timestamp_for_interval(interval, value):
    parsed = parse_utc_time(value)
    if not parsed:
        return canonical_utc_timestamp(value)
    interval = str(interval or "")
    if interval in {"1D", "1d"}:
        return utc_bucket_iso(datetime.combine(parsed.date(), time(0, 0), timezone.utc))
    if interval == "1W":
        bucket = parsed.date() - timedelta(days=parsed.weekday())
        return utc_bucket_iso(datetime.combine(bucket, time(0, 0), timezone.utc))
    if interval == "1M":
        bucket = parsed.date().replace(day=1)
        return utc_bucket_iso(datetime.combine(bucket, time(0, 0), timezone.utc))
    return canonical_utc_timestamp(value)


def utc_bucket_iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
