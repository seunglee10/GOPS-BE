"""S3 raw archive helpers for Alpaca historical payloads."""

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime

from market_data.alpaca.feed_profiles import market_session_for_timestamp
from market_data.common.canonical import candle_metadata
from market_data.common.env import utc_now_iso
from market_data.storage.s3_manifest import normalize_raw_channel, write_raw_manifest


def upload_raw_page_to_s3(
    s3,
    bucket,
    prefix,
    data_kind,
    feed,
    start,
    end,
    page_number,
    rows_by_symbol,
    manifest_prefix=None,
    object_id=None,
    partition_mode="daily",
    price_adjustment=None,
    canonical_version=None,
):
    total_rows = 0
    object_suffix = raw_object_suffix(data_kind, feed, start, end, page_number, object_id=object_id)
    for symbol, rows in rows_by_symbol.items():
        if not rows:
            continue
        rows_by_partition = defaultdict(list)
        for row in rows:
            event_time = row.get("t") or start or end
            partition_key = raw_chunk_partition_key(prefix, data_kind, symbol, object_suffix) if normalize_partition_mode(partition_mode) == "chunk" else raw_partition_key(prefix, data_kind, symbol, event_time)
            archive_row = {
                "source": "alpaca",
                "feed": feed,
                "feedProfile": feed,
                "marketSession": market_session_for_timestamp(event_time),
                "channel": data_kind,
                "symbol": symbol,
                "eventTime": event_time,
                "receivedAt": utc_now_iso(),
                "raw": row,
            }
            if normalize_raw_channel(data_kind) in {"bars", "updated-bars", "daily-bars"}:
                archive_row.update(candle_metadata(price_adjustment, canonical_version))
            rows_by_partition[partition_key].append(archive_row)

        for partition_key, partition_rows in rows_by_partition.items():
            body = "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in partition_rows) + "\n"
            object_key = f"{partition_key}/part-{page_number:06d}-{object_suffix}.jsonl"
            s3.put_object(Bucket=bucket, Key=object_key, Body=body.encode("utf-8"), ContentType="application/x-ndjson")
            if manifest_prefix:
                write_raw_manifest(
                    s3,
                    bucket,
                    manifest_prefix,
                    object_key,
                    partition_rows,
                    layout="compact" if normalize_partition_mode(partition_mode) == "chunk" else "daily",
                )
            total_rows += len(partition_rows)
            print(f"S3 raw archive upload: s3://{bucket}/{object_key} rows={len(partition_rows)}", flush=True)
    return total_rows


def raw_partition_key(prefix, channel, symbol, event_time):
    parsed = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
    return f"{prefix}/source=alpaca/channel={normalize_raw_channel(channel)}/symbol={symbol}/year={parsed:%Y}/month={parsed:%m}/day={parsed:%d}"


def raw_chunk_partition_key(prefix, channel, symbol, object_suffix):
    return f"{prefix}/source=alpaca/channel={normalize_raw_channel(channel)}/symbol={symbol}/request={object_suffix}"


def raw_object_suffix(data_kind, feed, start, end, page_number, object_id=None):
    if object_id:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(object_id)).strip("._-")
        if safe:
            return safe[:96]
    payload = json.dumps(
        {
            "dataKind": data_kind,
            "feed": feed,
            "start": start,
            "end": end,
            "pageNumber": page_number,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def normalize_partition_mode(value):
    normalized = str(value or "daily").strip().lower().replace("-", "_")
    return "chunk" if normalized in {"chunk", "request", "compact"} else "daily"
