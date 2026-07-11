# 역할: Kafka Processed Topic을 읽어 ClickHouse 조회 테이블에 적재합니다.
# 사용: GOPS API Server가 과거 캔들을 ClickHouse에서 읽을 수 있게 만드는 연결 job입니다.
# 입력: closed candle/trades/quotes/events layer topic을 적재합니다.
import hashlib
import json
import os
import re
import sys
import time
from collections import deque
from datetime import datetime, timezone

from alfaka.alpaca.feed_profiles import market_session_for_timestamp
from alfaka.common.canonical import candle_metadata
from alfaka.common.env import load_dotenv, parse_csv
from alfaka.common.kafka_io import create_json_consumer
from alfaka.common.kafka_topics import closed_candle_topic_values
from alfaka.common.runtime_config import validate_required_values
from alfaka.common.symbols import is_crypto_symbol
from alfaka.storage.candle_validation import invalid_candle_reason


def main():
    load_dotenv()

    kafka_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    group_id = os.getenv("KAFKA_CLICKHOUSE_GROUP_ID", "alfaka-clickhouse-loader")
    client_id = os.getenv("KAFKA_CLICKHOUSE_CLIENT_ID", "alfaka-clickhouse-consumer")
    load_trades = os.getenv("CLICKHOUSE_LOAD_TRADES", "true").lower() in {"1", "true", "yes"}
    load_quotes = os.getenv("CLICKHOUSE_LOAD_QUOTES", "true").lower() in {"1", "true", "yes"}
    topics = clickhouse_topics_from_env(os.environ, load_trades=load_trades, load_quotes=load_quotes)
    enable_auto_commit = os.getenv("KAFKA_CLICKHOUSE_ENABLE_AUTO_COMMIT", "false").lower() in {"1", "true", "yes"}
    batch_size = positive_int_env("CLICKHOUSE_INSERT_BATCH_SIZE", 500)
    max_poll_records = positive_int_env("KAFKA_CLICKHOUSE_MAX_POLL_RECORDS", batch_size)
    poll_timeout_ms = positive_int_env("KAFKA_CLICKHOUSE_POLL_TIMEOUT_MS", 1000)
    flush_interval_seconds = non_negative_float_env("CLICKHOUSE_FLUSH_INTERVAL_SECONDS", 1.0)
    recent_source_event_ids = positive_int_env("CLICKHOUSE_RECENT_SOURCE_EVENT_IDS", 100000)
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
    if should_ensure_schema_on_start():
        client.ensure_market_data_schema()

    consumer = create_json_consumer(
        topics,
        kafka_servers,
        group_id,
        client_id,
        enable_auto_commit=enable_auto_commit,
        max_poll_records=max_poll_records,
    )
    print(f"ClickHouse loader 시작: topics={topics}", flush=True)
    print(f"ClickHouse 연결: {client.url}/{client.database}", flush=True)
    print(
        "ClickHouse loader batch config: "
        f"batchSize={batch_size} maxPollRecords={max_poll_records} "
        f"flushIntervalSeconds={flush_interval_seconds} recentSourceEventIds={recent_source_event_ids}",
        flush=True,
    )

    run_clickhouse_loader(
        consumer,
        client,
        load_trades=load_trades,
        load_quotes=load_quotes,
        enable_auto_commit=enable_auto_commit,
        batch_size=batch_size,
        flush_interval_seconds=flush_interval_seconds,
        poll_timeout_ms=poll_timeout_ms,
        recent_source_ids=RecentSourceEventIds(recent_source_event_ids),
    )


