# 역할: 내부 market payload를 GOPS 차트 런타임이 기대하는 DTO로 바꿉니다.
# 사용: REST CandleSnapshot과 WebSocket CandleEvent 응답에서 공통으로 씁니다.
# 주의: 프론트는 ma5/ma20/ma60 flat field를 기대합니다.
import hashlib


def candle_to_gops(candle):
    ma = candle.get("ma") or {}
    result = {
        "timestamp": candle.get("timestamp"),
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
    return {
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
        "candles": [candle_to_gops(candle) for candle in candles],
    }


def websocket_event(event_type, symbol, interval, candle, source="alpaca", feed="sip"):
    cursor = cursor_for(symbol, interval, candle)
    return {
        "type": event_type,
        "eventId": f"delta/{event_type}/{symbol}/{interval}/{cursor}",
        "cursor": cursor,
        "symbol": symbol,
        "interval": interval,
        "source": source,
        "feed": feed,
        "data": candle_to_gops(candle),
    }


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


def cursor_for(symbol, interval, payload, time_field="timestamp"):
    if not payload:
        return f"v1:{symbol}:{interval}:empty:00000000"
    event_time = payload.get(time_field) or payload.get("timestamp") or payload.get("eventTime") or payload.get("updatedAt") or payload.get("createdAt") or "unknown"
    source_event_id = payload.get("sourceEventId") or f"{symbol}/{interval}/{event_time}"
    digest = hashlib.sha1(str(source_event_id).encode("utf-8")).hexdigest()[:10]
    return f"v1:{symbol}:{interval}:{event_time}:{digest}"


def number_or_zero(value):
    return float(value or 0)
