from collections import defaultdict
from datetime import datetime, timezone

from alfaka.storage.processed_s3_objects import flush_processed_rows_to_s3

DEFAULT_ROWS_PER_OBJECT = 10000


def archive_processed_candles_to_s3(
    s3,
    bucket,
    prefix,
    rows,
    output_format="jsonl",
    manifest_prefix=None,
    manifest_layout="compact",
    max_attempts=3,
    retry_sleep_seconds=1,
    rows_per_object=DEFAULT_ROWS_PER_OBJECT,
):
    if output_format not in {"jsonl", "parquet"}:
        raise ValueError("processed candle archive output_format must be jsonl or parquet.")

    rows = list(rows)
    archive_rows = [row for row in rows if is_processed_candle_row(row)]

    buffers = defaultdict(list)
    for row in archive_rows:
        buffers[processed_candle_archive_key(prefix, row)].append(row)

    object_keys = []
    row_count = 0
    chunk_size = positive_int(rows_per_object, DEFAULT_ROWS_PER_OBJECT)
    for archive_key, partition_rows in sorted(buffers.items()):
        for chunk_rows in chunked(partition_rows, chunk_size):
            object_keys.append(
                flush_processed_rows_to_s3(
                    s3,
                    bucket,
                    archive_key,
                    chunk_rows,
                    output_format,
                    manifest_prefix=manifest_prefix,
                    max_attempts=max_attempts,
                    retry_sleep_seconds=retry_sleep_seconds,
                    manifest_layout=manifest_layout,
                )
            )
            row_count += len(chunk_rows)

    return {
        "objectKeys": object_keys,
        "objectCount": len(object_keys),
        "rowCount": row_count,
        "skippedRowCount": len(rows) - len(archive_rows),
    }


def is_processed_candle_row(row):
    if (row.get("eventType") or "CANDLE") != "CANDLE":
        return False
    return bool(row.get("isClosed", row.get("is_closed", True)))


def processed_candle_archive_key(prefix, row):
    archive_time = datetime.now(timezone.utc)
    interval = row.get("interval", "unknown")
    symbol = row.get("symbol", "UNKNOWN")
    return (
        f"{prefix.strip('/')}/candles/interval={interval}/symbol={symbol}"
        f"/archive_year={archive_time:%Y}/archive_month={archive_time:%m}/archive_day={archive_time:%d}"
    )


def chunked(rows, size):
    for index in range(0, len(rows), size):
        yield rows[index:index + size]


def positive_int(value, default):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
