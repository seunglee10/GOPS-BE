MA_WINDOWS = (5, 20, 60)


def attach_moving_averages(candles, windows=MA_WINDOWS, overwrite=False):
    closes = []
    result = []
    for candle in candles:
        next_candle = dict(candle)
        if isinstance(next_candle.get("ma"), dict):
            next_candle["ma"] = dict(next_candle["ma"])
        close = number_or_none(next_candle.get("close"))
        if close is not None:
            closes.append(close)
        for window in windows:
            key = f"ma{window}"
            nested = next_candle.get("ma") if isinstance(next_candle.get("ma"), dict) else {}
            if overwrite:
                next_candle.pop(key, None)
                nested.pop(key, None)
            else:
                if next_candle.get(key) is not None:
                    continue
                if nested.get(key) is not None:
                    continue
            if len(closes) >= window:
                next_candle[key] = sum(closes[-window:]) / window
        result.append(next_candle)
    return result


def number_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
