from __future__ import annotations

import hashlib
import json
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


REALTIME_SHARD_COUNT = 32


def symbol_shard(symbol: Any, shard_count: int = REALTIME_SHARD_COUNT) -> str:
    normalized = str(symbol or "_MARKET").strip().upper() or "_MARKET"
    return f"{zlib.crc32(normalized.encode('utf-8')) % shard_count:02d}"


def normalized_symbol(symbol: Any) -> str:
    return str(symbol or "_MARKET").strip().upper() or "_MARKET"


def minute_window(value: Any) -> datetime:
    parsed = parse_time(value)
    return parsed.replace(second=0, microsecond=0)


def processed_v2_partition_key(final_prefix: str, payload: dict[str, Any]) -> str | None:
    event_type = str(payload.get("eventType") or "").upper()
    event_time = minute_window(payload.get("timestamp") or payload.get("eventTime") or payload.get("eventMinute"))
    shard = symbol_shard(payload.get("symbol"))
    root = realtime_v2_prefix(final_prefix, "final-v2", "final")
    if event_type == "CANDLE" and payload.get("isClosed", payload.get("is_closed", True)):
        interval = payload.get("interval") or "unknown"
        return f"{root}/candles/interval={interval}/date={event_time:%Y-%m-%d}/hour={event_time:%H}/shard={shard}"
    if event_type == "MARKET_STATUS" or payload.get("layer") == "events":
        event_name = str(payload.get("statusType") or payload.get("type") or "status").lower().replace("_", "-")
        return f"{root}/events/type={event_name}/date={event_time:%Y-%m-%d}/hour={event_time:%H}/shard={shard}"
    return None


def raw_v2_partition_key(raw_prefix: str, row: dict[str, Any]) -> str:
    event_time = minute_window(row.get("eventTime") or row.get("receivedAt"))
    channel = str(row.get("channel") or "unknown").strip() or "unknown"
    root = realtime_v2_prefix(raw_prefix, "raw-v2/alpaca", "raw/alpaca")
    return f"{root}/channel={channel}/date={event_time:%Y-%m-%d}/hour={event_time:%H}/shard={symbol_shard(row.get('symbol'))}"


def deterministic_realtime_object_key(partition_key: str, rows: Iterable[dict[str, Any]], extension: str) -> str:
    canonical_rows, _duplicates = canonical_rows_with_duplicate_count(rows)
    if not canonical_rows:
        raise ValueError("realtime S3 object에는 최소 한 행이 필요합니다")
    window = minute_window(event_time_for_row(canonical_rows[0]))
    if any(minute_window(event_time_for_row(row)) != window for row in canonical_rows[1:]):
        raise ValueError("realtime S3 object의 모든 행은 같은 UTC minute에 속해야 합니다")
    digest = hashlib.sha256(canonical_json_bytes(canonical_rows)).hexdigest()[:20]
    return f"{partition_key}/part-{window:%Y%m%dT%H%MZ}-{digest}.{extension}"


def realtime_buffer_identity(partition_key: str, row: dict[str, Any]) -> str | tuple[str, datetime]:
    if "/final-v2/" not in f"/{partition_key.strip('/')}" and "/raw-v2/" not in f"/{partition_key.strip('/')}":
        return partition_key
    return partition_key, minute_window(event_time_for_row(row))


def partition_key_from_buffer_identity(identity: str | tuple[str, datetime]) -> str:
    return identity[0] if isinstance(identity, tuple) else identity


def realtime_object_minute(object_key: str) -> datetime | None:
    name = str(object_key or "").rsplit("/", 1)[-1]
    if not name.startswith("part-"):
        return None
    token = name[5:].split("-", 1)[0]
    try:
        return datetime.strptime(token, "%Y%m%dT%H%MZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def realtime_object_overlaps_range(object_key: str, start: Any, end: Any) -> bool:
    minute = realtime_object_minute(object_key)
    if minute is None:
        return False
    start_dt = parse_time(start)
    end_dt = parse_time(end)
    return minute < end_dt and minute + timedelta(minutes=1) > start_dt


def canonical_rows_with_duplicate_count(rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    unique: dict[str, dict[str, Any]] = {}
    count = 0
    for row in rows:
        normalized = dict(row)
        identity = canonical_row_identity(normalized)
        if identity in unique:
            count += 1
            continue
        unique[identity] = normalized
    ordered = sorted(unique.values(), key=canonical_row_sort_key)
    return ordered, count


def canonical_row_identity(row: dict[str, Any]) -> str:
    source_event_id = row.get("sourceEventId") or row.get("source_event_id")
    if source_event_id:
        return f"source:{source_event_id}"
    selected = {
        "eventType": row.get("eventType"),
        "channel": row.get("channel"),
        "symbol": normalized_symbol(row.get("symbol")),
        "interval": row.get("interval"),
        "timestamp": event_time_for_row(row),
        "correctionType": row.get("correctionType"),
        "raw": row.get("raw"),
    }
    return f"row:{hashlib.sha256(canonical_json_bytes(selected)).hexdigest()}"


def canonical_row_sort_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(event_time_for_row(row) or ""),
        normalized_symbol(row.get("symbol")),
        canonical_row_identity(row),
    )


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def event_time_for_row(row: dict[str, Any]) -> Any:
    return row.get("timestamp") or row.get("eventTime") or row.get("eventMinute") or row.get("receivedAt")


def realtime_v2_prefix(configured_prefix: str, v2_suffix: str, v1_suffix: str) -> str:
    prefix = str(configured_prefix or "").strip("/")
    if prefix.endswith(v2_suffix):
        return prefix
    if prefix.endswith(v1_suffix):
        return f"{prefix[:-len(v1_suffix)]}{v2_suffix}".strip("/")
    return f"{prefix}-{v2_suffix.split('/')[0]}" if prefix else v2_suffix


def utc_hours_in_range(start: Any, end: Any) -> list[datetime]:
    start_dt = parse_time(start).replace(minute=0, second=0, microsecond=0)
    end_dt = parse_time(end)
    hours = []
    cursor = start_dt
    while cursor < end_dt:
        hours.append(cursor)
        cursor += timedelta(hours=1)
    return hours


def parse_time(value: Any) -> datetime:
    if value is None:
        raise ValueError("realtime S3 row에 event timestamp가 필요합니다")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
