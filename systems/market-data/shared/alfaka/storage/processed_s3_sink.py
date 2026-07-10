# 역할: Kafka Processed Topic의 차트 데이터를 S3/MinIO에 장기 저장합니다.
# 사용: 로컬은 MinIO, 운영은 AWS S3로 같은 코드가 동작합니다.
# 출력: 확정 candle/trades/quotes/events를 final prefix에 저장합니다. live candle은 저장하지 않습니다.
import io
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from alfaka.common.canonical import CANONICAL_VERSION, is_historical_canonical
from alfaka.common.env import load_dotenv, parse_csv
from alfaka.common.kafka_io import create_json_consumer
from alfaka.common.kafka_topics import closed_candle_topic_values
from alfaka.common.runtime_config import validate_required_values
from alfaka.storage.s3_manifest import DEFAULT_MANIFEST_PREFIX, write_processed_candle_manifest
from alfaka.storage.s3_realtime_layout import (
    canonical_rows_with_duplicate_count,
    deterministic_realtime_object_key,
    processed_v2_partition_key,
)


def main():
    load_dotenv()
    kafka_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    group_id = os.getenv("KAFKA_S3_GROUP_ID", "alfaka-processed-s3-sink")
    s3_bucket = os.getenv("S3_BUCKET")
    final_prefix = os.getenv("S3_FINAL_PREFIX", os.getenv("S3_PROCESSED_PREFIX", "market-data/rebuild-20260702-lazy-v1/final"))
    manifest_prefix = os.getenv("S3_MANIFEST_PREFIX", DEFAULT_MANIFEST_PREFIX)
    flush_count = int(os.getenv("S3_FLUSH_COUNT", "500"))
    flush_interval_seconds = parse_non_negative_float(os.getenv("S3_FLUSH_INTERVAL_SECONDS", "60"), default=60)
    poll_timeout_ms = parse_positive_int(os.getenv("S3_SINK_POLL_TIMEOUT_MS", "1000"), default=1000)
    put_max_attempts = parse_positive_int(os.getenv("S3_PUT_MAX_ATTEMPTS", "3"), default=3)
    put_retry_sleep_seconds = parse_non_negative_float(os.getenv("S3_PUT_RETRY_SLEEP_SECONDS", "1"), default=1)
    output_format = os.getenv("S3_PROCESSED_FORMAT", "parquet").lower()
    enable_auto_commit = os.getenv("KAFKA_S3_ENABLE_AUTO_COMMIT", "false").lower() in {"1", "true", "yes"}
    realtime_layout_mode = normalize_realtime_layout_mode(os.getenv("S3_REALTIME_LAYOUT_MODE", "v2"))

    if not s3_bucket:
        print("S3_BUCKET을 .env 또는 Kubernetes ConfigMap에 넣어주세요.", file=sys.stderr)
        sys.exit(1)
    if output_format not in {"parquet", "jsonl"}:
        print("S3_PROCESSED_FORMAT은 parquet 또는 jsonl만 가능합니다.", file=sys.stderr)
        sys.exit(1)

    topics = processed_topics_from_env(os.environ)
    validate_required_values("processed s3 sink", {
        "kafka_servers": kafka_servers,
        "processed_topics": topics,
        "s3_bucket": s3_bucket,
        "final_prefix": final_prefix,
    })

    consumer = create_json_consumer(
        topics,
        kafka_servers,
        group_id,
        "alfaka-processed-s3-consumer",
        enable_auto_commit=enable_auto_commit,
        realtime_layout_mode=realtime_layout_mode,
    )
    from alfaka.common.s3_client import create_s3_client

    s3 = create_s3_client()
    print(f"S3 sink 시작: topics={topics}", flush=True)
    print(f"S3 확정 저장 위치: s3://{s3_bucket}/{final_prefix}, format={output_format}", flush=True)
    print("S3 live candle 저장은 비활성화되어 있습니다. Tick성 trades/quotes는 raw S3/ClickHouse 경로를 사용합니다.", flush=True)

    run_processed_s3_sink(
        consumer,
        s3,
        s3_bucket,
        final_prefix,
        output_format,
        manifest_prefix=manifest_prefix,
        flush_count=flush_count,
        flush_interval_seconds=flush_interval_seconds,
        poll_timeout_ms=poll_timeout_ms,
        put_max_attempts=put_max_attempts,
        put_retry_sleep_seconds=put_retry_sleep_seconds,
        enable_auto_commit=enable_auto_commit,
    )


def processed_topics_from_env(environ=None):
    environ = os.environ if environ is None else environ
    return parse_csv(environ.get("KAFKA_PROCESSED_TOPICS", ",".join([
        *closed_candle_topic_values(environ),
        environ.get("KAFKA_EVENTS_LAYER_TOPIC", "market.layer.events.v1"),
    ])))


