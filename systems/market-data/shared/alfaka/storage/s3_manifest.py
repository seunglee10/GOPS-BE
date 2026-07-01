import hashlib
import json
from datetime import datetime, timezone

from alfaka.common.env import utc_now_iso


DEFAULT_MANIFEST_PREFIX = "market-data/dev/helixho/manifest"


def write_processed_candle_manifest(s3, bucket, manifest_prefix, object_key, rows, put_object=None, layout="daily"):
    entry = build_processed_candle_manifest(object_key, rows)
    if not entry:
        return None
    manifest_key = (
        processed_candle_compact_manifest_key(manifest_prefix, entry, object_key)
        if normalize_manifest_layout(layout) == "compact"
        else processed_candle_manifest_key(manifest_prefix, entry, object_key)
    )
    body = json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    put_object = put_object or s3.put_object
    put_object(Bucket=bucket, Key=manifest_key, Body=body, ContentType="application/json")
    return manifest_key


def build_processed_candle_manifest(object_key, rows):
    candle_rows = [row for row in rows if (row.get("eventType") or "CANDLE") == "CANDLE"]
    if not candle_rows:
        return None
    symbols = sorted({row.get("symbol") for row in candle_rows if row.get("symbol")})
    intervals = sorted({row.get("interval") for row in candle_rows if row.get("interval")})
    timestamps = sorted(row.get("timestamp") for row in candle_rows if row.get("timestamp"))
    if len(symbols) != 1 or len(intervals) != 1 or not timestamps:
        return None
    bucket_date = parse_time(timestamps[0]).date().isoformat()
    return {
        "schemaVersion": 1,
        "dataset": "candles",
        "symbol": symbols[0],
        "interval": intervals[0],
        "bucketDate": bucket_date,
        "objectKey": object_key,
        "rowCount": len(candle_rows),
        "availableFrom": timestamps[0],
        "availableTo": timestamps[-1],
        "objectFormat": object_format_from_key(object_key),
        "createdAt": utc_now_iso(),
    }


def processed_candle_manifest_key(manifest_prefix, entry, object_key):
    parsed_date = datetime.fromisoformat(entry["bucketDate"])
    digest = hashlib.sha1(object_key.encode("utf-8")).hexdigest()[:16]
    return (
        f"{manifest_prefix.strip('/')}/candles/interval={entry['interval']}/symbol={entry['symbol']}"
        f"/year={parsed_date:%Y}/month={parsed_date:%m}/day={parsed_date:%d}/{digest}.json"
    )


def processed_candle_compact_manifest_key(manifest_prefix, entry, object_key):
    digest = hashlib.sha1(object_key.encode("utf-8")).hexdigest()[:16]
    return (
        f"{manifest_prefix.strip('/')}/candles/interval={entry['interval']}/symbol={entry['symbol']}"
        f"/objects/{digest}.json"
    )


def object_format_from_key(key):
    lowered = key.lower()
    if lowered.endswith(".parquet"):
        return "parquet"
    if lowered.endswith(".jsonl") or lowered.endswith(".ndjson"):
        return "jsonl"
    return "unknown"


def parse_time(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def normalize_manifest_layout(layout):
    value = str(layout or "daily").strip().lower()
    return "compact" if value == "compact" else "daily"
