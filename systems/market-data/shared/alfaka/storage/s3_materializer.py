import io
import json
import os

from alfaka.alpaca.feed_profiles import market_session_for_timestamp
from alfaka.common.env import load_dotenv
from alfaka.storage.clickhouse_loader import ClickHouseHttpClient, candle_to_clickhouse_row
from alfaka.storage.candle_validation import invalid_candle_reason


def main():
    load_dotenv()
    bucket = os.getenv("S3_BUCKET")
    prefix = os.getenv("S3_MATERIALIZE_PREFIX") or os.getenv("S3_FINAL_PREFIX", "market-data/final")
    if not bucket:
        raise SystemExit("S3_BUCKET is required for S3 materialization.")
    from alfaka.common.s3_client import create_s3_client

    s3 = create_s3_client()
    client = ClickHouseHttpClient(
        url=os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
        database=os.getenv("CLICKHOUSE_DATABASE", "market_data"),
        user=os.getenv("CLICKHOUSE_USER", "alfaka"),
        password=os.getenv("CLICKHOUSE_PASSWORD", "alfaka"),
    )
    client.ensure_market_data_schema()
    keys = materialize_keys_from_env(s3, bucket, prefix)
    result = materialize_s3_processed_objects(client, s3, bucket, keys)
    print(json.dumps(result, ensure_ascii=False), flush=True)


def materialize_keys_from_env(s3, bucket, prefix):
    explicit_keys = parse_csv(os.getenv("S3_MATERIALIZE_KEYS"))
    if explicit_keys:
        return explicit_keys
    keys = list_s3_objects(s3, bucket, prefix)
    max_objects = os.getenv("S3_MATERIALIZE_MAX_OBJECTS")
    if max_objects not in {None, ""}:
        keys = keys[: int(max_objects)]
    return keys


def parse_csv(value):
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def list_s3_objects(s3, bucket, prefix):
    keys = []
    if hasattr(s3, "get_paginator"):
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            keys.extend(item["Key"] for item in page.get("Contents", []) if item.get("Key"))
        return keys

    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = s3.list_objects_v2(**kwargs)
        keys.extend(item["Key"] for item in page.get("Contents", []) if item.get("Key"))
        if not page.get("IsTruncated"):
            return keys
        token = page.get("NextContinuationToken")


def detect_s3_object_format(key, content_type=None):
    lowered_key = key.lower()
    lowered_content_type = (content_type or "").lower()
    if lowered_key.endswith(".parquet") or "parquet" in lowered_content_type:
        return "parquet"
    if lowered_key.endswith(".jsonl") or lowered_key.endswith(".ndjson") or "ndjson" in lowered_content_type or "json" in lowered_content_type:
        return "jsonl"
    raise ValueError(f"Unsupported S3 object format for {key}")


def read_s3_rows(s3, bucket, key):
    response = s3.get_object(Bucket=bucket, Key=key)
    body = response["Body"].read()
    object_format = detect_s3_object_format(key, response.get("ContentType"))
    if object_format == "jsonl":
        return [json.loads(line) for line in body.decode("utf-8").splitlines() if line.strip()]
    if object_format == "parquet":
        return read_parquet_rows(body)
    raise ValueError(f"Unsupported S3 object format: {object_format}")


def read_parquet_rows(body):
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("Reading parquet S3 artifacts requires pyarrow.") from exc
    return pq.read_table(io.BytesIO(body)).to_pylist()


def materialize_s3_processed_objects(client, s3, bucket, keys, source_name="s3-processed-final"):
    results = []
    for key in keys:
        rows = read_s3_rows(s3, bucket, key)
        results.append(materialize_processed_rows(client, f"s3://{bucket}/{key}", rows, source_name=source_name))
    return {"objects": results, "rowCount": sum(item["rowCount"] for item in results)}


def materialize_processed_rows(client, object_path, rows, source_name="s3-processed-final"):
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

    deduped = dedupe_candles(normalized)
    clickhouse_rows = [candle_to_clickhouse_row(row) for row in deduped]
    if clickhouse_rows:
        client.insert_json_each_row("chart_candles", clickhouse_rows)

    client.insert_json_each_row("load_audit", [{
        "source_name": source_name,
        "object_path": object_path,
        "row_count": len(clickhouse_rows),
        "note": f"S3 processed/final chart candle materialization; skipped_invalid={skipped_invalid}",
    }])
    return {"objectPath": object_path, "rowCount": len(clickhouse_rows), "skippedInvalidRowCount": skipped_invalid}


def normalize_processed_candle_row(row):
    required = ["symbol", "interval", "timestamp", "open", "high", "low", "close", "volume"]
    missing = [key for key in required if row.get(key) is None]
    if missing:
        raise ValueError(f"Processed candle row is missing required fields: {', '.join(missing)}")

    ma = dict(row.get("ma") or {})
    for key in ("ma5", "ma20", "ma60"):
        if row.get(key) is not None:
            ma[key] = row.get(key)

    return {
        "eventType": "CANDLE",
        "symbol": row["symbol"],
        "interval": row["interval"],
        "timestamp": row["timestamp"],
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
        "marketSession": row.get("marketSession") or row.get("market_session") or market_session_for_timestamp(row.get("timestamp")),
        "sourceEventId": row.get("sourceEventId") or row.get("source_event_id"),
        "createdAt": row.get("createdAt") or row.get("created_at") or row.get("updatedAt"),
    }


def dedupe_candles(rows):
    by_key = {}
    for row in rows:
        by_key[(row["symbol"], row["interval"], row["timestamp"])] = row
    return [by_key[key] for key in sorted(by_key)]


if __name__ == "__main__":
    main()