def run_clickhouse_loader(
    consumer,
    client,
    *,
    load_trades=False,
    load_quotes=True,
    enable_auto_commit=False,
    batch_size=500,
    flush_interval_seconds=1.0,
    poll_timeout_ms=1000,
    recent_source_ids=None,
):
    batch_size = max(1, int(batch_size or 1))
    flush_interval_seconds = max(0.0, float(flush_interval_seconds or 0))
    buffer = []
    last_flush_at = time.monotonic()
    try:
        while True:
            batches = consumer.poll(timeout_ms=poll_timeout_ms)
            now = time.monotonic()
            had_records = False
            for records in batches.values():
                for record in records:
                    had_records = True
                    buffer.append(record)
                    if len(buffer) >= batch_size:
                        flush_clickhouse_buffer(
                            consumer,
                            client,
                            buffer,
                            load_trades=load_trades,
                            load_quotes=load_quotes,
                            enable_auto_commit=enable_auto_commit,
                            recent_source_ids=recent_source_ids,
                        )
                        buffer.clear()
                        last_flush_at = now
            if buffer and (not had_records or now - last_flush_at >= flush_interval_seconds):
                flush_clickhouse_buffer(
                    consumer,
                    client,
                    buffer,
                    load_trades=load_trades,
                    load_quotes=load_quotes,
                    enable_auto_commit=enable_auto_commit,
                    recent_source_ids=recent_source_ids,
                )
                buffer.clear()
                last_flush_at = now
    except KeyboardInterrupt:
        if buffer:
            flush_clickhouse_buffer(
                consumer,
                client,
                buffer,
                load_trades=load_trades,
                load_quotes=load_quotes,
                enable_auto_commit=enable_auto_commit,
                recent_source_ids=recent_source_ids,
            )


def flush_clickhouse_buffer(
    consumer,
    client,
    records,
    *,
    load_trades=False,
    load_quotes=True,
    enable_auto_commit=False,
    recent_source_ids=None,
):
    if not records:
        return 0
    inserted = load_payload_batch(
        client,
        records,
        load_trades=load_trades,
        load_quotes=load_quotes,
        recent_source_ids=recent_source_ids,
    )
    if not enable_auto_commit:
        commit_processed_records(consumer, records)
    return inserted


def clickhouse_topics_from_env(environ=None, load_trades=False, load_quotes=True):
    environ = environ or os.environ
    default_topics = [
        *closed_candle_topic_values(environ),
        environ.get("KAFKA_TRADES_LAYER_TOPIC", "market.layer.trades.v1"),
        environ.get("KAFKA_EVENTS_LAYER_TOPIC", "market.layer.events.v1"),
        environ.get("KAFKA_NEWS_TOPIC", "market.news.alpaca.v1"),
    ]
    if load_quotes:
        default_topics.insert(2, environ.get("KAFKA_QUOTES_LAYER_TOPIC", "market.layer.quotes.v1"))
    topics = parse_csv(environ.get("KAFKA_CLICKHOUSE_TOPICS", ",".join(default_topics)))
    quotes_topic = environ.get("KAFKA_QUOTES_LAYER_TOPIC", "market.layer.quotes.v1")
    if load_quotes and quotes_topic not in topics:
        topics.append(quotes_topic)
    trades_topic = environ.get("KAFKA_TRADES_LAYER_TOPIC", "market.layer.trades.v1")
    if load_trades and trades_topic not in topics:
        topics.append(trades_topic)
    return topics


def positive_int_env(name, default):
    value = os.getenv(name)
    if value in {None, ""}:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def non_negative_float_env(name, default):
    value = os.getenv(name)
    if value in {None, ""}:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def load_payload(client, payload, load_trades=False, load_quotes=True):
    actions = clickhouse_actions_for_payload(payload, load_trades=load_trades, load_quotes=load_quotes)
    for table, row, message in actions:
        client.insert_json_each_row(table, [row])
        print(message, flush=True)


class RecentSourceEventIds:
    def __init__(self, max_seen=100000):
        self.max_seen = max(1, int(max_seen or 1))
        self.seen = set()
        self.order = deque()

    def contains(self, table, source_event_id):
        return bool(source_event_id) and (table, source_event_id) in self.seen

    def record_many(self, table, source_event_ids):
        for source_event_id in source_event_ids:
            if not source_event_id:
                continue
            key = (table, source_event_id)
            if key in self.seen:
                continue
            self.seen.add(key)
            self.order.append(key)
            if len(self.order) > self.max_seen:
                self.seen.discard(self.order.popleft())


