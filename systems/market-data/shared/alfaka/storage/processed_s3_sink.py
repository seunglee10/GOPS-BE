# 역할: Kafka Processed Topic의 차트 데이터를 S3/MinIO에 장기 저장합니다.
# 사용: 로컬은 MinIO, 운영은 AWS S3로 같은 코드가 동작합니다.
# 출력: 전날까지 확정 데이터는 market-data/final, 오늘 live/tick은 market-data/live 아래에 저장합니다.
import io
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from alfaka.common.env import load_dotenv, parse_csv
from alfaka.common.kafka_io import create_json_consumer
from alfaka.common.runtime_config import validate_required_values
from alfaka.storage.s3_manifest import DEFAULT_MANIFEST_PREFIX, write_processed_candle_manifest


def main():
    load_dotenv()
    kafka_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    group_id = os.getenv("KAFKA_S3_GROUP_ID", "alfaka-processed-s3-sink")
    s3_bucket = os.getenv("S3_BUCKET")
    final_prefix = os.getenv("S3_FINAL_PREFIX", os.getenv("S3_PROCESSED_PREFIX", "market-data/final"))
    live_prefix = os.getenv("S3_LIVE_PREFIX", "market-data/live")
    manifest_prefix = os.getenv("S3_MANIFEST_PREFIX", DEFAULT_MANIFEST_PREFIX)
    flush_count = int(os.getenv("S3_FLUSH_COUNT", "500"))
    flush_interval_seconds = parse_non_negative_float(os.getenv("S3_FLUSH_INTERVAL_SECONDS", "60"), default=60)
    poll_timeout_ms = parse_positive_int(os.getenv("S3_SINK_POLL_TIMEOUT_MS", "1000"), default=1000)
    put_max_attempts = parse_positive_int(os.getenv("S3_PUT_MAX_ATTEMPTS", "3"), default=3)
    put_retry_sleep_seconds = parse_non_negative_float(os.getenv("S3_PUT_RETRY_SLEEP_SECONDS", "1"), default=1)
    output_format = os.getenv("S3_PROCESSED_FORMAT", "parquet").lower()

    if not s3_bucket:
        print("S3_BUCKET을 .env 또는 Kubernetes ConfigMap에 넣어주세요.", file=sys.stderr)
        sys.exit(1)
    if output_format not in {"parquet", "jsonl"}:
        print("S3_PROCESSED_FORMAT은 parquet 또는 jsonl만 가능합니다.", file=sys.stderr)
        sys.exit(1)

    topics = parse_csv(os.getenv("KAFKA_PROCESSED_TOPICS", ",".join([
        os.getenv("KAFKA_TICKS_TOPIC", "market.ticks.v1"),
        os.getenv("KAFKA_LIVE_CANDLE_TOPIC", "market.candles.live.1m.v1"),
        os.getenv("KAFKA_CLOSED_CANDLE_TOPIC", "market.candles.closed.v1"),
        os.getenv("KAFKA_STATUS_TOPIC", "market.status.v1"),
        os.getenv("KAFKA_VOLUME_PROFILE_BINS_TOPIC", "market.volume-profile-bins.1m.v1"),
    ])))
    validate_required_values("processed s3 sink", {
        "kafka_servers": kafka_servers,
        "processed_topics": topics,
        "s3_bucket": s3_bucket,
        "final_prefix": final_prefix,
        "live_prefix": live_prefix,
    })

    consumer = create_json_consumer(topics, kafka_servers, group_id, "alfaka-processed-s3-consumer")
    from alfaka.common.s3_client import create_s3_client

    s3 = create_s3_client()
    print(f"S3 sink 시작: topics={topics}", flush=True)
    print(f"S3 확정 저장 위치: s3://{s3_bucket}/{final_prefix}, format={output_format}", flush=True)
    print(f"S3 live 저장 위치: s3://{s3_bucket}/{live_prefix}, format={output_format}", flush=True)

    run_processed_s3_sink(
        consumer,
        s3,
        s3_bucket,
        final_prefix,
        live_prefix,
        output_format,
        manifest_prefix=manifest_prefix,
        flush_count=flush_count,
        flush_interval_seconds=flush_interval_seconds,
        poll_timeout_ms=poll_timeout_ms,
        put_max_attempts=put_max_attempts,
        put_retry_sleep_seconds=put_retry_sleep_seconds,
    )