def run_processed_s3_sink(
    consumer,
    s3,
    bucket,
    final_prefix,
    output_format,
    manifest_prefix=None,
    flush_count=500,
    flush_interval_seconds=60,
    poll_timeout_ms=1000,
    put_max_attempts=3,
    put_retry_sleep_seconds=1,
    enable_auto_commit=False,
    now_fn=None,
    realtime_layout_mode="v1",
    metrics=None,
):
    buffers = defaultdict(list)
    last_updated_at = {}
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    failed = False
    try:
        while True:
            batches = consumer.poll(timeout_ms=poll_timeout_ms)
            for records in batches.values():
                for record in records:
                    payload = record.value
                    for partition_key in processed_realtime_partition_keys(final_prefix, payload, realtime_layout_mode):
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
                                metrics=metrics,
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
                    metrics=metrics,
                ),
                flush_interval_seconds,
                now_fn(),
            )
            if not enable_auto_commit and not any(buffers.values()):
                commit_consumer(consumer)
    except KeyboardInterrupt:
        print("S3 sink 종료 신호 수신: 남은 buffer를 flush합니다.", flush=True)
    except Exception:
        failed = True
        raise
    finally:
        if not failed:
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
                    metrics=metrics,
                ),
            )
            if not enable_auto_commit:
                commit_consumer(consumer)


def commit_consumer(consumer):
    commit = getattr(consumer, "commit", None)
    if callable(commit):
        commit()


def s3_partition_key(final_prefix, live_prefix_or_payload, payload=None):
    if payload is None:
        payload = live_prefix_or_payload
    event_type = payload.get("eventType")
    event_time = parse_event_time(payload.get("timestamp") or payload.get("eventTime") or payload.get("eventMinute"))
    symbol = payload.get("symbol", "UNKNOWN")

    if event_type == "LIVE_CANDLE":
        return None

    feed = payload.get("feed") or payload.get("feedProfile") or "unknown"

    if event_type == "CANDLE":
        interval = payload.get("interval", "unknown")
        return f"{final_prefix}/candles/feed={feed}/interval={interval}/symbol={symbol}/year={event_time:%Y}/month={event_time:%m}/day={event_time:%d}"


    if event_type == "MARKET_STATUS":
        return f"{final_prefix}/events/event_type=status/symbol={symbol or '_MARKET'}/year={event_time:%Y}/month={event_time:%m}/day={event_time:%d}"

    if event_type == "QUOTE" or payload.get("layer") == "quotes":
        return f"{final_prefix}/quotes/symbol={symbol}/year={event_time:%Y}/month={event_time:%m}/day={event_time:%d}/feed={feed}"

    if event_type == "TRADE":
        return f"{final_prefix}/trades/symbol={symbol}/year={event_time:%Y}/month={event_time:%m}/day={event_time:%d}/feed={feed}"

    return f"{final_prefix}/events/event_type=unknown/symbol={symbol}/year={event_time:%Y}/month={event_time:%m}/day={event_time:%d}"


def processed_realtime_partition_keys(final_prefix, payload, mode="v1"):
    normalized_mode = normalize_realtime_layout_mode(mode)
    keys = []
    if normalized_mode in {"v1", "dual"}:
        v1_key = s3_partition_key(final_prefix, payload)
        if v1_key is not None:
            keys.append(v1_key)
    if normalized_mode in {"v2", "dual"}:
        v2_key = processed_v2_partition_key(final_prefix, payload)
        if v2_key is not None:
            keys.append(v2_key)
    return keys


def normalize_realtime_layout_mode(value):
    normalized = str(value or "v1").strip().lower()
    return normalized if normalized in {"v1", "dual", "v2"} else "v1"


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
    force=False,
    metrics=None,
):
    now = datetime.now(timezone.utc)
    storage_rows = [normalize_storage_row(row) for row in rows]
    if is_realtime_v2_partition(partition_key):
        storage_rows, duplicate_count = canonical_rows_with_duplicate_count(storage_rows)
        increment_metric(metrics, "duplicateRows", duplicate_count)
    object_key = s3_object_key(partition_key, storage_rows, output_format, now=now, force=force)
    content_type = content_type_for_format(output_format)
    if is_deterministic_canonical_object_key(object_key) and not force and s3_object_exists(s3, bucket, object_key):
        if manifest_prefix and not is_realtime_v2_partition(partition_key) and is_processed_candle_partition(partition_key, rows):
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
                    metrics=metrics,
                ),
            )
        print(f"S3 canonical 중복 업로드 skip: s3://{bucket}/{object_key} rows={len(rows)}", flush=True)
        return object_key

    if output_format == "parquet":
        body = rows_to_parquet(storage_rows)
    else:
        body = ("\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in storage_rows) + "\n").encode("utf-8")

    put_object_with_retry(
        s3,
        {"Bucket": bucket, "Key": object_key, "Body": body, "ContentType": content_type},
        max_attempts=max_attempts,
        retry_sleep_seconds=retry_sleep_seconds,
        metrics=metrics,
    )
    increment_metric(metrics, "objects", 1)
    increment_metric(metrics, "rows", len(storage_rows))
    if manifest_prefix and not is_realtime_v2_partition(partition_key) and is_processed_candle_partition(partition_key, rows):
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
                metrics=metrics,
            ),
        )
    print(f"S3 업로드: s3://{bucket}/{object_key} rows={len(rows)}", flush=True)
    return object_key


