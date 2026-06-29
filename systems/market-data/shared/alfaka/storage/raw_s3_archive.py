"""S3 raw archive helpers for Alpaca historical payloads."""

import json
from collections import defaultdict
from datetime import datetime

from alfaka.common.env import utc_now_iso


def upload_raw_page_to_s3(s3, bucket, prefix, data_kind, feed, start, end, page_number, rows_by_symbol):
    total_rows = 0
    for symbol, rows in rows_by_symbol.items():
        if not rows:
            continue
        rows_by_partition = defaultdict(list)
        for row in rows:
            event_time = row.get("t") or start or end
            partition_key = raw_partition_key(prefix, data_kind, symbol, event_time)
            rows_by_partition[partition_key].append({
                "source": "alpaca",
                "feed": feed,
                "channel": data_kind,
                "symbol": symbol,
                "eventTime": event_time,
                "receivedAt": utc_now_iso(),
                "raw": row,
            })

        for partition_key, partition_rows in rows_by_partition.items():
            body = "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in partition_rows) + "\n"
            object_key = f"{partition_key}/part-{page_number:06d}.jsonl"
            s3.put_object(Bucket=bucket, Key=object_key, Body=body.encode("utf-8"), ContentType="application/x-ndjson")
            total_rows += len(partition_rows)
            print(f"S3 raw archive upload: s3://{bucket}/{object_key} rows={len(partition_rows)}", flush=True)
    return total_rows


def raw_partition_key(prefix, channel, symbol, event_time):
    parsed = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
    return f"{prefix}/source=alpaca/channel={channel}/symbol={symbol}/year={parsed:%Y}/month={parsed:%m}/day={parsed:%d}"