def run_processed_s3_sink(
    consumer,
    s3,
    bucket,
    final_prefix,
    live_prefix,
    output_format,
    manifest_prefix=None,
    flush_count=500,
    flush_interval_seconds=60,
    poll_timeout_ms=1000,
    put_max_attempts=3,
    put_retry_sleep_seconds=1,
    now_fn=None,
):
    buffers = defaultdict(list)
    last_updated_at = {}
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    try:
        while True:
            batches = consumer.poll(timeout_ms=poll_timeout_ms)
            for records in batches.values():
                for record in records:
                    payload = record.value
                    partition_key = s3_partition_key(final_prefix, live_prefix, payload)
                    buffers[partition_key].append(payload)
                    last_updated_at[partition_key] = now_fn()
                    if len(buffers[partition_key]) >= flush_count:
                        flush_buffer(
                            s3,
                            bucket,
                            partition_key,
                            buffers[partition_key],
                            output_format,
                            manifest_prefix=manifest_prefix,
                            max_attempts=put_max_attempts,
                            retry_sleep_seconds=put_retry_sleep_seconds,
                        )
                        buffers[partition_key].clear()
                        last_updated_at.pop(partition_key, None)
            flush_due_buffers(
                buffers,
                last_updated_at,
                lambda partition_key, rows: flush_buffer(
                    s3,
                    bucket,
                    partition_key,
                    rows,
                    output_format,
                    manifest_prefix=manifest_prefix,
                    max_attempts=put_max_attempts,
                    retry_sleep_seconds=put_retry_sleep_seconds,
                ),
                flush_interval_seconds,
                now_fn(),
            )
    except KeyboardInterrupt:
        print("S3 sink 종료 신호 수신: 남은 buffer를 flush합니다.", flush=True)
    finally:
        flush_all_buffers(
            buffers,
            last_updated_at,
            lambda partition_key, rows: flush_buffer(
                s3,
                bucket,
                partition_key,
                rows,
                output_format,
                manifest_prefix=manifest_prefix,
                max_attempts=put_max_attempts,
                retry_sleep_seconds=put_retry_sleep_seconds,
            ),
        )


def s3_partition_key(final_prefix, live_prefix, payload):
    event_type = payload.get("eventType")
    event_time = parse_event_time(payload.get("timestamp") or payload.get("eventTime") or payload.get("eventMinute"))
    symbol = payload.get("symbol", "UNKNOWN")

    if event_type in {"CANDLE", "LIVE_CANDLE"}:
        interval = payload.get("interval", "unknown")
        prefix = final_prefix if event_type == "CANDLE" and is_before_current_market_day(event_time) else live_prefix
        return f"{prefix}/candles/interval={interval}/symbol={symbol}/year={event_time:%Y}/month={event_time:%m}/day={event_time:%d}"

    if event_type == "VOLUME_PROFILE_BIN":
        return f"{final_prefix}/volume-profile-bins/timeBucket=1m/symbol={symbol}/year={event_time:%Y}/month={event_time:%m}/day={event_time:%d}"

    if event_type == "MARKET_STATUS":
        return f"{final_prefix}/status/symbol={symbol or '_MARKET'}/year={event_time:%Y}/month={event_time:%m}/day={event_time:%d}"

    return f"{live_prefix}/trades/symbol={symbol}/year={event_time:%Y}/month={event_time:%m}/day={event_time:%d}/hour={event_time:%H}"


def flush_buffer(
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


def flush_due_buffers(buffers, last_updated_at, flush_fn, flush_interval_seconds, now):
    if flush_interval_seconds <= 0:
        return []
    flushed = []
    for partition_key, rows in list(buffers.items()):
        if not rows:
            continue
        last_updated = last_updated_at.get(partition_key, now)
        age = (now - last_updated).total_seconds()
        if age < flush_interval_seconds:
            continue
        flushed.append(flush_fn(partition_key, rows))
        buffers[partition_key].clear()
        last_updated_at.pop(partition_key, None)
    return flushed


def flush_all_buffers(buffers, last_updated_at, flush_fn):
    flushed = []
    for partition_key, rows in list(buffers.items()):
        if not rows:
            continue
        flushed.append(flush_fn(partition_key, rows))
        buffers[partition_key].clear()
        last_updated_at.pop(partition_key, None)
    return flushed


def put_object_with_retry(s3, put_kwargs, max_attempts=3, retry_sleep_seconds=1):
    max_attempts = max(1, int(max_attempts or 1))
    for attempt in range(1, max_attempts + 1):
        try:
            return s3.put_object(**put_kwargs)
        except Exception:
            if attempt >= max_attempts:
                raise
            time.sleep(retry_sleep_seconds)


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


def parse_event_time(value):
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def is_before_current_market_day(value):
    market_timezone = ZoneInfo(os.getenv("MARKET_TIMEZONE", "America/New_York"))
    return value.astimezone(market_timezone).date() < datetime.now(market_timezone).date()


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


def parse_positive_int(value, default):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def parse_non_negative_float(value, default):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default