def load_payload_batch(client, records, load_trades=False, load_quotes=True, recent_source_ids=None):
    table_entries = {}
    table_order = []
    skipped = 0
    deduplicated = 0
    batch_source_ids = {}
    for record in records:
        try:
            payload = clickhouse_record_payload(record)
            actions = clickhouse_actions_for_payload(payload, load_trades=load_trades, load_quotes=load_quotes)
        except Exception as exc:
            skipped += 1
            raw_payload = getattr(record, "value", record)
            print(f"ClickHouse batch row 변환 실패: {exc}; payload={json.dumps(raw_payload, ensure_ascii=False)}", file=sys.stderr, flush=True)
            continue
        if not actions:
            skipped += 1
            continue
        for table, row, _message in actions:
            source_event_id = str(row.get("source_event_id") or "")
            table_seen = batch_source_ids.setdefault(table, set())
            if source_event_id and (
                source_event_id in table_seen
                or (recent_source_ids is not None and recent_source_ids.contains(table, source_event_id))
            ):
                deduplicated += 1
                continue
            if source_event_id:
                table_seen.add(source_event_id)
            if table not in table_entries:
                table_entries[table] = []
                table_order.append(table)
            table_entries[table].append((row, source_event_id, clickhouse_record_identity(record, payload)))

    inserted = 0
    for table in table_order:
        entries = table_entries[table]
        rows = [entry[0] for entry in entries]
        token = clickhouse_insert_deduplication_token(table, [entry[2] for entry in entries])
        client.insert_json_each_row(table, rows, deduplication_token=token)
        if recent_source_ids is not None:
            recent_source_ids.record_many(table, [entry[1] for entry in entries])
        inserted += len(rows)
        print(f"ClickHouse batch 적재: table={table} rows={len(rows)}", flush=True)
    if skipped:
        print(f"ClickHouse batch 제외: rows={skipped}", flush=True)
    if deduplicated:
        print(f"ClickHouse batch 중복 제외: rows={deduplicated}", flush=True)
    return inserted


def clickhouse_record_payload(record):
    value = getattr(record, "value", record)
    if not isinstance(value, dict):
        raise TypeError(f"ClickHouse payload must be a dict, got {type(value).__name__}")
    return value


def clickhouse_record_identity(record, payload):
    topic = getattr(record, "topic", None)
    partition = getattr(record, "partition", None)
    offset = getattr(record, "offset", None)
    if topic is not None and partition is not None and offset is not None:
        return f"kafka/{topic}/{partition}/{offset}"
    source_event_id = payload.get("sourceEventId") or payload.get("source_event_id")
    if source_event_id:
        return f"source/{source_event_id}"
    return ""


def clickhouse_insert_deduplication_token(table, identities):
    identity_values = list(identities)
    stable_identities = sorted(identity for identity in identity_values if identity)
    if not stable_identities or len(stable_identities) != len(identity_values):
        return None
    digest = hashlib.sha256((table + "\n" + "\n".join(stable_identities)).encode("utf-8")).hexdigest()
    return f"gops-kafka-v1-{digest}"


def commit_processed_records(consumer, records):
    offsets = processed_record_offsets(records)
    if not offsets:
        consumer.commit()
        return

    from kafka import OffsetAndMetadata, TopicPartition

    consumer.commit(offsets={
        TopicPartition(topic, partition): OffsetAndMetadata(offset, None)
        for (topic, partition), offset in offsets.items()
    })


def processed_record_offsets(records):
    offsets = {}
    for record in records:
        topic = getattr(record, "topic", None)
        partition = getattr(record, "partition", None)
        offset = getattr(record, "offset", None)
        if topic is None or partition is None or offset is None:
            continue
        key = (str(topic), int(partition))
        offsets[key] = max(offsets.get(key, 0), int(offset) + 1)
    return offsets


