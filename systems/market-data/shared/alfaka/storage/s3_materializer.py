import io
import json
import os
from datetime import datetime, timezone

from alfaka.alpaca.feed_profiles import market_session_for_timestamp
from alfaka.common.canonical import candle_metadata, is_historical_canonical
from alfaka.common.env import load_dotenv
from alfaka.serving.intervals import intraday_preload_min_start_iso, normalize_chart_interval
from alfaka.storage.clickhouse_loader import ClickHouseHttpClient, candle_to_clickhouse_row, should_ensure_schema_on_start
from alfaka.storage.candle_validation import invalid_candle_reason


def main():
    load_dotenv()
    bucket = os.getenv("S3_BUCKET")
    prefix = os.getenv("S3_MATERIALIZE_PREFIX") or os.getenv("S3_FINAL_PREFIX", "market-data/rebuild-20260702-lazy-v1/final")
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
    if should_ensure_schema_on_start():
        client.ensure_market_data_schema()
    keys = materialize_keys_from_env(s3, bucket, prefix)
    result = materialize_s3_processed_objects(client, s3, bucket, keys)
    print(json.dumps(result, ensure_ascii=False), flush=True)


def materialize_keys_from_env(s3, bucket, prefix):
    explicit_keys = parse_csv(os.getenv("S3_MATERIALIZE_KEYS"))
    if explicit_keys:
        return explicit_keys
    if materialize_range_env_present():
        return materialize_manifest_keys_from_env(s3, bucket)
    keys = list_s3_objects(s3, bucket, prefix)
    max_objects = os.getenv("S3_MATERIALIZE_MAX_OBJECTS")
    if max_objects not in {None, ""}:
        keys = keys[: int(max_objects)]
    return keys


def materialize_range_env_present():
    return any(
        (os.getenv(name) or "").strip()
        for name in (
            "S3_MATERIALIZE_SYMBOL",
            "S3_MATERIALIZE_INTERVAL",
            "S3_MATERIALIZE_START",
            "S3_MATERIALIZE_END",
        )
    )


def materialize_manifest_keys_from_env(s3, bucket):
    symbol = (os.getenv("S3_MATERIALIZE_SYMBOL") or "").strip().upper()
    interval = (os.getenv("S3_MATERIALIZE_INTERVAL") or "").strip()
    start = (os.getenv("S3_MATERIALIZE_START") or "").strip()
    end = (os.getenv("S3_MATERIALIZE_END") or "").strip()
    values = [symbol, interval, start, end]
    if not any(values):
        return []
    if not all(values):
        raise ValueError("S3_MATERIALIZE_SYMBOL, S3_MATERIALIZE_INTERVAL, S3_MATERIALIZE_START, and S3_MATERIALIZE_END must be set together.")

    interval = normalize_chart_interval(interval)
    if interval not in {"1m", "1D"}:
        raise ValueError(f"S3 materialization supports canonical source intervals only: {interval}")
    validate_materialize_range(interval, start)

    from alfaka.storage.s3_manifest import DEFAULT_MANIFEST_PREFIX, processed_candle_keys_from_manifest

    manifest_prefix = os.getenv("S3_MATERIALIZE_MANIFEST_PREFIX") or os.getenv("S3_MANIFEST_PREFIX", DEFAULT_MANIFEST_PREFIX)
    return processed_candle_keys_from_manifest(s3, bucket, manifest_prefix, symbol, interval, start, end)


def validate_materialize_range(interval, start):
    if normalize_chart_interval(interval) != "1m":
        return
    requested = parse_timestamp(start)
    minimum = parse_timestamp(intraday_preload_min_start_iso())
    if requested and minimum and requested < minimum:
        raise ValueError("1m S3 materialization start is before BACKFILL_INITIAL_LOAD_1M_MIN_START.")


