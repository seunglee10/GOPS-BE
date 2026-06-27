# 역할: Kafka Processed Topic을 읽어 ClickHouse 조회 테이블에 적재합니다.
# 사용: GOPS API Server가 과거 캔들을 ClickHouse에서 읽을 수 있게 만드는 연결 job입니다.
# 입력: 기본은 market.candles.closed.v1만 적재합니다. tick 적재는 옵션입니다.
import json
import os
import sys
from datetime import datetime, timezone

import requests

from alfaka.common.env import load_dotenv, parse_csv
from alfaka.common.kafka_io import create_json_consumer


def main():
    load_dotenv()

    kafka_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    group_id = os.getenv("KAFKA_CLICKHOUSE_GROUP_ID", "alfaka-clickhouse-loader")
    topics = parse_csv(os.getenv("KAFKA_CLICKHOUSE_TOPICS", ",".join([
        os.getenv("KAFKA_CLOSED_CANDLE_TOPIC", "market.candles.closed.v1"),
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
        "ma5": float_or_none(ma.get("ma5")),
        "ma20": float_or_none(ma.get("ma20")),
        "ma60": float_or_none(ma.get("ma60")),
        "is_closed": bool(payload.get("isClosed", True)),
        "correction_type": payload.get("correctionType", "NONE"),
        "source": payload.get("source", "stream-processor"),
        "feed": payload.get("feed") or "unknown",
        "created_at": clickhouse_time_or_none(payload.get("createdAt") or payload.get("updatedAt")),
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
        self.database = database
        self.user = user
        self.password = password

    def insert_json_each_row(self, table, rows):
        if not rows:
            return

        query = f"INSERT INTO {self.database}.{table} FORMAT JSONEachRow"
        body = "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows) + "\n"
        response = requests.post(
            self.url,
            params={"user": self.user, "password": self.password, "database": self.database, "query": query},
            data=body.encode("utf-8"),
            timeout=10,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"status={response.status_code}, body={response.text}")