def clickhouse_actions_for_payload(payload, load_trades=False, load_quotes=True):
    event_type = payload.get("eventType")
    if event_type == "QUOTE" or payload.get("layer") == "quotes":
        if not load_quotes:
            print(f"ClickHouse quote 적재 제외: symbol={payload.get('symbol', 'UNKNOWN')}", flush=True)
            return []
        row = quote_to_clickhouse_row(payload)
        return [("quote_ticks", row, f"ClickHouse quote 적재: symbol={row['symbol']} time={row['event_time']}")]
    if event_type == "TRADE":
        if not load_trades:
            print(f"ClickHouse trade 적재 제외: symbol={payload.get('symbol', 'UNKNOWN')}", flush=True)
            return []
        row = trade_to_clickhouse_row(payload)
        return [("trade_ticks", row, f"ClickHouse trade 적재: symbol={row['symbol']} time={row['event_time']}")]

    if event_type == "MARKET_STATUS":
        event_row = market_event_to_clickhouse_row(payload)
        row = status_to_clickhouse_row(payload)
        return [
            ("market_events", event_row, f"ClickHouse event 적재: symbol={event_row['symbol']} type={event_row['event_type']} time={event_row['event_time']}"),
            ("market_status_events", row, f"ClickHouse status 적재: symbol={row['symbol']} status={row['status']} time={row['event_time']}"),
        ]

    if event_type == "SYMBOL_METADATA":
        row = symbol_to_clickhouse_row(payload)
        return [("symbols", row, f"ClickHouse symbol 적재: symbol={row['symbol']}")]

    if event_type == "NEWS_ARTICLE":
        row = news_to_clickhouse_row(payload)
        return [("news_articles", row, f"ClickHouse news 적재: symbol={row['symbol']} article={row['article_id']}")]

    if event_type == "CANDLE" and payload.get("isClosed", True):
        reason = invalid_candle_reason(payload)
        if reason:
            print(
                f"ClickHouse candle 적재 제외: symbol={payload.get('symbol', 'UNKNOWN')} "
                f"interval={payload.get('interval', 'unknown')} reason={reason}",
                flush=True,
            )
            return []
        row = candle_to_clickhouse_row(payload)
        return [("chart_candles", row, f"ClickHouse candle 적재: symbol={row['symbol']} interval={row['interval']} time={row['event_time']}")]

    if payload.get("layer") == "events" or event_type:
        row = market_event_to_clickhouse_row(payload)
        return [("market_events", row, f"ClickHouse event 적재: symbol={row['symbol']} type={row['event_type']} time={row['event_time']}")]

    print(f"ClickHouse 적재 제외 eventType={event_type}", flush=True)
    return []


def trade_to_clickhouse_row(payload):
    """processed trade payload를 trade_ticks 테이블 row로 변환합니다."""
    return {
        "event_time": clickhouse_time(payload.get("timestamp")),
        "symbol": payload.get("symbol", "UNKNOWN"),
        "trade_id": int_or_zero(payload.get("tradeId")),
        "price": float_or_zero(payload.get("price")),
        "size": float_or_none(payload.get("size")),
        "exchange": payload.get("exchange"),
        "conditions": payload.get("conditions") or [],
        "tape": payload.get("tape"),
        "source": payload.get("source", "alpaca"),
        "feed": payload.get("feed") or "unknown",
        "feed_profile": payload.get("feedProfile") or payload.get("feed") or "unknown",
        "market_session": payload.get("marketSession") or market_session_for_symbol(payload.get("symbol"), payload.get("timestamp")),
        "source_event_id": payload.get("sourceEventId"),
        "received_at": clickhouse_time_or_none(payload.get("receivedAt")),
    }


def quote_to_clickhouse_row(payload):
    """processed quote payload를 quote_ticks 테이블 row로 변환합니다."""
    return {
        "event_time": clickhouse_time(payload.get("timestamp")),
        "symbol": payload.get("symbol", "UNKNOWN"),
        "bid_price": float_or_none(payload.get("bidPrice")),
        "bid_size": float_or_none(payload.get("bidSize")),
        "ask_price": float_or_none(payload.get("askPrice")),
        "ask_size": float_or_none(payload.get("askSize")),
        "bid_exchange": payload.get("bidExchange"),
        "ask_exchange": payload.get("askExchange"),
        "conditions": payload.get("conditions") or [],
        "source": payload.get("source", "alpaca.quotes"),
        "feed": payload.get("feed") or "unknown",
        "feed_profile": payload.get("feedProfile") or payload.get("feed") or "unknown",
        "market_session": payload.get("marketSession") or market_session_for_symbol(payload.get("symbol"), payload.get("timestamp")),
        "source_event_id": payload.get("sourceEventId"),
        "received_at": clickhouse_time_or_none(payload.get("receivedAt")),
    }


