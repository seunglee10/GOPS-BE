# 역할: 내부 market payload를 GOPS 차트 런타임이 기대하는 DTO로 바꿉니다.
# 사용: REST CandleSnapshot과 WebSocket CandleEvent 응답에서 공통으로 씁니다.
# 주의: 프론트는 ma5/ma20/ma60 flat field를 기대합니다.


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


def snapshot(symbol, interval, candles, source="alpaca", feed="sip", indicators=None):
    return {
        "symbol": symbol,
        "interval": interval,
        "source": source,
        "feed": feed,
        "isSynthetic": False,
        "indicators": indicators or {"ma": [5, 20, 60], "volume": True},
        "candles": [candle_to_gops(candle) for candle in candles],
    }


def websocket_event(event_type, symbol, interval, candle, source="alpaca", feed="sip"):
    return {
        "type": event_type,
        "symbol": symbol,
        "interval": interval,
        "source": source,
        "feed": feed,
        "isSynthetic": False,
        "data": candle_to_gops(candle),
    }


def number_or_zero(value):
    return float(value or 0)