def s3_object_key(partition_key, rows, output_format, now=None, force=False):
    now = now or datetime.now(timezone.utc)
    ext = "parquet" if output_format == "parquet" else "jsonl"
    if is_realtime_v2_partition(partition_key):
        return deterministic_realtime_object_key(partition_key, rows, ext)
    deterministic_key = deterministic_canonical_candle_object_key(partition_key, rows, ext, now=now, force=force)
    if deterministic_key:
        return deterministic_key
    return f"{partition_key}/part-{now:%Y%m%dT%H%M%S%f}.{ext}"


def deterministic_canonical_candle_object_key(partition_key, rows, ext, now=None, force=False):
    if os.getenv("S3_CANONICAL_DETERMINISTIC_KEYS", "true").lower() not in {"1", "true", "yes"}:
        return None
    if "/backfill_request=" not in partition_key:
        return None
    candle_rows = [
        row for row in rows
        if (row.get("eventType") or "CANDLE") == "CANDLE"
        and row.get("isClosed", row.get("is_closed", True))
    ]
    if not candle_rows or len(candle_rows) != len(rows):
        return None
    if not all(is_historical_canonical(row.get("priceAdjustment"), row.get("canonicalVersion")) for row in candle_rows):
        return None
    symbols = sorted({row.get("symbol") for row in candle_rows if row.get("symbol")})
    intervals = sorted({row.get("interval") for row in candle_rows if row.get("interval")})
    adjustments = sorted({str(row.get("priceAdjustment") or row.get("price_adjustment") or "unknown").lower() for row in candle_rows})
    timestamps = sorted(row.get("timestamp") for row in candle_rows if row.get("timestamp"))
    if len(symbols) != 1 or len(intervals) != 1 or len(adjustments) != 1 or not timestamps:
        return None
    base_partition = partition_key.split("/backfill_request=", 1)[0].rstrip("/")
    range_key = f"{compact_timestamp(timestamps[0])}_{compact_timestamp(timestamps[-1])}"
    base_key = (
        f"{base_partition}/range={range_key}"
        f"/adjustment={adjustments[0]}/canonical={CANONICAL_VERSION}.{ext}"
    )
    if not force:
        return base_key
    now = now or datetime.now(timezone.utc)
    return (
        f"{base_partition}/range={range_key}/adjustment={adjustments[0]}"
        f"/revisions/revision={now:%Y%m%dT%H%M%S%f}/canonical={CANONICAL_VERSION}.{ext}"
    )


def compact_timestamp(value):
    return parse_event_time(value).strftime("%Y%m%dT%H%M%SZ")


def content_type_for_format(output_format):
    return "application/vnd.apache.parquet" if output_format == "parquet" else "application/x-ndjson"


def is_deterministic_canonical_object_key(object_key):
    return "/range=" in object_key and f"/canonical={CANONICAL_VERSION}." in object_key


def s3_object_exists(s3, bucket, key):
    head_object = getattr(s3, "head_object", None)
    if callable(head_object):
        try:
            head_object(Bucket=bucket, Key=key)
            return True
        except Exception:
            return False
    try:
        s3.get_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False


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


def put_object_with_retry(s3, put_kwargs, max_attempts=3, retry_sleep_seconds=1, metrics=None):
    max_attempts = max(1, int(max_attempts or 1))
    for attempt in range(1, max_attempts + 1):
        try:
            return s3.put_object(**put_kwargs)
        except Exception:
            if attempt >= max_attempts:
                raise
            increment_metric(metrics, "putRetries", 1)
            time.sleep(retry_sleep_seconds)


def is_realtime_v2_partition(partition_key):
    return "/final-v2/" in f"/{str(partition_key).strip('/')}"


def increment_metric(metrics, name, amount=1):
    if metrics is not None and amount:
        metrics[name] = int(metrics.get(name, 0)) + int(amount)


def is_processed_candle_partition(partition_key, rows):
    if "/candles/" not in partition_key or "/interval=" not in partition_key:
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
