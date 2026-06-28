from datetime import datetime, timezone

from alfaka.serving.intervals import normalize_chart_interval


def invalid_candle_reason(row):
    try:
        interval = normalize_chart_interval(row.get("interval", "1m"))
    except ValueError as exc:
        return str(exc)

    timestamp = parse_time(row.get("timestamp") or row.get("event_time"))
    if timestamp is None:
        return "Candle timestamp is missing or invalid."
    if interval not in {"1W", "1M"} and timestamp.weekday() >= 5:
        return f"{interval} stock candle timestamp falls outside weekday market sessions."
    return None


def parse_time(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
