"""Kafka raw Alpaca envelope archive sink."""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

from market_data.common.env import load_dotenv, parse_csv
from market_data.common.kafka_io import create_json_consumer
from market_data.common.runtime_config import validate_required_values
from market_data.storage.processed_s3_sink import (
    flush_all_buffers,
    flush_due_buffers,
    parse_non_negative_float,
    parse_boolean,
    parse_positive_int,
    put_object_with_retry,
    increment_metric,
    normalize_realtime_layout_mode,
    s3_object_exists,
)
from market_data.storage.s3_manifest import DEFAULT_MANIFEST_PREFIX, normalize_raw_channel, write_raw_manifest
from market_data.storage.s3_realtime_layout import (
    canonical_rows_with_duplicate_count,
    deterministic_realtime_object_key,
    partition_key_from_buffer_identity,
    raw_v2_partition_key,
    realtime_buffer_identity,
)


RAW_ARCHIVE_TOPICS = (
    "market.input.realtime.events.v1",
    "market.input.realtime.bars.1m.v1",
    "market.input.realtime.updated-bars.1m.v1",
    "market.input.realtime.daily-bars.v1",
)


def main():
    load_dotenv()
    config = raw_s3_archive_runtime_config()
    start_raw_s3_archive_sink(config)


def start_raw_s3_archive_sink(config, consumer_factory=create_json_consumer, s3_factory=None, sink_runner=None):
    if s3_factory is None:
        from market_data.common.s3_client import create_s3_client

        s3_factory = create_s3_client
    sink_runner = sink_runner or run_raw_s3_archive_sink
    consumer = consumer_factory(
        config["topics"],
        config["kafka_servers"],
        config["group_id"],
        "alfaka-raw-s3-archive-consumer",
        enable_auto_commit=config["enable_auto_commit"],
    )
    s3 = s3_factory()
    print(f"Raw S3 archive 시작: topics={config['topics']}", flush=True)
    print(f"Raw S3 archive 위치: s3://{config['s3_bucket']}/{config['raw_prefix']}", flush=True)
    sink_runner(consumer, s3, **config)


def raw_s3_archive_runtime_config(environ=None):
    environ = os.environ if environ is None else environ
    kafka_servers = environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    configured_topics = parse_csv(environ.get("KAFKA_RAW_ARCHIVE_TOPICS", ""))
    topics = configured_topics or list(RAW_ARCHIVE_TOPICS)
    config = {
        "kafka_servers": kafka_servers,
        "group_id": environ.get("KAFKA_RAW_S3_GROUP_ID", "alfaka-raw-s3-archive"),
        "topics": topics,
        "s3_bucket": environ.get("S3_BUCKET"),
        "raw_prefix": environ.get("S3_RAW_PREFIX", "market-data/rebuild-20260702-lazy-v1/raw/alpaca"),
        "manifest_prefix": environ.get("S3_MANIFEST_PREFIX", DEFAULT_MANIFEST_PREFIX),
        "flush_count": parse_positive_int(environ.get("S3_RAW_FLUSH_COUNT", environ.get("S3_FLUSH_COUNT", "500")), default=500),
        "flush_interval_seconds": parse_non_negative_float(environ.get("S3_RAW_FLUSH_INTERVAL_SECONDS", environ.get("S3_FLUSH_INTERVAL_SECONDS", "60")), default=60),
        "poll_timeout_ms": parse_positive_int(environ.get("S3_SINK_POLL_TIMEOUT_MS", "1000"), default=1000),
        "put_max_attempts": parse_positive_int(environ.get("S3_PUT_MAX_ATTEMPTS", "3"), default=3),
        "put_retry_sleep_seconds": parse_non_negative_float(environ.get("S3_PUT_RETRY_SLEEP_SECONDS", "1"), default=1),
        "enable_auto_commit": parse_boolean(environ.get("KAFKA_RAW_S3_ENABLE_AUTO_COMMIT", "false")),
        "realtime_layout_mode": normalize_realtime_layout_mode(environ.get("S3_REALTIME_LAYOUT_MODE", "v2")),
    }
    validate_required_values("raw s3 archive", {
        "kafka_servers": config["kafka_servers"],
        "group_id": config["group_id"],
        "topics": config["topics"],
        "s3_bucket": config["s3_bucket"],
        "raw_prefix": config["raw_prefix"],
    })
    return config


