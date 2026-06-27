# 역할: Kafka Processed Topic을 읽어 ClickHouse 조회 테이블에 적재합니다.
# 사용: GOPS API Server가 과거 캔들을 ClickHouse에서 읽을 수 있게 만드는 연결 job입니다.
# 입력: 기본은 market.candles.closed.v1만 적재합니다. tick 적재는 옵션입니다.
import json
import os
import re
import sys
from datetime import datetime, timezone

from alfaka.common.env import load_dotenv, parse_csv
from alfaka.common.kafka_io import create_json_consumer


def main():
    load_dotenv()

    kafka_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    group_id = os.getenv("KAFKA_CLICKHOUSE_GROUP_ID", "alfaka-clickhouse-loader")
    topics = parse_csv(os.getenv("KAFKA_CLICKHOUSE_TOPICS", ",".join([
        os.getenv("KAFKA_CLOSED_CANDLE_TOPIC", "market.candles.closed.v1"),
        os.getenv("KAFKA_STATUS_TOPIC", "market.status.v1"),
        os.getenv("KAFKA_VOLUME_PROFILE_BINS_TOPIC", "market.volume-profile-bins.1m.v1"),
    ])))
    load_trades = os.getenv("CLICKHOUSE_LOAD_TRADES", "false").lower() in {"1", "true", "yes"}

    client = ClickHouseHttpClient(
        url=os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
        database=os.getenv("CLICKHOUSE_DATABASE", "market_data"),
        user=os.getenv("CLICKHOUSE_USER", "alfaka"),
        password=os.getenv("CLICKHOUSE_PASSWORD", "alfaka"),
    )

    consumer = create_json_consumer(topics, kafka_servers, group_id, "alfaka-clickhouse-consumer")
    print(f"ClickHouse loader 시작: topics={topics}", flush=True)
    print(f"ClickHouse 연결: {client.url}/{client.database}", flush=True)

    for record in consumer:
        payload = record.value
        try:
            load_payload(client, payload, load_trades=load_trades)
        except Exception as exc:
            print(f"ClickHouse 적재 실패: {exc}; payload={json.dumps(payload, ensure_ascii=False)}", file=sys.stderr, flush=True)


def load_payload(client, payload, load_trades=False):
    event_type = payload.get("eventType")
    if event_type == "TRADE":
        if not load_trades:
            print(f"ClickHouse trade 적재 제외: symbol={payload.get('symbol', 'UNKNOWN')}", flush=True)
            return
        row = trade_to_clickhouse_row(payload)
        client.insert_json_each_row("trade_ticks", [row])
        print(f"ClickHouse trade 적재: symbol={row['symbol']} time={row['event_time']}", flush=True)
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

    if event_type == "CANDLE" and payload.get("isClosed", True):
        row = candle_to_clickhouse_row(payload)
        client.insert_json_each_row("chart_candles", [row])
        print(f"ClickHouse candle 적재: symbol={row['symbol']} interval={row['interval']} time={row['event_time']}", flush=True)
        return

    print(f"ClickHouse 적재 제외 eventType={event_type}", flush=True)


def trade_to_clickhouse_row(payload):
    return {
        "event_time": clickhouse_time(payload.get("timestamp")),
        "symbol": payload.get("symbol", "UNKNOWN"),
        "trade_id": int_or_zero(payload.get("tradeId")),
        "price": float_or_zero(payload.get("price")),
        "size": int_or_none(payload.get("size")),
        "exchange": payload.get("exchange"),
        "conditions": payload.get("conditions") or [],
        "tape": payload.get("tape"),
        "source": payload.get("source", "alpaca"),
        "feed": payload.get("feed") or "unknown",
        "source_event_id": payload.get("sourceEventId"),
        "received_at": clickhouse_time_or_none(payload.get("receivedAt")),
    }


def candle_to_clickhouse_row(payload):
    ma = payload.get("ma") or {}
    return {
        "event_time": clickhouse_time(payload.get("timestamp")),
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
        "source_event_id": payload.get("sourceEventId"),
        "created_at": clickhouse_time_or_none(payload.get("createdAt") or payload.get("updatedAt")),
    }


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


def clickhouse_identifier(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(value)):
        raise ValueError(f"Invalid ClickHouse identifier: {value}")
    return str(value)
