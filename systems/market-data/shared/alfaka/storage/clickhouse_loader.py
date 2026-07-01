# 역할: Kafka Processed Topic을 읽어 ClickHouse 조회 테이블에 적재합니다.
# 사용: GOPS API Server가 과거 캔들을 ClickHouse에서 읽을 수 있게 만드는 연결 job입니다.
# 입력: 기본은 closed candles/status/volume profile/news를 적재합니다.
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

from alfaka.alpaca.feed_profiles import market_session_for_timestamp
from alfaka.common.env import load_dotenv, parse_csv
from alfaka.common.kafka_io import create_json_consumer
from alfaka.common.runtime_config import validate_required_values
from alfaka.storage.candle_validation import invalid_candle_reason
from alfaka.storage.processed_s3_archive import archive_processed_candles_to_s3
from alfaka.storage.s3_prefixes import default_s3_archive_prefix, first_configured_prefix


def main():
    load_dotenv()

    kafka_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    group_id = os.getenv("KAFKA_CLICKHOUSE_GROUP_ID", "alfaka-clickhouse-loader")
    topics = parse_csv(os.getenv("KAFKA_CLICKHOUSE_TOPICS", ",".join([
        os.getenv("KAFKA_CLOSED_CANDLE_TOPIC", "market.candles.closed.v1"),
        os.getenv("KAFKA_STATUS_TOPIC", "market.status.v1"),
        os.getenv("KAFKA_VOLUME_PROFILE_BINS_TOPIC", "market.volume-profile-bins.1m.v1"),
        os.getenv("KAFKA_NEWS_TOPIC", "market.news.alpaca.v1"),
    ])))
    enable_auto_commit = os.getenv("KAFKA_CLICKHOUSE_ENABLE_AUTO_COMMIT", "false").lower() in {"1", "true", "yes"}
    validate_required_values("clickhouse loader", {
        "kafka_servers": kafka_servers,
        "clickhouse_topics": topics,
        "clickhouse_url": os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
        "clickhouse_database": os.getenv("CLICKHOUSE_DATABASE", "market_data"),
        "clickhouse_user": os.getenv("CLICKHOUSE_USER", "alfaka"),
    })

    client = ClickHouseHttpClient(
        url=os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
        database=os.getenv("CLICKHOUSE_DATABASE", "market_data"),
        user=os.getenv("CLICKHOUSE_USER", "alfaka"),
        password=os.getenv("CLICKHOUSE_PASSWORD", "alfaka"),
    )
    client.ensure_market_data_schema()
    archive = PostClickHouseCandleArchive.from_env()

    consumer = create_json_consumer(
        topics,
        kafka_servers,
        group_id,
        "alfaka-clickhouse-consumer",
        enable_auto_commit=enable_auto_commit,
    )
    print(f"ClickHouse loader 시작: topics={topics}", flush=True)
    print(f"ClickHouse 연결: {client.url}/{client.database}", flush=True)

    try:
        for record in consumer:
            payload = record.value
            try:
                load_payload(client, payload, archive=archive)
                if not enable_auto_commit:
                    consumer.commit()
                flush_archive_due(archive)
            except Exception as exc:
                print(f"ClickHouse 적재 실패: {exc}; payload={json.dumps(payload, ensure_ascii=False)}", file=sys.stderr, flush=True)
    finally:
        flush_archive(archive)


def load_payload(client, payload, archive=None):
    event_type = payload.get("eventType")
    if event_type == "TRADE":
        print(f"ClickHouse trade 적재 제외: symbol={payload.get('symbol', 'UNKNOWN')} realtime-only", flush=True)
        return

    if event_type == "MARKET_STATUS":
        row = status_to_clickhouse_row(payload)
        client.insert_json_each_row("market_status_events", [row])
        print(f"ClickHouse status 적재: symbol={row['symbol']} status={row['status']} time={row['event_time']}", flush=True)
        return

    if event_type == "VOLUME_PROFILE_BIN":
        row = volume_profile_bin_to_clickhouse_row(payload)
        client.insert_json_each_row("volume_profile_bins_1m", [row])
        print(f"ClickHouse volume profile 적재: symbol={row['symbol']} minute={row['event_minute']}", flush=True)
        return

    if event_type == "SYMBOL_METADATA":
        row = symbol_to_clickhouse_row(payload)
        client.insert_json_each_row("symbols", [row])
        print(f"ClickHouse symbol 적재: symbol={row['symbol']}", flush=True)
        return

    if event_type == "NEWS_ARTICLE":
        row = news_to_clickhouse_row(payload)
        client.insert_json_each_row("news_articles", [row])
        print(f"ClickHouse news 적재: symbol={row['symbol']} article={row['article_id']}", flush=True)
        return

    if event_type == "CANDLE" and payload.get("isClosed", True):
        payload = canonicalize_candle_payload(payload)
        reason = invalid_candle_reason(payload)
        if reason:
            print(
                f"ClickHouse candle 적재 제외: symbol={payload.get('symbol', 'UNKNOWN')} "
                f"interval={payload.get('interval', 'unknown')} reason={reason}",
                flush=True,
            )
            return
        row = candle_to_clickhouse_row(payload)
        client.insert_json_each_row("chart_candles", [row])
        print(f"ClickHouse candle 적재: symbol={row['symbol']} interval={row['interval']} time={row['event_time']}", flush=True)
        archive_loaded_candle(archive, payload)
        return

    print(f"ClickHouse 적재 제외 eventType={event_type}", flush=True)


