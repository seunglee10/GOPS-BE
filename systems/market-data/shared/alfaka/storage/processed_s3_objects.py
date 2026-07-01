import io
import json
import time
from datetime import datetime, timezone

from alfaka.storage.s3_manifest import write_processed_candle_manifest


def flush_processed_rows_to_s3(
    s3,
    bucket,
    partition_key,
    rows,
    output_format,
    manifest_prefix=None,
    max_attempts=3,
    retry_sleep_seconds=1,
    manifest_layout="daily",
):
    now = datetime.now(timezone.utc)
    storage_rows = [normalize_storage_row(row) for row in rows]
    if output_format == "parquet":
        body = rows_to_parquet(storage_rows)
        object_key = f"{partition_key}/part-{now:%Y%m%dT%H%M%S%f}.parquet"
        content_type = "application/vnd.apache.parquet"
    else:
        body = ("\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in storage_rows) + "\n").encode("utf-8")
        object_key = f"{partition_key}/part-{now:%Y%m%dT%H%M%S%f}.jsonl"
        content_type = "application/x-ndjson"

    put_object_with_retry(
        s3,
        {"Bucket": bucket, "Key": object_key, "Body": body, "ContentType": content_type},
        max_attempts=max_attempts,
        retry_sleep_seconds=retry_sleep_seconds,
    )
    if manifest_prefix and is_processed_candle_partition(partition_key, rows):
        write_processed_candle_manifest(
            s3,
            bucket,
            manifest_prefix,
            object_key,
            storage_rows,
            layout=manifest_layout,
            put_object=lambda **kwargs: put_object_with_retry(
                s3,
                kwargs,
                max_attempts=max_attempts,
                retry_sleep_seconds=retry_sleep_seconds,
            ),
        )
    print(f"S3 업로드: s3://{bucket}/{object_key} rows={len(rows)}", flush=True)
    return object_key


def is_processed_candle_partition(partition_key, rows):
    if "/candles/interval=" not in partition_key:
        return False
    return any((row.get("eventType") or "CANDLE") == "CANDLE" and row.get("isClosed", row.get("is_closed", True)) for row in rows)


def normalize_storage_row(row):
    # Kafka/Redis에서는 ma 딕셔너리를 유지해도 되지만 Parquet은 빈 struct를 쓰지 못합니다.
    # S3 저장용으로 ma를 ma5/ma20/ma60 컬럼으로 평탄화합니다.
    normalized = dict(row)
    ma = normalized.pop("ma", None)
    if isinstance(ma, dict):
        for period in (5, 20, 60):
            key = f"ma{period}"
            normalized.setdefault(key, ma.get(key))

    normalized.setdefault("ma5", None)
    normalized.setdefault("ma20", None)
    normalized.setdefault("ma60", None)
    if "raw" in normalized and not isinstance(normalized["raw"], (str, type(None))):
        normalized["raw"] = json.dumps(normalized["raw"], ensure_ascii=False, separators=(",", ":"))
    return normalized


def rows_to_parquet(rows):
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("S3_PROCESSED_FORMAT=parquet에는 pyarrow가 필요합니다. requirements.txt 설치 후 다시 실행해주세요.") from exc

    output = io.BytesIO()
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, output, compression="snappy")
    return output.getvalue()


def put_object_with_retry(s3, put_kwargs, max_attempts=3, retry_sleep_seconds=1):
    max_attempts = max(1, int(max_attempts or 1))
    for attempt in range(1, max_attempts + 1):
        try:
            return s3.put_object(**put_kwargs)
        except Exception:
            if attempt >= max_attempts:
                raise
            time.sleep(retry_sleep_seconds)
