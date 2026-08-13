import math
from datetime import datetime, timezone

from market_data.common.symbols import is_crypto_symbol
from market_data.serving.intervals import normalize_chart_interval


def invalid_candle_reason(row):
    """적재하면 안 되는 candle이면 사유를 반환하고, 정상 candle이면 None을 반환합니다."""
    try:
        interval = normalize_chart_interval(row.get("interval", "1m"))
    except ValueError as exc:
        return str(exc)

    timestamp = parse_time(row.get("timestamp") or row.get("event_time"))
    if timestamp is None:
        return "Candle timestamp is missing or invalid."
    numeric_reason = invalid_candle_numeric_reason(row)
    if numeric_reason:
        return numeric_reason
    if is_crypto_symbol(row.get("symbol")):
        return None
    if interval not in {"1W", "1M"} and timestamp.weekday() >= 5:
        return f"{interval} stock candle timestamp falls outside weekday market sessions."
    return None


def invalid_candle_numeric_reason(row, *, require=False):
    """Validate finite OHLCV and the candle price envelope."""
    price_keys = ("open", "high", "low", "close")
    numeric_present = any(row.get(key) is not None for key in (*price_keys, "volume"))
    if not numeric_present and not require:
        return None
    missing = [key for key in price_keys if row.get(key) is None]
    if missing:
        return f"Candle OHLC is missing: {', '.join(missing)}."
    try:
        open_price, high, low, close = (float(row[key]) for key in price_keys)
        volume = float(row.get("volume") or 0)
    except (TypeError, ValueError):
        return "Candle OHLCV is not numeric."
    if not all(math.isfinite(value) for value in (open_price, high, low, close, volume)):
        return "Candle OHLCV must be finite."
    if min(open_price, high, low, close) <= 0:
        return "Candle prices must be positive."
    if low > min(open_price, close) or high < max(open_price, close) or high < low:
        return "Candle open/close must stay within low/high."
    if volume < 0:
        return "Candle volume must be non-negative."
    return None


def parse_time(value):
    """검증용 timestamp 값을 UTC datetime으로 파싱하고 실패하면 None을 반환합니다."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
