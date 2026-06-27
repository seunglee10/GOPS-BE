# 역할: GOPS API Server가 ClickHouse에서 과거 캔들을 읽는 adapter입니다.
# 사용: Redis에 없는 과거 구간을 /api/charts/candles 응답으로 변환합니다.
# 연결: ClickHouse HTTP API를 사용해 별도 driver 없이 동작합니다.
import json
import os
import re

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
        query = f"""
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
          correction_type AS correctionType,
          source,
          feed,
          source_event_id AS sourceEventId
        FROM {self.table('chart_candles')}
        WHERE symbol = {{symbol:String}}
          AND interval = {{interval:String}}
        ORDER BY event_time DESC
        LIMIT {{limit:UInt32}}
        FORMAT JSONEachRow
        """
        rows = self.query_json_each_row(query, {"symbol": symbol, "interval": interval, "limit": int(limit)})
        return list(reversed(rows))

    def candles_since(self, symbol, interval, timestamp, limit=500, include_from=False):
        operator = ">=" if include_from else ">"
        query = f"""
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
          correction_type AS correctionType,
          source,
          feed,
          source_event_id AS sourceEventId
        FROM {self.table('chart_candles')}
        WHERE symbol = {{symbol:String}}
          AND interval = {{interval:String}}
          AND event_time {operator} parseDateTime64BestEffort({{timestamp:String}})
        ORDER BY event_time ASC, source_event_id ASC
        LIMIT {{limit:UInt32}}
        FORMAT JSONEachRow
        """
        return self.query_json_each_row(query, {"symbol": symbol, "interval": interval, "timestamp": timestamp, "limit": int(limit)})

    def candle_snapshot(self, symbol, interval, limit=160):
        candles = self.candles(symbol, interval, limit)
        feed = first_value(candles, "feed", "sip")
        source = first_value(candles, "source", "alpaca")
        return snapshot(symbol=symbol, interval=interval, candles=candles, source=source, feed=feed)

    def latest_status(self, symbol=None):
        where = "WHERE symbol = {symbol:String}" if symbol else "WHERE symbol IS NULL"
        query = f"""
        SELECT
          formatDateTime(event_time, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS eventTime,
          coalesce(symbol, '_MARKET') AS symbol,
          status_type AS statusType,
          status,
          reason,
          source,
          feed,
          source_event_id AS sourceEventId
        FROM {self.table('market_status_events')}
        {where}
        ORDER BY event_time DESC
        LIMIT 1
        FORMAT JSONEachRow
        """
        params = {"symbol": symbol} if symbol else {}
        rows = self.query_json_each_row(query, params)
        return rows[0] if rows else None

    def volume_profile_bins(self, symbol, from_time, to_time, price_bin_size=None, limit=10000):
        size_filter = "AND price_bin_size = {priceBinSize:Float64}" if price_bin_size else ""
        query = f"""
        SELECT
          formatDateTime(event_minute, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS minute,
          price_bin AS priceBin,
          price_bin_size AS priceBinSize,
          volume,
          trade_count AS tradeCount,
          vwap,
          source,
          feed
        FROM {self.table('volume_profile_bins_1m')}
        WHERE symbol = {{symbol:String}}
          AND event_minute >= parseDateTime64BestEffort({{fromTime:String}})
          AND event_minute <= parseDateTime64BestEffort({{toTime:String}})
          {size_filter}
        ORDER BY event_minute ASC, price_bin ASC
        LIMIT {{limit:UInt32}}
        FORMAT JSONEachRow
        """
        params = {"symbol": symbol, "fromTime": from_time, "toTime": to_time, "limit": int(limit)}
        if price_bin_size:
            params["priceBinSize"] = float(price_bin_size)
        return self.query_json_each_row(query, params)

    def search_symbols(self, query_text, limit=20):
        query = f"""
        SELECT
          symbol,
          name,
          exchange,
          market,
          asset_class AS assetClass,
          tradable,
          status,
          source,
          formatDateTime(updated_at, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS updatedAt
        FROM {self.table('symbols')}
        WHERE positionCaseInsensitive(symbol, {{query:String}}) > 0
           OR positionCaseInsensitive(name, {{query:String}}) > 0
        ORDER BY symbol ASC
        LIMIT {{limit:UInt32}}
        FORMAT JSONEachRow
        """
        return self.query_json_each_row(query, {"query": query_text, "limit": int(limit)})

    def symbol(self, symbol):
        query = f"""
        SELECT
          symbol,
          name,
          exchange,
          market,
          asset_class AS assetClass,
          tradable,
          status,
          source,
          formatDateTime(updated_at, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS updatedAt
        FROM {self.table('symbols')}
        WHERE symbol = {{symbol:String}}
        ORDER BY updated_at DESC
        LIMIT 1
        FORMAT JSONEachRow
        """
        rows = self.query_json_each_row(query, {"symbol": symbol})
        return rows[0] if rows else None

    def table(self, name):
        return f"{clickhouse_identifier(self.database)}.{clickhouse_identifier(name)}"

    def query_json_each_row(self, query, parameters):
        import requests

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


def clickhouse_identifier(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(value)):
        raise ValueError(f"Invalid ClickHouse identifier: {value}")
    return str(value)