def archive_loaded_candle(archive, payload):
    if archive is None:
        return
    try:
        archive.archive_candle_payload(payload)
    except Exception as exc:
        print(
            f"ClickHouse post-insert S3 archive 실패: "
            f"symbol={payload.get('symbol', 'UNKNOWN')} interval={payload.get('interval', 'unknown')} error={exc}",
            file=sys.stderr,
            flush=True,
        )


def flush_archive_due(archive):
    if archive is None:
        return
    try:
        archive.flush_due()
    except Exception as exc:
        print(f"ClickHouse post-insert S3 archive flush 실패: error={exc}", file=sys.stderr, flush=True)


def flush_archive(archive):
    if archive is None:
        return
    try:
        archive.flush()
    except Exception as exc:
        print(f"ClickHouse post-insert S3 archive final flush 실패: error={exc}", file=sys.stderr, flush=True)


def candle_to_clickhouse_row(payload):
    ma = payload.get("ma") or {}
    timestamp = canonical_candle_timestamp(payload)
    return {
        "event_time": clickhouse_time(timestamp),
        "symbol": payload.get("symbol", "UNKNOWN"),
        "interval": payload.get("interval", "1m"),
        "open": float_or_zero(payload.get("open")),
        "high": float_or_zero(payload.get("high")),
        "low": float_or_zero(payload.get("low")),
        "close": float_or_zero(payload.get("close")),
        "volume": int_or_zero(payload.get("volume")),
        "trade_count": int_or_none(payload.get("tradeCount")),
        "vwap": float_or_none(payload.get("vwap")),
        "ma5": float_or_none(ma.get("ma5", payload.get("ma5"))),
        "ma20": float_or_none(ma.get("ma20", payload.get("ma20"))),
        "ma60": float_or_none(ma.get("ma60", payload.get("ma60"))),
        "is_closed": bool(payload.get("isClosed", True)),
        "correction_type": payload.get("correctionType", "NONE"),
        "source": payload.get("source", "stream-processor"),
        "feed": payload.get("feed") or "unknown",
        "feed_profile": payload.get("feedProfile") or payload.get("feed") or "unknown",
        "market_session": payload.get("marketSession") or candle_market_session(payload, timestamp),
        "source_event_id": payload.get("sourceEventId"),
        "created_at": clickhouse_time_or_none(payload.get("createdAt") or payload.get("updatedAt")),
    }


def canonicalize_candle_payload(payload):
    timestamp = canonical_candle_timestamp(payload)
    if timestamp == payload.get("timestamp"):
        return payload
    normalized = dict(payload)
    normalized["timestamp"] = timestamp
    return normalized


def canonical_candle_timestamp(payload):
    timestamp = payload.get("timestamp")
    if not timestamp:
        return timestamp
    if str(payload.get("interval", "1m")).upper() != "1D":
        return timestamp
    parsed = parse_time(timestamp).astimezone(timezone.utc)
    return parsed.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def candle_market_session(payload, timestamp):
    if str(payload.get("interval", "1m")).upper() == "1D":
        return "regular"
    return market_session_for_timestamp(timestamp)


def status_to_clickhouse_row(payload):
    symbol = payload.get("symbol")
    return {
        "event_time": clickhouse_time(payload.get("eventTime")),
        "symbol": None if symbol == "_MARKET" else symbol,
        "status_type": payload.get("statusType", "market"),
        "status": str(payload.get("status", "unknown")),
        "reason": payload.get("reason"),
        "source": payload.get("source", "alpaca"),
        "feed": payload.get("feed") or "unknown",
        "feed_profile": payload.get("feedProfile") or payload.get("feed") or "unknown",
        "market_session": payload.get("marketSession") or market_session_for_timestamp(payload.get("eventTime")),
        "source_event_id": payload.get("sourceEventId"),
        "raw": json.dumps(payload.get("raw") or {}, ensure_ascii=False, separators=(",", ":")),
    }


