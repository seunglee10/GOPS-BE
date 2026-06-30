# 역할: GOPS API Server가 ClickHouse에서 과거 캔들을 읽는 adapter입니다.
# 사용: Redis에 없는 과거 구간을 /api/charts/candles 응답으로 변환합니다.
# 연결: ClickHouse HTTP API를 사용해 별도 driver 없이 동작합니다.
import json
import os
import re

from alfaka.common.env import load_dotenv
from alfaka.serving.dto import snapshot
from alfaka.serving.intervals import normalize_chart_interval, resolve_candle_limit
from alfaka.serving.moving_average import attach_moving_averages


class ClickHouseMarketDataProvider:
    def __init__(self, url=None, database=None, user=None, password=None):
        load_dotenv()
        self.url = (url or os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123")).rstrip("/")
        self.database = database or os.getenv("CLICKHOUSE_DATABASE", "market_data")
        self.user = user or os.getenv("CLICKHOUSE_USER", "alfaka")
        self.password = password or os.getenv("CLICKHOUSE_PASSWORD", "alfaka")
        if os.getenv("CLICKHOUSE_PROVIDER_ENSURE_SESSION_COLUMNS", "false").lower() in {"1", "true", "yes"}:
            self.ensure_market_data_schema()

    def candles(self, symbol, interval, limit=None, before=None, from_time=None, to_time=None):
        interval = normalize_chart_interval(interval)
        limit = resolve_candle_limit(interval, limit)
        if interval in {"5m", "10m"}:
            return self.aggregated_minute_candles(symbol, interval, limit, before=before, from_time=from_time, to_time=to_time)
        if interval == "1D":
            return self.daily_candles(symbol, interval, limit, before=before, from_time=from_time, to_time=to_time)
        if interval in {"1W", "1M"}:
            return self.aggregated_daily_candles(symbol, interval, limit, before=before, from_time=from_time, to_time=to_time)
        time_filter = ""
        params = {"symbol": symbol, "limit": int(limit)}
        interval_filter = "interval IN ('1D', '1d')" if interval == "1D" else "interval = {interval:String}"
        if interval != "1D":
            params["interval"] = interval
        if from_time:
            time_filter += "\n          AND event_time >= parseDateTime64BestEffort({fromTime:String})"
            params["fromTime"] = from_time
        if to_time:
            time_filter += "\n          AND event_time <= parseDateTime64BestEffort({toTime:String})"
            params["toTime"] = to_time
        if before:
            time_filter += "\n          AND event_time < parseDateTime64BestEffort({before:String})"
            params["before"] = before

        source_query = self.latest_chart_candles_source(f"""
            symbol = {{symbol:String}}
            AND {interval_filter}
            AND toDayOfWeek(event_time) BETWEEN 1 AND 5
            {time_filter}
        """)
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
          feed_profile AS feedProfile,
          market_session AS marketSession,
          source_event_id AS sourceEventId
        FROM (
          {source_query}
        )
        ORDER BY event_time DESC
        LIMIT {{limit:UInt32}}
        FORMAT JSONEachRow
        """
        rows = self.query_json_each_row(query, params)
        return list(reversed(rows))

    def daily_candles(self, symbol, interval="1D", limit=None, before=None, from_time=None, to_time=None):
        interval = normalize_chart_interval(interval)
        limit = resolve_candle_limit(interval, limit)
        time_filter = ""
        params = {"symbol": symbol, "limit": int(limit)}
        if from_time:
            time_filter += "\n          AND event_time >= parseDateTime64BestEffort({fromTime:String})"
            params["fromTime"] = from_time
        if to_time:
            time_filter += "\n          AND event_time <= parseDateTime64BestEffort({toTime:String})"
            params["toTime"] = to_time
        if before:
            time_filter += "\n          AND event_time < parseDateTime64BestEffort({before:String})"
            params["before"] = before

        source_query = self.latest_chart_candles_source(f"""
            symbol = {{symbol:String}}
            AND interval IN ('1D', '1d')
            AND toDayOfWeek(event_time) BETWEEN 1 AND 5
            {time_filter}
        """)
        query = f"""
        SELECT
          formatDateTime(bucket, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS timestamp,
          argMin(open, event_time) AS open,
          max(high) AS high,
          min(low) AS low,
          argMax(close, event_time) AS close,
          sum(volume) AS volume,
          min(is_closed) AS isClosed,
          'NONE' AS correctionType,
          anyLast(source) AS source,
          anyLast(feed) AS feed,
          anyLast(feed_profile) AS feedProfile,
          anyLast(market_session) AS marketSession,
          concat('agg/1D/', symbol, '/', formatDateTime(bucket, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC')) AS sourceEventId
        FROM (
          SELECT
            toStartOfDay(event_time) AS bucket,
            event_time,
            symbol,
            open,
            high,
            low,
            close,
            volume,
            is_closed,
            source,
            feed,
            feed_profile,
            market_session
          FROM (
            {source_query}
          )
        )
        GROUP BY symbol, bucket
        ORDER BY bucket DESC
        LIMIT {{limit:UInt32}}
        FORMAT JSONEachRow
        """
        rows = self.query_json_each_row(query, params)
        for row in rows:
            row["interval"] = "1D"
        return attach_moving_averages(list(reversed(rows)))

    def aggregated_minute_candles(self, symbol, interval, limit=None, before=None, from_time=None, to_time=None):
        interval = normalize_chart_interval(interval)
        limit = resolve_candle_limit(interval, limit)
        bucket_minutes = {"5m": 5, "10m": 10}[interval]
        time_filter = ""
        params = {"symbol": symbol, "limit": int(limit)}
        if from_time:
            time_filter += "\n          AND event_time >= parseDateTime64BestEffort({fromTime:String})"
            params["fromTime"] = from_time
        if to_time:
            time_filter += "\n          AND event_time <= parseDateTime64BestEffort({toTime:String})"
            params["toTime"] = to_time
        if before:
            time_filter += "\n          AND event_time < parseDateTime64BestEffort({before:String})"
            params["before"] = before

        source_query = self.latest_chart_candles_source(f"""
            symbol = {{symbol:String}}
            AND interval = '1m'
            AND toDayOfWeek(event_time) BETWEEN 1 AND 5
            {time_filter}
        """)
        query = f"""
        SELECT
          formatDateTime(bucket, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS timestamp,
          argMin(open, event_time) AS open,
          max(high) AS high,
          min(low) AS low,
          argMax(close, event_time) AS close,
          sum(volume) AS volume,
          1 AS isClosed,
          'NONE' AS correctionType,
          anyLast(source) AS source,
          anyLast(feed) AS feed,
          anyLast(feed_profile) AS feedProfile,
          anyLast(market_session) AS marketSession,
          concat('agg/{interval}/', symbol, '/', formatDateTime(bucket, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC')) AS sourceEventId
        FROM (
          SELECT
            toStartOfInterval(event_time, INTERVAL {bucket_minutes} minute) AS bucket,
            event_time,
            symbol,
            open,
            high,
            low,
            close,
            volume,
            source,
            feed,
            feed_profile,
            market_session
          FROM (
            {source_query}
          )
        )
        GROUP BY symbol, bucket
        ORDER BY bucket DESC
        LIMIT {{limit:UInt32}}
        FORMAT JSONEachRow
        """
        rows = self.query_json_each_row(query, params)
        for row in rows:
            row["interval"] = interval
        return attach_moving_averages(list(reversed(rows)))

    def aggregated_daily_candles(self, symbol, interval, limit=None, before=None, from_time=None, to_time=None):
        # V1 serves higher timeframes as query-time aggregation from stored daily candles.
        # The long-term contract is to materialize these interval candles into chart_candles.
        interval = normalize_chart_interval(interval)
        limit = resolve_candle_limit(interval, limit)
        bucket_expr = "toMonday(event_time)" if interval == "1W" else "toStartOfMonth(event_time)"
        time_filter = ""
        params = {"symbol": symbol, "limit": int(limit)}
        if from_time:
            time_filter += "\n          AND event_time >= parseDateTime64BestEffort({fromTime:String})"
            params["fromTime"] = from_time
        if to_time:
            time_filter += "\n          AND event_time <= parseDateTime64BestEffort({toTime:String})"
            params["toTime"] = to_time
        if before:
            time_filter += "\n          AND event_time < parseDateTime64BestEffort({before:String})"
            params["before"] = before

        source_query = self.latest_chart_candles_source(f"""
            symbol = {{symbol:String}}
            AND interval IN ('1D', '1d')
            AND toDayOfWeek(event_time) BETWEEN 1 AND 5
            {time_filter}
        """)
        query = f"""
        SELECT
          formatDateTime(bucket, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS timestamp,
          argMin(open, event_time) AS open,
          max(high) AS high,
          min(low) AS low,
          argMax(close, event_time) AS close,
          sum(volume) AS volume,
          1 AS isClosed,
          'NONE' AS correctionType,
          anyLast(source) AS source,
          anyLast(feed) AS feed,
          anyLast(feed_profile) AS feedProfile,
          anyLast(market_session) AS marketSession,
          concat('agg/{interval}/', symbol, '/', formatDateTime(bucket, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC')) AS sourceEventId
        FROM (
          SELECT
            {bucket_expr} AS bucket,
            event_time,
            symbol,
            open,
            high,
            low,
            close,
            volume,
            source,
            feed,
            feed_profile,
            market_session
          FROM (
            {source_query}
          )
        )
        GROUP BY symbol, bucket
        ORDER BY bucket DESC
        LIMIT {{limit:UInt32}}
        FORMAT JSONEachRow
        """
        rows = self.query_json_each_row(query, params)
        for row in rows:
            row["interval"] = interval
        return attach_moving_averages(list(reversed(rows)))

    def candles_since(self, symbol, interval, timestamp, limit=500, include_from=False):
        interval = normalize_chart_interval(interval)
        if interval in {"5m", "10m", "1W", "1M"}:
            return self.candles(symbol, interval, limit, from_time=timestamp)
        operator = ">=" if include_from else ">"
        interval_filter = "interval IN ('1D', '1d')" if interval == "1D" else "interval = {interval:String}"
        params = {"symbol": symbol, "timestamp": timestamp, "limit": int(limit)}
        if interval != "1D":
            params["interval"] = interval
        source_query = self.latest_chart_candles_source(f"""
            symbol = {{symbol:String}}
            AND {interval_filter}
            AND toDayOfWeek(event_time) BETWEEN 1 AND 5
            AND event_time {operator} parseDateTime64BestEffort({{timestamp:String}})
        """)
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
          feed_profile AS feedProfile,
          market_session AS marketSession,
          source_event_id AS sourceEventId
        FROM (
          {source_query}
        )
        ORDER BY event_time ASC, source_event_id ASC
        LIMIT {{limit:UInt32}}
        FORMAT JSONEachRow
        """
        return self.query_json_each_row(query, params)

    def candle_snapshot(self, symbol, interval, limit=None):
        interval = normalize_chart_interval(interval)
        candles = attach_moving_averages(self.candles(symbol, interval, limit))
        feed = first_value(candles, "feed", "sip")
        source = first_value(candles, "source", "alpaca")
        return snapshot(symbol=symbol, interval=interval, candles=candles, source=source, feed=feed)

    def candle_coverage(self, symbol, interval):
        interval = normalize_chart_interval(interval)
        stored_interval = "1m" if interval in {"5m", "10m"} else "1D" if interval in {"1W", "1M"} else interval
        interval_filter = "interval IN ('1D', '1d')" if stored_interval == "1D" else "interval = {interval:String}"
        params = {"symbol": symbol}
        if stored_interval != "1D":
            params["interval"] = stored_interval
        if stored_interval == "1D":
            row_count_expr = "uniqExactIf(toDate(event_time), toDayOfWeek(event_time) BETWEEN 1 AND 5)"
        else:
            row_count_expr = "countIf(toDayOfWeek(event_time) BETWEEN 1 AND 5)"

        source_query = self.latest_chart_candles_source(f"""
            symbol = {{symbol:String}}
            AND {interval_filter}
        """)
        query = f"""
        SELECT
          {row_count_expr} AS rowCount,
          countIf(toDayOfWeek(event_time) NOT BETWEEN 1 AND 5) AS invalidRowCount,
          formatDateTime(minIf(event_time, toDayOfWeek(event_time) BETWEEN 1 AND 5), '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS availableFrom,
          formatDateTime(maxIf(event_time, toDayOfWeek(event_time) BETWEEN 1 AND 5), '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS availableTo
        FROM (
          {source_query}
        )
        FORMAT JSONEachRow
        """
        rows = self.query_json_each_row(query, params)
        row = rows[0] if rows else {}
        count = int(row.get("rowCount") or 0)
        if count <= 0:
            return {"rowCount": 0, "invalidRowCount": int(row.get("invalidRowCount") or 0), "availableFrom": None, "availableTo": None}
        return {
            "rowCount": count,
            "invalidRowCount": int(row.get("invalidRowCount") or 0),
            "availableFrom": row.get("availableFrom"),
            "availableTo": row.get("availableTo"),
        }

    def candle_timestamps(self, symbol, interval, from_time, to_time, limit=200000):
        interval = normalize_chart_interval(interval)
        stored_interval = "1m" if interval in {"5m", "10m"} else "1D" if interval in {"1W", "1M"} else interval
        interval_filter = "interval IN ('1D', '1d')" if stored_interval == "1D" else "interval = {interval:String}"
        params = {
            "symbol": symbol,
            "from": from_time,
            "to": to_time,
            "limit": int(limit),
        }
        if stored_interval != "1D":
            params["interval"] = stored_interval
        source_query = self.latest_chart_candles_source(f"""
            symbol = {{symbol:String}}
            AND {interval_filter}
            AND event_time >= parseDateTimeBestEffort({{from:String}})
            AND event_time < parseDateTimeBestEffort({{to:String}})
        """)
        query = f"""
        SELECT
          formatDateTime(event_time, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS timestamp
        FROM (
          {source_query}
        )
        WHERE toDayOfWeek(event_time) BETWEEN 1 AND 5
        ORDER BY event_time ASC
        LIMIT {{limit:UInt32}}
        FORMAT JSONEachRow
        """
        return [row["timestamp"] for row in self.query_json_each_row(query, params) if row.get("timestamp")]

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
          feed_profile AS feedProfile,
          market_session AS marketSession,
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

    def hot_symbols_by_dollar_volume(self, symbols, limit=20):
        symbols = [symbol for symbol in symbols if isinstance(symbol, str) and symbol.strip()]
        if not symbols:
            return []
        rows = self._hot_symbols_by_interval_dollar_volume(symbols, limit=limit, interval="1m")
        if rows:
            return rows
        return self._hot_symbols_by_interval_dollar_volume(symbols, limit=limit, interval="1D")

    def _hot_symbols_by_interval_dollar_volume(self, symbols, limit=20, interval="1m"):
        normalized_interval = "1D" if interval in {"1D", "1d"} else "1m"
        interval_filter = "interval IN ('1D', '1d')" if normalized_interval == "1D" else "interval = '1m'"
        try:
            lookback_days = max(1, int(os.getenv("HOT_TIER_LOOKBACK_DAYS", "14")))
        except ValueError:
            lookback_days = 14
        latest_source_query = f"""
        SELECT event_time
        FROM {self.table('chart_candles')}
        WHERE symbol IN {{symbols:Array(String)}}
          AND {interval_filter}
          AND event_time >= subtractDays(now(), {{lookbackDays:UInt32}})
          AND toDayOfWeek(event_time) BETWEEN 1 AND 5
        """
        session_source_query = self.latest_chart_candles_source(f"""
            symbol IN {{symbols:Array(String)}}
            AND {interval_filter}
            AND event_time >= subtractDays(now(), {{lookbackDays:UInt32}})
            AND toDate(event_time) = latest_session_date
            AND toDayOfWeek(event_time) BETWEEN 1 AND 5
        """)
        query = f"""
        WITH (
          SELECT max(toDate(event_time))
          FROM ({latest_source_query})
        ) AS latest_session_date
        SELECT
          symbol,
          argMax(close, event_time) AS lastPrice,
          if(argMin(open, event_time) = 0, NULL, round(((argMax(close, event_time) - argMin(open, event_time)) / argMin(open, event_time)) * 100, 2)) AS changePercent,
          sum(toFloat64(row_volume) * close) AS sessionDollarVolume,
          sum(row_volume) AS volume,
          formatDateTime(max(event_time), '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS sourceUpdatedAt,
          {clickhouse_string_literal('current_session' if normalized_interval == '1m' else 'latest_daily_session')} AS rankingWindow,
          {clickhouse_string_literal(f'clickhouse_{normalized_interval}_session_aggregate')} AS rankReason
        FROM (
          SELECT
            symbol,
            event_time,
            open,
            close,
            volume AS row_volume
          FROM (
            {session_source_query}
          )
          WHERE toDate(event_time) = latest_session_date
        )
        GROUP BY symbol
        ORDER BY sessionDollarVolume DESC, symbol ASC
        LIMIT {{limit:UInt32}}
        FORMAT JSONEachRow
        """
        return self.query_json_each_row(query, {"symbols": symbols, "limit": int(limit), "lookbackDays": lookback_days})

    def latest_chart_candles_source(self, where_sql):
        return f"""
        SELECT
          event_time,
          symbol,
          interval,
          open,
          high,
          low,
          close,
          volume,
          is_closed,
          ma5,
          ma20,
          ma60,
          correction_type,
          source,
          feed,
          feed_profile,
          market_session,
          source_event_id
        FROM (
          SELECT
            event_time,
            symbol,
            interval,
            open,
            high,
            low,
            close,
            volume,
            is_closed,
            ma5,
            ma20,
            ma60,
            correction_type,
            source,
            feed,
            feed_profile,
            market_session,
            source_event_id,
            row_number() OVER (
              PARTITION BY symbol, if(interval = '1d', '1D', interval), event_time
              ORDER BY inserted_at DESC, ifNull(source_event_id, '') DESC
            ) AS rn
          FROM {self.table('chart_candles')}
          WHERE {where_sql}
        )
        WHERE rn = 1
        """

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
          feed,
          feed_profile AS feedProfile,
          market_session AS marketSession
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

    def ensure_market_data_schema(self):
        for table in ("trade_ticks", "chart_candles", "volume_profile_bins_1m", "market_status_events"):
            self.execute(
                f"ALTER TABLE {self.table(table)} "
                "ADD COLUMN IF NOT EXISTS feed_profile LowCardinality(String) DEFAULT feed AFTER feed, "
                "ADD COLUMN IF NOT EXISTS market_session LowCardinality(String) DEFAULT 'unknown' AFTER feed_profile"
            )

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
            params[f"param_{key}"] = clickhouse_param_value(value)

        response = requests.post(self.url, params=params, timeout=10)
        if response.status_code >= 400:
            raise RuntimeError(f"ClickHouse query failed: status={response.status_code}, body={response.text}")
        return [json.loads(line) for line in response.text.splitlines() if line.strip()]

    def execute(self, query):
        import requests

        response = requests.post(
            self.url,
            params={"user": self.user, "password": self.password, "database": self.database, "query": query},
            timeout=10,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"ClickHouse query failed: status={response.status_code}, body={response.text}")


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


def clickhouse_param_value(value):
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(clickhouse_string_literal(item) for item in value) + "]"
    return value


def clickhouse_string_literal(value):
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"