def candle_to_clickhouse_row(payload):
    """processed candle payload를 chart_candles 테이블 row로 변환합니다."""
    ma = payload.get("ma") or {}
    metadata = candle_metadata(payload.get("priceAdjustment") or payload.get("price_adjustment"), payload.get("canonicalVersion") or payload.get("canonical_version"))
    return {
        "event_time": clickhouse_time(payload.get("timestamp")),
        "symbol": payload.get("symbol", "UNKNOWN"),
        "interval": payload.get("interval", "1m"),
        "open": float_or_zero(payload.get("open")),
        "high": float_or_zero(payload.get("high")),
        "low": float_or_zero(payload.get("low")),
        "close": float_or_zero(payload.get("close")),
        "volume": float_or_zero(payload.get("volume")),
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
        "market_session": payload.get("marketSession") or market_session_for_symbol(payload.get("symbol"), payload.get("timestamp")),
        "price_adjustment": metadata["priceAdjustment"],
        "canonical_version": metadata["canonicalVersion"],
        "source_event_id": payload.get("sourceEventId"),
        "created_at": clickhouse_time_or_none(payload.get("createdAt") or payload.get("updatedAt")),
    }


def status_to_clickhouse_row(payload):
    """Alpaca market status payload를 market_status_events 테이블 row로 변환합니다."""
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
        "market_session": payload.get("marketSession") or market_session_for_symbol(symbol, payload.get("eventTime")),
        "source_event_id": payload.get("sourceEventId"),
        "raw": json.dumps(payload.get("raw") or {}, ensure_ascii=False, separators=(",", ":")),
    }