def volume_profile_bin_to_clickhouse_row(payload):
    return {
        "event_minute": clickhouse_time(payload.get("eventMinute")),
        "symbol": payload.get("symbol", "UNKNOWN"),
        "price_bin": float_or_zero(payload.get("priceBin")),
        "price_bin_size": float_or_zero(payload.get("priceBinSize")),
        "volume": int_or_zero(payload.get("volume")),
        "trade_count": int_or_zero(payload.get("tradeCount")),
        "vwap": float_or_none(payload.get("vwap")),
        "source": payload.get("source", "alpaca"),
        "feed": payload.get("feed") or "unknown",
        "feed_profile": payload.get("feedProfile") or payload.get("feed") or "unknown",
        "market_session": payload.get("marketSession") or market_session_for_timestamp(payload.get("eventMinute")),
        "source_event_id": payload.get("sourceEventId"),
        "updated_at": clickhouse_time_or_none(payload.get("updatedAt")),
    }


def symbol_to_clickhouse_row(payload):
    return {
        "symbol": payload.get("symbol", "UNKNOWN"),
        "name": payload.get("name") or payload.get("symbol", "UNKNOWN"),
        "exchange": payload.get("exchange") or payload.get("market"),
        "market": payload.get("market", "US"),
        "asset_class": payload.get("assetClass", "us_equity"),
        "tradable": bool(payload.get("tradable", True)),
        "status": payload.get("status", "unknown"),
        "source": payload.get("source", "alpaca"),
        "updated_at": clickhouse_time(payload.get("updatedAt")),
        "raw": json.dumps(payload.get("raw"), ensure_ascii=False, separators=(",", ":")) if payload.get("raw") is not None else None,
    }


def news_to_clickhouse_row(payload):
    return {
        "published_at": clickhouse_time(payload.get("publishedAt") or payload.get("createdAt") or payload.get("timestamp")),
        "symbol": payload.get("symbol", "UNKNOWN"),
        "article_id": str(payload.get("articleId") or payload.get("sourceEventId") or ""),
        "headline": payload.get("headline") or "Untitled news",
        "summary": payload.get("summary"),
        "content": payload.get("content"),
        "url": payload.get("url"),
        "source": payload.get("source") or "alpaca",
        "author": payload.get("author"),
        "updated_at": clickhouse_time_or_none(payload.get("updatedAt")),
        "received_at": clickhouse_time_or_none(payload.get("receivedAt")),
        "raw": json.dumps(payload.get("raw") or payload, ensure_ascii=False, separators=(",", ":")),
    }


def parse_time(value):
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def clickhouse_time(value):
    return parse_time(value).strftime("%Y-%m-%d %H:%M:%S.%f")[:23]


def clickhouse_time_or_none(value):
    return clickhouse_time(value) if value else None


def int_or_zero(value):
    return int(value or 0)


def int_or_none(value):
    return int(value) if value is not None else None


def float_or_zero(value):
    return float(value or 0)


def float_or_none(value):
    return float(value) if value is not None else None


class ClickHouseHttpClient:
    def __init__(self, url, database, user, password):
        self.url = url.rstrip("/")
        self.database = clickhouse_identifier(database)
        self.user = user
        self.password = password

    def insert_json_each_row(self, table, rows):
        if not rows:
            return

        import requests

        query = f"INSERT INTO {self.database}.{clickhouse_identifier(table)} FORMAT JSONEachRow"
        body = "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows) + "\n"
        response = requests.post(
            self.url,
            params={"user": self.user, "password": self.password, "database": self.database, "query": query},
            data=body.encode("utf-8"),
            timeout=10,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"status={response.status_code}, body={response.text}")

    def execute(self, query):
        import requests

        response = requests.post(
            self.url,
            params={"user": self.user, "password": self.password, "database": self.database, "query": query},
            timeout=10,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"status={response.status_code}, body={response.text}")

    def ensure_market_data_schema(self):
        if os.getenv("CLICKHOUSE_ENSURE_SESSION_COLUMNS", "true").lower() not in {"1", "true", "yes"}:
            return
        for table, after_column in (
            ("chart_candles", "feed"),
            ("volume_profile_bins_1m", "feed"),
            ("market_status_events", "feed"),
        ):
            table_name = f"{self.database}.{clickhouse_identifier(table)}"
            self.execute(
                f"ALTER TABLE {table_name} "
                f"ADD COLUMN IF NOT EXISTS feed_profile LowCardinality(String) DEFAULT feed AFTER {after_column}, "
                "ADD COLUMN IF NOT EXISTS market_session LowCardinality(String) DEFAULT 'unknown' AFTER feed_profile"
            )


def clickhouse_identifier(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(value)):
        raise ValueError(f"Invalid ClickHouse identifier: {value}")
    return str(value)


