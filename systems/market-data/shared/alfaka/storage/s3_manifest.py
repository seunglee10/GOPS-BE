import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

from alfaka.common.canonical import is_historical_canonical
from alfaka.storage.s3_realtime_layout import realtime_v2_prefix, symbol_shard, utc_hours_in_range
from alfaka.common.env import utc_now_iso
from alfaka.storage.s3_materializer import list_s3_objects


DEFAULT_MANIFEST_PREFIX = "market-data/rebuild-20260702-lazy-v1/manifest"


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
    price_adjustments = sorted({
        str(row.get("priceAdjustment") or row.get("price_adjustment") or "unknown").strip().lower()
        for row in candle_rows
    })
    canonical_versions = sorted({
        str(row.get("canonicalVersion") or row.get("canonical_version") or "legacy").strip().lower()
        for row in candle_rows
    })
    bucket_date = parse_time(timestamps[0]).date().isoformat()
    return {
        "schemaVersion": 1,
        "dataset": "candles",
        "symbol": symbols[0],
        "interval": intervals[0],
        "priceAdjustment": single_or_mixed(price_adjustments, "unknown"),
        "canonicalVersion": single_or_mixed(canonical_versions, "legacy"),
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
    price_adjustments = sorted({
        str(row.get("priceAdjustment") or row.get("price_adjustment") or "unknown").strip().lower()
        for row in raw_rows
    })
    canonical_versions = sorted({
        str(row.get("canonicalVersion") or row.get("canonical_version") or "legacy").strip().lower()
        for row in raw_rows
    })
    bucket_date = parse_time(timestamps[0]).date().isoformat()
    return {
        "schemaVersion": 1,
        "dataset": "raw",
        "source": "alpaca",
        "channel": channels[0],
        "symbol": symbols[0],
        "priceAdjustment": single_or_mixed(price_adjustments, "unknown"),
        "canonicalVersion": single_or_mixed(canonical_versions, "legacy"),
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
    return select_preferred_manifest_entries(sort_unique_manifest_entries(entries), start, end)


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


def bounded_v2_processed_candle_keys(s3, bucket, final_prefix, symbol, interval, start, end, metrics=None):
    keys = []
    root = realtime_v2_prefix(final_prefix, "final-v2", "final")
    shard = symbol_shard(symbol)
    for hour in utc_hours_in_range(start, end):
        prefix = (
            f"{root}/candles/interval={interval}/date={hour:%Y-%m-%d}"
            f"/hour={hour:%H}/shard={shard}/"
        )
        keys.extend(key for key in list_s3_objects(s3, bucket, prefix, metrics=metrics) if key.endswith((".parquet", ".jsonl", ".ndjson")))
    return unique_ordered(keys)


def bounded_v2_raw_keys(s3, bucket, raw_prefix, symbol, channels, start, end, metrics=None):
    keys = []
    root = realtime_v2_prefix(raw_prefix, "raw-v2/alpaca", "raw/alpaca")
    shard = symbol_shard(symbol)
    for channel in channels:
        normalized_channel = normalize_raw_channel(channel)
        for hour in utc_hours_in_range(start, end):
            prefix = (
                f"{root}/channel={normalized_channel}/date={hour:%Y-%m-%d}"
                f"/hour={hour:%H}/shard={shard}/"
            )
            keys.extend(key for key in list_s3_objects(s3, bucket, prefix, metrics=metrics) if key.endswith((".jsonl", ".ndjson", ".parquet")))
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
    if require_canonical_processed_manifest() and not is_historical_canonical(entry.get("priceAdjustment"), entry.get("canonicalVersion")):
        return False
    available_from = entry.get("availableFrom")
    available_to = entry.get("availableTo")
    if not available_from or not available_to:
        return False
    return str(available_to) >= str(start) and str(available_from) < str(end)


def raw_entry_matches_range(entry, symbol, channel, start, end):
    if not entry:
        return False
    normalized_channel = normalize_raw_channel(channel)
    if entry.get("symbol") != symbol or normalize_raw_channel(entry.get("channel")) != normalized_channel:
        return False
    if (
        normalized_channel in {"bars", "updated-bars", "daily-bars"}
        and require_canonical_processed_manifest()
        and not is_historical_canonical(entry.get("priceAdjustment"), entry.get("canonicalVersion"))
    ):
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


def select_preferred_manifest_entries(entries, start, end):
    _ = (start, end)
    selected = []
    for entry in sorted(entries, key=manifest_entry_priority_key):
        if any(entry_range_covers(existing, entry) for existing in selected):
            continue
        selected.append(entry)
    return sorted(selected, key=lambda item: (item.get("availableFrom") or "", item.get("objectKey") or ""))


def manifest_entry_priority_key(entry):
    object_key = entry.get("objectKey") or ""
    start = entry.get("availableFrom") or ""
    end = entry.get("availableTo") or ""
    duration = manifest_entry_duration_seconds(entry)
    return (
        0 if is_historical_canonical(entry.get("priceAdjustment"), entry.get("canonicalVersion")) else 1,
        manifest_object_priority(object_key, entry.get("objectFormat")),
        start,
        -duration,
        end,
        object_key,
    )


def manifest_object_priority(object_key, object_format=None):
    key = str(object_key or "")
    fmt = str(object_format or object_format_from_key(key))
    if "/live/" in key:
        return 5
    if "/backfill_request=" in key and fmt == "parquet":
        return 0
    if "/backfill_request=" in key:
        return 1
    if fmt == "parquet":
        return 2
    if fmt == "jsonl":
        return 3
    return 4


def manifest_entry_duration_seconds(entry):
    start = entry.get("availableFrom")
    end = entry.get("availableTo")
    if not start or not end:
        return 0
    try:
        return int((parse_time(end) - parse_time(start)).total_seconds())
    except Exception:
        return 0


def entry_range_covers(covering, candidate):
    covering_from = covering.get("availableFrom")
    covering_to = covering.get("availableTo")
    candidate_from = candidate.get("availableFrom")
    candidate_to = candidate.get("availableTo")
    if not covering_from or not covering_to or not candidate_from or not candidate_to:
        return False
    return str(covering_from) <= str(candidate_from) and str(covering_to) >= str(candidate_to)


def normalize_manifest_layout(layout):
    value = str(layout or "daily").strip().lower()
    return "compact" if value == "compact" else "daily"


def single_or_mixed(values, default):
    clean_values = [value for value in values if value]
    if not clean_values:
        return default
    if len(clean_values) == 1:
        return clean_values[0]
    return "mixed"


def require_canonical_processed_manifest():
    return os.getenv("S3_REQUIRE_CANONICAL_PROCESSED_CANDLES", "true").lower() in {"1", "true", "yes"}