def parse_timestamp(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


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
        object_path = f"s3://{bucket}/{key}"
        if s3_object_already_materialized(client, object_path):
            results.append({"objectPath": object_path, "rowCount": 0, "skippedAlreadyMaterialized": True})
            continue
        rows = read_s3_rows(s3, bucket, key)
        results.append(materialize_processed_rows(client, object_path, rows, source_name=source_name))
    return {"objects": results, "rowCount": sum(item["rowCount"] for item in results)}


def s3_object_already_materialized(client, object_path):
    checker = getattr(client, "s3_object_already_materialized", None)
    if not callable(checker):
        return False
    try:
        return bool(checker(object_path))
    except Exception:
        return False


def materialize_processed_rows(client, object_path, rows, source_name="s3-processed-final"):
    normalized = []
    skipped_invalid = 0
    for row in rows:
        if (row.get("eventType") or "CANDLE") != "CANDLE":
            continue
        candle = normalize_processed_candle_row(row)
        try:
            source_interval = normalize_chart_interval(candle.get("interval"))
        except ValueError:
            skipped_invalid += 1
            continue
        if source_interval not in {"1m", "1D"}:
            skipped_invalid += 1
            continue
        if require_historical_canonical_materialization() and not is_historical_canonical(
            candle.get("priceAdjustment"),
            candle.get("canonicalVersion"),
        ):
            skipped_invalid += 1
            continue
        if invalid_candle_reason(candle):
            skipped_invalid += 1
            continue
        normalized.append(candle)

    deduped = dedupe_candles(normalized)
    clickhouse_rows = [candle_to_clickhouse_row(row) for row in deduped]
    if clickhouse_rows:
        client.insert_json_each_row("chart_candles", clickhouse_rows)

    client.insert_json_each_row("storage_object_audit", [storage_object_audit_row(
        object_path,
        clickhouse_rows,
        source_name=source_name,
    )])
    client.insert_json_each_row("load_audit", [{
        "source_name": source_name,
        "object_path": object_path,
        "row_count": len(clickhouse_rows),
        "note": f"S3 processed/final chart candle materialization; skipped_invalid={skipped_invalid}",
    }])
    return {"objectPath": object_path, "rowCount": len(clickhouse_rows), "skippedInvalidRowCount": skipped_invalid}


def storage_object_audit_row(object_path, rows, source_name="s3-processed-final"):
    first = rows[0] if rows else {}
    bucket = None
    if str(object_path).startswith("s3://"):
        bucket = str(object_path)[5:].split("/", 1)[0]
    try:
        object_format = detect_s3_object_format(object_path)
    except ValueError:
        object_format = "virtual"
    return {
        "object_path": object_path,
        "bucket": bucket,
        "dataset": "candles",
        "layer": "candles",
        "symbol": first.get("symbol"),
        "interval": first.get("interval"),
        "object_format": object_format,
        "row_count": len(rows),
        "checksum": None,
        "source": source_name,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:23],
    }


def normalize_processed_candle_row(row):
    required = ["symbol", "interval", "timestamp", "open", "high", "low", "close", "volume"]
    missing = [key for key in required if row.get(key) is None]
    if missing:
        raise ValueError(f"Processed candle row is missing required fields: {', '.join(missing)}")

    ma = dict(row.get("ma") or {})
    for key in ("ma5", "ma20", "ma60"):
        if row.get(key) is not None:
            ma[key] = row.get(key)

    metadata = candle_metadata(row.get("priceAdjustment") or row.get("price_adjustment"), row.get("canonicalVersion") or row.get("canonical_version"))
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
        **metadata,
    }


def dedupe_candles(rows):
    by_key = {}
    ranks = {}
    for index, row in enumerate(rows):
        key = (row["symbol"], row["interval"], row["timestamp"])
        rank = (1 if is_historical_canonical(row.get("priceAdjustment"), row.get("canonicalVersion")) else 0, index)
        if key not in by_key or rank >= ranks[key]:
            by_key[key] = row
            ranks[key] = rank
    return [by_key[key] for key in sorted(by_key)]


def require_historical_canonical_materialization():
    return os.getenv("S3_REQUIRE_CANONICAL_PROCESSED_CANDLES", "true").lower() in {"1", "true", "yes"}


if __name__ == "__main__":
    main()
