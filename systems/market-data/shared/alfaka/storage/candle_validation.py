from datetime import datetime, timezone

from alfaka.common.symbols import is_crypto_symbol
from alfaka.serving.intervals import normalize_chart_interval


def invalid_candle_reason(row):
    """적재하면 안 되는 candle이면 사유를 반환하고, 정상 candle이면 None을 반환합니다."""
    try:
        interval = normalize_chart_interval(row.get("interval", "1m"))
    except ValueError as exc:
        return str(exc)

    timestamp = parse_time(row.get("timestamp") or row.get("event_time"))
    if timestamp is None:
        return "Candle timestamp is missing or invalid."
    if is_crypto_symbol(row.get("symbol")):
        return None
    if interval not in {"1W", "1M"} and timestamp.weekday() >= 5:
        return f"{interval} stock candle timestamp falls outside weekday market sessions."
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
