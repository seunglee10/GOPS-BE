# 역할: GOPS API Server가 ClickHouse에서 과거 캔들을 읽는 adapter입니다.
# 사용: Redis에 없는 과거 구간을 /api/charts/candles 응답으로 변환합니다.
# 연결: ClickHouse HTTP API를 사용해 별도 driver 없이 동작합니다.
import json
import os

import requests

from alfaka.common.env import load_dotenv
from alfaka.serving.dto import snapshot


class ClickHouseMarketDataProvider:
    def __init__(self, url=None, database=None, user=None, password=None):
        load_dotenv()
        self.url = (url or os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123")).rstrip("/")
        self.database = database or os.getenv("CLICKHOUSE_DATABASE", "market_data")
        self.user = user or os.getenv("CLICKHOUSE_USER", "alfaka")
        self.password = password or os.getenv("CLICKHOUSE_PASSWORD", "alfaka")

    def candles(self, symbol, interval, limit=160):
        query = """
        SELECT
          formatDateTime(event_time, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS timestamp,
          open,
          high,
          low,
          close,
          volume,
          is_closed AS isClosed,
          ma5,
          ma20,
          ma60,
          source,
          feed
        FROM market_data.chart_candles
        WHERE symbol = {symbol:String}
          AND interval = {interval:String}
        ORDER BY event_time DESC
        LIMIT {limit:UInt32}
        FORMAT JSONEachRow
        """
        rows = self.query_json_each_row(query, {"symbol": symbol, "interval": interval, "limit": int(limit)})
        return list(reversed(rows))

    def candle_snapshot(self, symbol, interval, limit=160):
        candles = self.candles(symbol, interval, limit)
        feed = first_value(candles, "feed", "sip")
        source = first_value(candles, "source", "alpaca")
        return snapshot(symbol=symbol, interval=interval, candles=candles, source=source, feed=feed)

    def query_json_each_row(self, query, parameters):
        params = {
            "user": self.user,
            "password": self.password,
            "database": self.database,
            "query": query,
        }
        for key, value in parameters.items():
            params[f"param_{key}"] = value

        response = requests.post(self.url, params=params, timeout=10)
        if response.status_code >= 400:
            raise RuntimeError(f"ClickHouse query failed: status={response.status_code}, body={response.text}")
        return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def first_value(rows, key, fallback):
    for row in rows:
        value = row.get(key)
        if value:
            return value
    return fallback