class PostClickHouseCandleArchive:
    def __init__(
        self,
        *,
        bucket,
        prefix,
        output_format,
        manifest_prefix,
        manifest_layout,
        max_attempts,
        retry_sleep_seconds,
        rows_per_object,
        flush_rows,
        flush_interval_seconds,
        s3=None,
    ):
        self.s3 = s3
        self.bucket = bucket
        self.prefix = prefix
        self.output_format = output_format
        self.manifest_prefix = manifest_prefix
        self.manifest_layout = manifest_layout
        self.max_attempts = max_attempts
        self.retry_sleep_seconds = retry_sleep_seconds
        self.rows_per_object = rows_per_object
        self.flush_rows = flush_rows
        self.flush_interval_seconds = flush_interval_seconds
        self.buffered_rows = []
        self.buffer_started_at = None

    @classmethod
    def from_env(cls, environ=None):
        environ = environ or os.environ
        if str(environ.get("CLICKHOUSE_LOADER_S3_ARCHIVE_ENABLED", "true")).strip().lower() not in {"1", "true", "yes", "y", "on"}:
            print("ClickHouse post-insert S3 archive 비활성화: CLICKHOUSE_LOADER_S3_ARCHIVE_ENABLED=false", flush=True)
            return None
        bucket = environ.get("S3_BUCKET")
        if not bucket:
            print("ClickHouse post-insert S3 archive 비활성화: S3_BUCKET not configured", flush=True)
            return None
        return cls(
            bucket=bucket,
            prefix=first_configured_prefix(["S3_FINAL_PREFIX"], default_s3_archive_prefix("final", environ), environ),
            output_format=str(environ.get("S3_PROCESSED_FORMAT", "parquet")).strip().lower(),
            manifest_prefix=first_configured_prefix(["S3_MANIFEST_PREFIX"], default_s3_archive_prefix("manifest", environ), environ),
            manifest_layout=str(environ.get("S3_PROCESSED_MANIFEST_LAYOUT", "daily")).strip() or "daily",
            max_attempts=parse_positive_int(environ.get("S3_PUT_MAX_ATTEMPTS", "3"), 3),
            retry_sleep_seconds=parse_non_negative_float(environ.get("S3_PUT_RETRY_SLEEP_SECONDS", "1"), 1),
            rows_per_object=parse_positive_int(environ.get("S3_CLICKHOUSE_ARCHIVE_ROWS_PER_OBJECT", "10000"), 10000),
            flush_rows=parse_positive_int(environ.get("S3_CLICKHOUSE_ARCHIVE_FLUSH_ROWS", "1000"), 1000),
            flush_interval_seconds=parse_non_negative_float(environ.get("S3_CLICKHOUSE_ARCHIVE_FLUSH_SECONDS", "60"), 60),
        )

    def archive_candle_payload(self, payload):
        if not self.buffered_rows:
            self.buffer_started_at = datetime.now(timezone.utc)
        self.buffered_rows.append(dict(payload))
        if len(self.buffered_rows) >= self.flush_rows:
            return self.flush()
        return {"archiveStatus": "buffered", "archiveBufferedRowCount": len(self.buffered_rows)}

    def flush_due(self, now=None):
        if not self.buffered_rows or self.flush_interval_seconds <= 0:
            return {"archiveStatus": "skipped", "archiveReason": "not due"}
        now = now or datetime.now(timezone.utc)
        started_at = self.buffer_started_at or now
        if (now - started_at).total_seconds() < self.flush_interval_seconds:
            return {"archiveStatus": "skipped", "archiveReason": "not due"}
        return self.flush()

    def flush(self):
        if not self.buffered_rows:
            return {"archiveStatus": "skipped", "archiveReason": "empty buffer"}
        if self.s3 is None:
            from alfaka.common.s3_client import create_s3_client
            self.s3 = create_s3_client()
        rows = self.buffered_rows
        self.buffered_rows = []
        self.buffer_started_at = None
        result = archive_processed_candles_to_s3(
            self.s3,
            self.bucket,
            self.prefix,
            rows,
            output_format=self.output_format,
            manifest_prefix=self.manifest_prefix,
            manifest_layout=self.manifest_layout,
            max_attempts=self.max_attempts,
            retry_sleep_seconds=self.retry_sleep_seconds,
            rows_per_object=self.rows_per_object,
        )
        summary = summarize_archive_rows(rows)
        print(
            f"ClickHouse post-insert S3 archive: "
            f"rows={result['rowCount']} objects={result['objectCount']} groups={summary}",
            flush=True,
        )
        return result


def summarize_archive_rows(rows, max_groups=8):
    groups = defaultdict(int)
    for row in rows:
        groups[(row.get("symbol", "UNKNOWN"), row.get("interval", "unknown"))] += 1
    items = sorted(groups.items())
    visible = items[:max_groups]
    summary = ",".join(f"{symbol}:{interval}:{count}" for (symbol, interval), count in visible)
    remaining = len(items) - len(visible)
    if remaining > 0:
        summary = f"{summary},+{remaining} groups"
    return summary


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