def run_raw_s3_archive_sink(
    consumer,
    s3,
    kafka_servers=None,
    group_id=None,
    topics=None,
    s3_bucket=None,
    raw_prefix="market-data/rebuild-20260702-lazy-v1/raw/alpaca",
    manifest_prefix=DEFAULT_MANIFEST_PREFIX,
    flush_count=500,
    flush_interval_seconds=60,
    poll_timeout_ms=1000,
    put_max_attempts=3,
    put_retry_sleep_seconds=1,
    now_fn=None,
    enable_auto_commit=False,
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
                    simulation = record.value.get("simulation") if isinstance(record.value, dict) else None
                    if isinstance(simulation, dict) and simulation.get("source") == "gops-simulator":
                        increment_metric(metrics, "simulationRowsSkipped", 1)
                        continue
                    archive_row = raw_archive_row(record.value, now_fn=now_fn)
                    for partition_key in raw_realtime_partition_keys(raw_prefix, archive_row, realtime_layout_mode):
                        buffer_key = realtime_buffer_identity(partition_key, archive_row)
                        buffers[buffer_key].append(archive_row)
                        last_updated_at[buffer_key] = now_fn()
                        if len(buffers[buffer_key]) >= flush_count:
                            flush_raw_buffer(
                                s3,
                                s3_bucket,
                                partition_key,
                                buffers[buffer_key],
                                manifest_prefix=manifest_prefix,
                                max_attempts=put_max_attempts,
                                retry_sleep_seconds=put_retry_sleep_seconds,
                                metrics=metrics,
                            )
                            buffers[buffer_key].clear()
                            last_updated_at.pop(buffer_key, None)
            flush_due_buffers(
                buffers,
                last_updated_at,
                lambda buffer_key, rows: flush_raw_buffer(
                    s3,
                    s3_bucket,
                    partition_key_from_buffer_identity(buffer_key),
                    rows,
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
        print("Raw S3 archive 종료 신호 수신: 남은 buffer를 flush합니다.", flush=True)
    except Exception:
        failed = True
        raise
    finally:
        if not failed:
            flush_all_buffers(
                buffers,
                last_updated_at,
                lambda buffer_key, rows: flush_raw_buffer(
                    s3,
                    s3_bucket,
                    partition_key_from_buffer_identity(buffer_key),
                    rows,
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


def flush_raw_buffer(s3, bucket, partition_key, rows, manifest_prefix=None, max_attempts=3, retry_sleep_seconds=1, metrics=None):
    now = datetime.now(timezone.utc)
    storage_rows = list(rows)
    if is_raw_v2_partition(partition_key):
        storage_rows, duplicate_count = canonical_rows_with_duplicate_count(storage_rows)
        increment_metric(metrics, "duplicateRows", duplicate_count)
        object_key = deterministic_realtime_object_key(partition_key, storage_rows, "jsonl")
        if s3_object_exists(s3, bucket, object_key):
            increment_metric(metrics, "exactReplaySkips", 1)
            print(f"S3 raw canonical 중복 업로드 skip: s3://{bucket}/{object_key} rows={len(rows)}", flush=True)
            return object_key
    else:
        object_key = f"{partition_key}/part-{now:%Y%m%dT%H%M%S%f}.jsonl"
    body = ("\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in storage_rows) + "\n").encode("utf-8")
    put_object_with_retry(
        s3,
        {"Bucket": bucket, "Key": object_key, "Body": body, "ContentType": "application/x-ndjson"},
        max_attempts=max_attempts,
        retry_sleep_seconds=retry_sleep_seconds,
        metrics=metrics,
    )
    increment_metric(metrics, "objects", 1)
    increment_metric(metrics, "rows", len(storage_rows))
    if manifest_prefix and not is_raw_v2_partition(partition_key):
        write_raw_manifest(
            s3,
            bucket,
            manifest_prefix,
            object_key,
            rows,
            put_object=lambda **kwargs: put_object_with_retry(
                s3,
                kwargs,
                max_attempts=max_attempts,
                retry_sleep_seconds=retry_sleep_seconds,
                metrics=metrics,
            ),
        )
    print(f"S3 raw archive upload: s3://{bucket}/{object_key} rows={len(rows)}", flush=True)
    return object_key


def raw_realtime_partition_keys(raw_prefix, archive_row, mode="v1"):
    normalized_mode = normalize_realtime_layout_mode(mode)
    keys = []
    if normalized_mode in {"v1", "dual"}:
        keys.append(raw_envelope_partition_key(raw_prefix, archive_row))
    if normalized_mode in {"v2", "dual"}:
        keys.append(raw_v2_partition_key(raw_prefix, archive_row))
    return keys


def is_raw_v2_partition(partition_key):
    return "/raw-v2/" in f"/{str(partition_key).strip('/')}"


def raw_archive_row(envelope, now_fn=None):
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    raw = envelope.get("raw") or {}
    received_at = envelope.get("receivedAt") or to_iso(now_fn())
    event_time = envelope.get("eventTime") or raw.get("t") or received_at
    row = {
        "source": envelope.get("source") or "alpaca",
        "feed": envelope.get("feed") or "unknown",
        "feedProfile": envelope.get("feedProfile") or envelope.get("feed") or "unknown",
        "marketSession": envelope.get("marketSession") or "unknown",
        "channel": normalize_raw_channel(envelope.get("channel")),
        "symbol": envelope.get("symbol") or raw.get("S") or "_MARKET",
        "eventTime": event_time,
        "receivedAt": received_at,
        "sourceEventId": envelope.get("sourceEventId"),
        "raw": raw,
    }
    if envelope.get("priceAdjustment") or envelope.get("canonicalVersion"):
        row["priceAdjustment"] = envelope.get("priceAdjustment")
        row["canonicalVersion"] = envelope.get("canonicalVersion")
    return row


def raw_envelope_partition_key(prefix, archive_row):
    event_time = parse_event_time(archive_row.get("eventTime") or archive_row.get("receivedAt"))
    channel = normalize_raw_channel(archive_row.get("channel"))
    symbol = archive_row.get("symbol") or "_MARKET"
    return (
        f"{prefix.strip('/')}/source=alpaca/channel={channel}/symbol={symbol}"
        f"/year={event_time:%Y}/month={event_time:%m}/day={event_time:%d}"
    )


def parse_event_time(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def to_iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