def market_event_to_clickhouse_row(payload):
    """기타 market event payload를 market_events 테이블 row로 변환합니다."""
    symbol = payload.get("symbol")
    event_time = payload.get("eventTime") or payload.get("timestamp") or payload.get("receivedAt")
    return {
        "event_time": clickhouse_time(event_time),
        "symbol": None if symbol in {None, "_MARKET"} else symbol,
        "event_type": str(payload.get("eventType") or payload.get("type") or "UNKNOWN"),
        "layer": str(payload.get("layer") or "events"),
        "source": payload.get("source", "alpaca"),
        "feed": payload.get("feed") or "unknown",
        "feed_profile": payload.get("feedProfile") or payload.get("feed") or "unknown",
        "market_session": payload.get("marketSession") or market_session_for_symbol(symbol, event_time),
        "source_event_id": payload.get("sourceEventId"),
        "payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
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


def clickhouse_param_value(value):
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(clickhouse_array_param_item_value(item) for item in value) + "]"
    if isinstance(value, bool):
        return "1" if value else "0"
    if value is None:
        return "NULL"
    return str(value)


def clickhouse_array_param_item_value(value):
    if isinstance(value, str):
        return clickhouse_string_literal(value)
    if isinstance(value, bool):
        return "1" if value else "0"
    if value is None:
        return "NULL"
    return str(value)


def int_or_zero(value):
    return int(value or 0)


def int_or_none(value):
    return int(value) if value is not None else None


def float_or_zero(value):
    return float(value or 0)


def float_or_none(value):
    return float(value) if value is not None else None


def market_session_for_symbol(symbol, timestamp):
    """crypto는 항상 crypto 세션으로, 주식은 timestamp 기반 장 세션으로 분류합니다."""
    return "crypto" if is_crypto_symbol(symbol) else market_session_for_timestamp(timestamp)


class ClickHouseHttpClient:
    def __init__(self, url, database, user, password):
        self.url = url.rstrip("/")
        self.database = clickhouse_identifier(database)
        self.user = user
        self.password = password
        self.timeout_seconds = float(os.getenv("CLICKHOUSE_HTTP_TIMEOUT_SECONDS", "10"))

    def insert_json_each_row(self, table, rows, deduplication_token=None):
        if not rows:
            return

        import requests

        query = f"INSERT INTO {self.database}.{clickhouse_identifier(table)} FORMAT JSONEachRow"
        body = "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows) + "\n"
        params = {"user": self.user, "password": self.password, "database": self.database, "query": query}
        if deduplication_token:
            params["insert_deduplicate"] = "1"
            params["insert_deduplication_token"] = deduplication_token
        response = requests.post(
            self.url,
            params=params,
            data=body.encode("utf-8"),
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"status={response.status_code}, body={response.text}")

    def execute(self, query, parameters=None):
        import requests

        params = {"user": self.user, "password": self.password, "database": self.database, "query": query}
        for key, value in (parameters or {}).items():
            params[f"param_{key}"] = clickhouse_param_value(value)
        response = requests.post(
            self.url,
            params=params,
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"status={response.status_code}, body={response.text}")

    def query_json_each_row(self, query, parameters=None):
        import requests

        params = {"user": self.user, "password": self.password, "database": self.database, "query": query}
        for key, value in (parameters or {}).items():
            params[f"param_{key}"] = clickhouse_param_value(value)
        response = requests.post(self.url, params=params, timeout=self.timeout_seconds)
        if response.status_code >= 400:
            raise RuntimeError(f"status={response.status_code}, body={response.text}")
        return [json.loads(line) for line in response.text.splitlines() if line.strip()]

    def s3_object_already_materialized(self, object_path):
        query = (
            f"SELECT 1 AS found FROM {self.database}.load_audit "
            f"WHERE object_path = {clickhouse_string_literal(object_path)} AND row_count > 0 "
            "LIMIT 1 FORMAT JSONEachRow"
        )
        return bool(self.query_json_each_row(query))

    def ensure_market_data_schema(self):
        """기존 ClickHouse 테이블을 현재 적재 계약에 맞게 보정합니다."""
        self.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.database}.news_articles
            (
                published_at DateTime64(3, 'UTC'),
                symbol LowCardinality(String),
                article_id String,
                headline String,
                summary Nullable(String),
                content Nullable(String),
                url Nullable(String),
                source Nullable(String),
                author Nullable(String),
                updated_at Nullable(DateTime64(3, 'UTC')),
                received_at Nullable(DateTime64(3, 'UTC')),
                raw String,
                inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
            )
            ENGINE = ReplacingMergeTree(inserted_at)
            PARTITION BY toYYYYMM(published_at)
            ORDER BY (symbol, published_at, article_id)
            TTL toDateTime(published_at) + INTERVAL 30 DAY DELETE
            """
        )
        self.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.database}.news_article_localizations
            (
                published_at DateTime64(3, 'UTC'),
                symbol LowCardinality(String),
                article_id String,
                locale LowCardinality(String),
                symbols Array(String),
                target_symbol LowCardinality(String),
                subject_relevance LowCardinality(String),
                relevance_score_v2 Float32,
                relevance_reason String,
                direct_signals Array(String),
                headline Nullable(String),
                summary Nullable(String),
                url Nullable(String),
                source Nullable(String),
                localized_headline String,
                localized_summary String,
                key_points Array(String),
                positive_points Array(String),
                concerns Array(String),
                event_type LowCardinality(String),
                sentiment LowCardinality(String),
                impact_direction LowCardinality(String),
                why_it_matters String,
                model LowCardinality(String),
                localized_at DateTime64(3, 'UTC'),
                raw String,
                inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
            )
            ENGINE = ReplacingMergeTree(localized_at)
            PARTITION BY toYYYYMM(published_at)
            ORDER BY (symbol, locale, published_at, article_id)
            TTL toDateTime(published_at) + INTERVAL 30 DAY DELETE
            """
        )
        self.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.database}.news_company_daily_summaries
            (
                date Date,
                symbol LowCardinality(String),
                locale LowCardinality(String),
                summary String,
                key_points Array(String),
                positive_points Array(String),
                concerns Array(String),
                impact_direction LowCardinality(String),
                sentiment LowCardinality(String),
                article_ids Array(String),
                article_ids_hash String,
                article_count UInt32,
                mention_count UInt32,
                status LowCardinality(String),
                model LowCardinality(String),
                generated_at DateTime64(3, 'UTC'),
                version LowCardinality(String),
                raw String,
                inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
            )
            ENGINE = ReplacingMergeTree(generated_at)
            PARTITION BY toYYYYMM(date)
            ORDER BY (symbol, locale, date, version)
            TTL toDate(date) + INTERVAL 366 DAY DELETE
            """
        )
        self.execute(
            f"ALTER TABLE {self.database}.news_article_localizations "
            "ADD COLUMN IF NOT EXISTS key_points Array(String) AFTER localized_summary, "
            "ADD COLUMN IF NOT EXISTS positive_points Array(String) AFTER key_points, "
            "ADD COLUMN IF NOT EXISTS concerns Array(String) AFTER positive_points, "
            "ADD COLUMN IF NOT EXISTS target_symbol LowCardinality(String) DEFAULT symbol AFTER symbols, "
            "ADD COLUMN IF NOT EXISTS subject_relevance LowCardinality(String) DEFAULT 'mention' AFTER target_symbol, "
            "ADD COLUMN IF NOT EXISTS relevance_score_v2 Float32 DEFAULT 0 AFTER subject_relevance, "
            "ADD COLUMN IF NOT EXISTS relevance_reason String DEFAULT '' AFTER relevance_score_v2, "
            "ADD COLUMN IF NOT EXISTS direct_signals Array(String) DEFAULT [] AFTER relevance_reason"
        )
        if os.getenv("CLICKHOUSE_ENSURE_SESSION_COLUMNS", "true").lower() not in {"1", "true", "yes"}:
            return
        self.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.database}.market_events
            (
                event_time DateTime64(3, 'UTC'),
                symbol Nullable(String),
                event_type LowCardinality(String),
                layer LowCardinality(String),
                source LowCardinality(String),
                feed LowCardinality(String),
                feed_profile LowCardinality(String) DEFAULT feed,
                market_session LowCardinality(String) DEFAULT 'unknown',
                source_event_id Nullable(String),
                payload String,
                inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
            )
            ENGINE = ReplacingMergeTree(inserted_at)
            PARTITION BY toYYYYMM(event_time)
            ORDER BY (coalesce(symbol, '_MARKET'), event_type, event_time, feed_profile, market_session)
        """)
        self.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.database}.backfill_jobs
            (
                request_id String,
                symbol LowCardinality(String),
                interval LowCardinality(String),
                job_type LowCardinality(String),
                status LowCardinality(String),
                range_start DateTime64(3, 'UTC'),
                range_end DateTime64(3, 'UTC'),
                source_preference LowCardinality(String),
                object_paths Array(String),
                error Nullable(String),
                created_at DateTime64(3, 'UTC'),
                updated_at DateTime64(3, 'UTC'),
                finished_at Nullable(DateTime64(3, 'UTC')),
                raw String,
                inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
            )
            ENGINE = ReplacingMergeTree(inserted_at)
            PARTITION BY toYYYYMM(created_at)
            ORDER BY (request_id, symbol, interval)
        """)
        self.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.database}.storage_object_audit
            (
                object_path String,
                bucket Nullable(String),
                dataset LowCardinality(String),
                layer LowCardinality(String),
                symbol Nullable(String),
                interval Nullable(String),
                object_format LowCardinality(String),
                row_count UInt64,
                checksum Nullable(String),
                source LowCardinality(String),
                created_at DateTime64(3, 'UTC'),
                inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
            )
            ENGINE = ReplacingMergeTree(inserted_at)
            PARTITION BY toYYYYMM(created_at)
            ORDER BY (object_path, dataset, layer)
        """)
        self.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.database}.quote_ticks
            (
                event_time DateTime64(3, 'UTC'),
                symbol LowCardinality(String),
                bid_price Nullable(Float64),
                bid_size Nullable(Float64),
                ask_price Nullable(Float64),
                ask_size Nullable(Float64),
                bid_exchange Nullable(String),
                ask_exchange Nullable(String),
                conditions Array(String),
                source LowCardinality(String),
                feed LowCardinality(String),
                feed_profile LowCardinality(String) DEFAULT feed,
                market_session LowCardinality(String) DEFAULT 'unknown',
                source_event_id Nullable(String),
                received_at Nullable(DateTime64(3, 'UTC')),
                inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
            )
            ENGINE = MergeTree
            PARTITION BY toYYYYMM(event_time)
            ORDER BY (symbol, event_time, feed_profile)
            TTL event_time + INTERVAL 21 DAY DELETE
            SETTINGS non_replicated_deduplication_window = 100000
        """)
        self.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.database}.order_flow_profile_daily
            (
                session_date        Date,
                symbol              LowCardinality(String),
                price_bin           Float64,
                price_bin_size      Float64,
                ask_volume          Float64,
                bid_volume          Float64,
                unknown_volume      Float64,
                ask_trade_count     UInt64,
                bid_trade_count     UInt64,
                unknown_trade_count UInt64,
                trade_count         UInt64,
                volume              Float64,
                classification_version LowCardinality(String) DEFAULT 'orderflow-estimated-v2',
                source              LowCardinality(String) DEFAULT 'clickhouse-rollup',
                feed                LowCardinality(String) DEFAULT 'sip',
                feed_profile        LowCardinality(String) DEFAULT feed,
                market_session      LowCardinality(String) DEFAULT 'regular',
                inserted_at         DateTime64(3, 'UTC') DEFAULT now64(3)
            ) ENGINE = ReplacingMergeTree(inserted_at)
            PARTITION BY toYYYYMM(session_date)
            ORDER BY (symbol, session_date, price_bin_size, price_bin)
        """)
        for table, after_column in (
            ("trade_ticks", "feed"),
            ("quote_ticks", "feed"),
            ("chart_candles", "feed"),
            ("market_status_events", "feed"),
            ("market_events", "feed"),
        ):
            table_name = f"{self.database}.{clickhouse_identifier(table)}"
            self.execute(
                f"ALTER TABLE {table_name} "
                f"ADD COLUMN IF NOT EXISTS feed_profile LowCardinality(String) DEFAULT feed AFTER {after_column}, "
                "ADD COLUMN IF NOT EXISTS market_session LowCardinality(String) DEFAULT 'unknown' AFTER feed_profile"
            )
        chart_candles = f"{self.database}.chart_candles"
        self.execute(
            f"ALTER TABLE {chart_candles} "
            "ADD COLUMN IF NOT EXISTS price_adjustment LowCardinality(String) DEFAULT 'unknown' AFTER market_session, "
            "ADD COLUMN IF NOT EXISTS canonical_version LowCardinality(String) DEFAULT 'legacy' AFTER price_adjustment"
        )
        # Crypto 체결 수량과 거래량은 0.013 BTC처럼 소수일 수 있어서 Float64로 보정합니다.
        for table, column, column_type in (
            ("trade_ticks", "size", "Nullable(Float64)"),
            ("quote_ticks", "bid_size", "Nullable(Float64)"),
            ("quote_ticks", "ask_size", "Nullable(Float64)"),
            ("chart_candles", "volume", "Float64"),
        ):
            table_name = f"{self.database}.{clickhouse_identifier(table)}"
            self.execute(f"ALTER TABLE {table_name} MODIFY COLUMN IF EXISTS {column} {column_type}")


def clickhouse_identifier(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(value)):
        raise ValueError(f"Invalid ClickHouse identifier: {value}")
    return str(value)


def should_ensure_schema_on_start(environ=None):
    if environ is None:
        environ = os.environ
    value = environ.get("CLICKHOUSE_ENSURE_SCHEMA_ON_START")
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def clickhouse_string_literal(value):
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"
