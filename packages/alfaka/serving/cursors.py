def timestamp_from_cursor(cursor):
    parts = cursor_parts(cursor)
    if not parts:
        return None
    timestamp = parts["timestamp"]
    if timestamp in {"empty", "unknown"}:
        return None
    return timestamp


def cursor_parts(cursor):
    if not cursor:
        return None
    try:
        version, symbol, interval, remainder = cursor.split(":", 3)
        timestamp, marker = remainder.rsplit(":", 1)
        return {
            "version": version,
            "symbol": symbol,
            "interval": interval,
            "timestamp": timestamp,
            "marker": marker,
        }
    except (AttributeError, IndexError, ValueError):
        return None
