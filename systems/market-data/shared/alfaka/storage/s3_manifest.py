import hashlib
import json
from datetime import datetime, timedelta, timezone

from alfaka.common.env import utc_now_iso
from alfaka.storage.s3_materializer import list_s3_objects


DEFAULT_MANIFEST_PREFIX = "market-data/manifest"


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


def write_raw_manifest(s3, bucket, manifest_prefix, object_key, rows, put_object=None, layout="daily"):
    entry = build_raw_manifest(object_key, rows)
    if not entry:
        return None
    manifest_key = (
        raw_compact_manifest_key(manifest_prefix, entry, object_key)
        if normalize_manifest_layout(layout) == "compact"
        else raw_manifest_key(manifest_prefix, entry, object_key)
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


def build_raw_manifest(object_key, rows):
    raw_rows = [row for row in rows if row.get("raw") is not None]
    if not raw_rows:
        return None
    symbols = sorted({row.get("symbol") for row in raw_rows if row.get("symbol")})
    channels = sorted({normalize_raw_channel(row.get("channel")) for row in raw_rows if row.get("channel")})
    timestamps = sorted((row.get("eventTime") or (row.get("raw") or {}).get("t")) for row in raw_rows if row.get("eventTime") or (row.get("raw") or {}).get("t"))
    if len(symbols) != 1 or len(channels) != 1 or not timestamps:
        return None
    bucket_date = parse_time(timestamps[0]).date().isoformat()
    return {
        "schemaVersion": 1,
        "dataset": "raw",
        "source": "alpaca",
        "channel": channels[0],
        "symbol": symbols[0],
        "bucketDate": bucket_date,
        "objectKey": object_key,
        "rowCount": len(raw_rows),
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


def raw_manifest_key(manifest_prefix, entry, object_key):
    parsed_date = datetime.fromisoformat(entry["bucketDate"])
    digest = hashlib.sha1(object_key.encode("utf-8")).hexdigest()[:16]
    return (
        f"{manifest_prefix.strip('/')}/raw/channel={entry['channel']}/symbol={entry['symbol']}"
        f"/year={parsed_date:%Y}/month={parsed_date:%m}/day={parsed_date:%d}/{digest}.json"
    )


def raw_compact_manifest_key(manifest_prefix, entry, object_key):
    digest = hashlib.sha1(object_key.encode("utf-8")).hexdigest()[:16]
    return (
        f"{manifest_prefix.strip('/')}/raw/channel={entry['channel']}/symbol={entry['symbol']}"
        f"/objects/{digest}.json"
    )


def processed_candle_manifest_entries(s3, bucket, manifest_prefix, symbol, interval, start, end):
    entries = []
    prefixes = [
        processed_candle_compact_manifest_prefix(manifest_prefix, symbol, interval),
        *manifest_prefixes_for_range(manifest_prefix, symbol, interval, start, end),
    ]
    for prefix in prefixes:
        for key in list_s3_objects(s3, bucket, prefix):
            if not key.endswith(".json"):
                continue
            entry = read_manifest_entry(s3, bucket, key)
            if entry_matches_range(entry, symbol, interval, start, end):
                entries.append(entry)
    return sort_unique_manifest_entries(entries)


def raw_manifest_entries(s3, bucket, manifest_prefix, symbol, channels, start, end):
    entries = []
    for channel in channels:
        prefixes = [
            raw_compact_manifest_prefix(manifest_prefix, symbol, channel),
            *raw_manifest_prefixes_for_range(manifest_prefix, symbol, channel, start, end),
        ]
        for prefix in prefixes:
            for key in list_s3_objects(s3, bucket, prefix):
                if not key.endswith(".json"):
                    continue
                entry = read_manifest_entry(s3, bucket, key)
                if raw_entry_matches_range(entry, symbol, channel, start, end):
                    entries.append(entry)
    return sort_unique_manifest_entries(entries)


def processed_candle_keys_from_manifest(s3, bucket, manifest_prefix, symbol, interval, start, end):
    return unique_ordered(entry["objectKey"] for entry in processed_candle_manifest_entries(s3, bucket, manifest_prefix, symbol, interval, start, end) if entry.get("objectKey"))


def raw_keys_from_manifest(s3, bucket, manifest_prefix, symbol, channels, start, end):
    return unique_ordered(entry["objectKey"] for entry in raw_manifest_entries(s3, bucket, manifest_prefix, symbol, channels, start, end) if entry.get("objectKey"))


def bounded_processed_candle_partition_keys(s3, bucket, final_prefix, symbol, interval, start, end):
    keys = []
    for day in utc_days_in_range(start, end):
        prefix = f"{final_prefix.strip('/')}/candles/interval={interval}/symbol={symbol}/year={day:%Y}/month={day:%m}/day={day:%d}/"
        keys.extend(key for key in list_s3_objects(s3, bucket, prefix) if key.endswith((".jsonl", ".ndjson", ".parquet")))
    return unique_ordered(keys)


def bounded_raw_partition_keys(s3, bucket, raw_prefix, symbol, channels, start, end):
    keys = []
    for channel in channels:
        for day in utc_days_in_range(start, end):
            prefix = f"{raw_prefix.strip('/')}/source=alpaca/channel={channel}/symbol={symbol}/year={day:%Y}/month={day:%m}/day={day:%d}/"
            keys.extend(key for key in list_s3_objects(s3, bucket, prefix) if key.endswith((".jsonl", ".ndjson", ".parquet")))
    return unique_ordered(keys)


def manifest_prefixes_for_range(manifest_prefix, symbol, interval, start, end):
    return [
        f"{manifest_prefix.strip('/')}/candles/interval={interval}/symbol={symbol}/year={day:%Y}/month={day:%m}/day={day:%d}/"
        for day in utc_days_in_range(start, end)
    ]


def processed_candle_compact_manifest_prefix(manifest_prefix, symbol, interval):
    return f"{manifest_prefix.strip('/')}/candles/interval={interval}/symbol={symbol}/objects/"


def raw_manifest_prefixes_for_range(manifest_prefix, symbol, channel, start, end):
    return [
        f"{manifest_prefix.strip('/')}/raw/channel={channel}/symbol={symbol}/year={day:%Y}/month={day:%m}/day={day:%d}/"
        for day in utc_days_in_range(start, end)
    ]


def raw_compact_manifest_prefix(manifest_prefix, symbol, channel):
    return f"{manifest_prefix.strip('/')}/raw/channel={normalize_raw_channel(channel)}/symbol={symbol}/objects/"


def utc_days_in_range(start, end):
    start_dt = parse_time(start)
    end_dt = parse_time(end)
    if start_dt >= end_dt:
        return []
    day = start_dt.date()
    end_day = (end_dt - timedelta(milliseconds=1)).date()
    days = []
    while day <= end_day:
        days.append(day)
        day += timedelta(days=1)
    return days


def read_manifest_entry(s3, bucket, key):
    response = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(response["Body"].read().decode("utf-8"))


def entry_matches_range(entry, symbol, interval, start, end):
    if not entry:
        return False
    if entry.get("symbol") != symbol or entry.get("interval") != interval:
        return False
    available_from = entry.get("availableFrom")
    available_to = entry.get("availableTo")
    if not available_from or not available_to:
        return False
    return str(available_to) >= str(start) and str(available_from) < str(end)


def raw_entry_matches_range(entry, symbol, channel, start, end):
    if not entry:
        return False
    if entry.get("symbol") != symbol or normalize_raw_channel(entry.get("channel")) != normalize_raw_channel(channel):
        return False
    available_from = entry.get("availableFrom")
    available_to = entry.get("availableTo")
    if not available_from or not available_to:
        return False
    return str(available_to) >= str(start) and str(available_from) < str(end)


def normalize_raw_channel(channel):
    value = str(channel or "").strip()
    return {
        "dailyBars": "daily-bars",
        "updatedBars": "updated-bars",
        "cancelErrors": "cancel-errors",
    }.get(value, value)


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


def unique_ordered(values):
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def sort_unique_manifest_entries(entries):
    seen = set()
    unique = []
    for entry in sorted(entries, key=lambda item: (item.get("availableFrom") or "", item.get("objectKey") or "")):
        object_key = entry.get("objectKey")
        if not object_key or object_key in seen:
            continue
        seen.add(object_key)
        unique.append(entry)
    return unique


def normalize_manifest_layout(layout):
    value = str(layout or "daily").strip().lower()
    return "compact" if value == "compact" else "daily"
