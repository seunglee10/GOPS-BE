from alfaka.storage.candle_validation import invalid_candle_reason
from alfaka.storage.clickhouse_loader import candle_market_session, candle_to_clickhouse_row, canonical_candle_timestamp


def materialize_processed_rows(client, source_path, rows, source_name="backfill-alpaca"):
    prepared = prepare_processed_candle_rows(rows)
    return materialize_prepared_processed_rows(
        client,
        source_path,
        prepared["rows"],
        source_name=source_name,
        skipped_invalid=prepared["skippedInvalidRowCount"],
    )


def prepare_processed_candle_rows(rows):
    normalized = []
    skipped_invalid = 0
    for row in rows:
        if (row.get("eventType") or "CANDLE") != "CANDLE":
            continue
        candle = normalize_processed_candle_row(row)
        if invalid_candle_reason(candle):
            skipped_invalid += 1
            continue
        normalized.append(candle)

    return {"rows": dedupe_candles(normalized), "skippedInvalidRowCount": skipped_invalid}


def materialize_prepared_processed_rows(client, source_path, rows, source_name="backfill-alpaca", skipped_invalid=0):
    clickhouse_rows = [candle_to_clickhouse_row(row) for row in rows]
    for _partition, partition_rows in partition_rows_by_month(clickhouse_rows):
        client.insert_json_each_row("chart_candles", partition_rows)

    client.insert_json_each_row("load_audit", [{
        "source_name": source_name,
        "object_path": source_path,
        "row_count": len(clickhouse_rows),
        "note": f"Chart candle materialization; skipped_invalid={skipped_invalid}",
    }])
    return {"objectPath": source_path, "rowCount": len(clickhouse_rows), "skippedInvalidRowCount": skipped_invalid}


def partition_rows_by_month(rows):
    partitions = {}
    for row in rows:
        partition = month_partition(row.get("event_time"))
        partitions.setdefault(partition, []).append(row)
    return [
        (partition, sorted(partition_rows, key=lambda row: (row.get("event_time") or "", row.get("symbol") or "", row.get("interval") or "")))
        for partition, partition_rows in sorted(partitions.items())
    ]


def month_partition(event_time):
    if event_time is None:
        return "unknown"
    value = str(event_time).strip()
    if len(value) < 7:
        return value or "unknown"
    return value[:7]


def normalize_processed_candle_row(row):
    required = ["symbol", "interval", "timestamp", "open", "high", "low", "close", "volume"]
    missing = [key for key in required if row.get(key) is None]
    if missing:
        raise ValueError(f"Processed candle row is missing required fields: {', '.join(missing)}")

    ma = dict(row.get("ma") or {})
    for key in ("ma5", "ma20", "ma60"):
        if row.get(key) is not None:
            ma[key] = row.get(key)

    normalized = {
        "eventType": "CANDLE",
        "symbol": row["symbol"],
        "interval": row["interval"],
        "timestamp": canonical_candle_timestamp(row),
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "close": row["close"],
        "volume": row["volume"],
        "tradeCount": row.get("tradeCount"),
        "vwap": row.get("vwap"),
        "ma": ma,
        "isClosed": row.get("isClosed", row.get("is_closed", True)),
        "correctionType": row.get("correctionType", row.get("correction_type", "NONE")),
        "source": row.get("source", "backfill"),
        "feed": row.get("feed") or "unknown",
        "feedProfile": row.get("feedProfile") or row.get("feed_profile") or row.get("feed") or "unknown",
        "sourceEventId": row.get("sourceEventId") or row.get("source_event_id"),
        "createdAt": row.get("createdAt") or row.get("created_at") or row.get("updatedAt"),
    }
    normalized["marketSession"] = row.get("marketSession") or row.get("market_session") or candle_market_session(normalized, normalized["timestamp"])
    return normalized


def dedupe_candles(rows):
    by_key = {}
    for row in rows:
        by_key[(row["symbol"], row["interval"], row["timestamp"])] = row
    return [by_key[key] for key in sorted(by_key)]
