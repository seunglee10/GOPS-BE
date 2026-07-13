# 역할: GOPS API Server가 ClickHouse에서 과거 캔들을 읽는 adapter입니다.
# 사용: Redis에 없는 과거 구간을 /api/charts/candles 응답으로 변환합니다.
# 연결: ClickHouse HTTP API를 사용해 별도 driver 없이 동작합니다.
import json
import os
import re
import time
from datetime import datetime, timezone

from alfaka.alpaca.feed_profiles import visible_extended_session_windows
from alfaka.common.env import load_dotenv
from alfaka.common.canonical import CANONICAL_VERSION, HISTORICAL_SERVING_PRICE_ADJUSTMENTS, SERVING_PRICE_ADJUSTMENTS
from alfaka.common.symbols import is_crypto_symbol
from alfaka.serving.dto import snapshot
from alfaka.serving.intervals import INTRADAY_DERIVED_INTERVALS, INTRADAY_INTERVAL_MINUTES, historical_source_interval_for, max_request_bars, normalize_chart_interval, resolve_candle_limit
from alfaka.serving.moving_average import attach_moving_averages
from alfaka.serving.session_buckets import BUCKET_POLICY_REGULAR_SESSION, aggregate_regular_session_candles


class ClickHouseMarketDataProvider:
    def __init__(self, url=None, database=None, user=None, password=None, now_provider=None):
        """ClickHouse HTTP API 접속 정보를 환경변수 또는 인자로 초기화합니다."""
        load_dotenv()
        self.url = (url or os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123")).rstrip("/")
        self.database = database or os.getenv("CLICKHOUSE_DATABASE", "market_data")
        self.user = user or os.getenv("CLICKHOUSE_USER", "alfaka")
        self.password = password or os.getenv("CLICKHOUSE_PASSWORD", "alfaka")
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        if os.getenv("CLICKHOUSE_PROVIDER_ENSURE_SESSION_COLUMNS", "false").lower() in {"1", "true", "yes"}:
            self.ensure_market_data_schema()

    def candles(self, symbol, interval, limit=None, before=None, from_time=None, to_time=None):
        """요청 interval에 맞는 캔들 목록을 ClickHouse에서 조회합니다."""
        interval = normalize_chart_interval(interval)
        limit = resolve_candle_limit(interval, limit)
        if interval in INTRADAY_DERIVED_INTERVALS:
            return self.direct_or_aggregated_candles(
                symbol,
                interval,
                limit,
                aggregate=self.aggregated_minute_candles,
                before=before,
                from_time=from_time,
                to_time=to_time,
            )
        if interval == "1D":
            return self.daily_candles(symbol, interval, limit, before=before, from_time=from_time, to_time=to_time)
        if interval in {"1W", "1M"}:
            return self.direct_or_aggregated_candles(
                symbol,
                interval,
                limit,
                aggregate=self.aggregated_daily_candles,
                before=before,
                from_time=from_time,
                to_time=to_time,
            )
        return self.stored_interval_candles(symbol, interval, limit, before=before, from_time=from_time, to_time=to_time)

    def direct_or_aggregated_candles(self, symbol, interval, limit=None, aggregate=None, before=None, from_time=None, to_time=None):
        """저장된 direct interval 캔들을 우선 쓰고, 비어 있으면 기존 source 집계로 보강합니다."""
        interval = normalize_chart_interval(interval)
        limit = resolve_candle_limit(interval, limit)
        reference = self.now_provider()
        direct_rows = merge_candle_rows(
            self.stored_interval_candles(symbol, interval, limit, before=before, from_time=from_time, to_time=to_time),
            interval=interval,
        )
        direct_rows = with_higher_timeframe_closed_state(direct_rows, interval, now=reference)
        if len(direct_rows) >= limit:
            return attach_moving_averages(direct_rows[-limit:], overwrite=True)
        aggregate_rows = aggregate(symbol, interval, limit, before=before, from_time=from_time, to_time=to_time) if aggregate else []
        aggregate_rows = with_higher_timeframe_closed_state(aggregate_rows, interval, now=reference)
        return attach_moving_averages(
            merge_candle_rows(aggregate_rows, direct_rows, interval=interval)[-limit:],
            overwrite=True,
        )

    def stored_interval_candles(self, symbol, interval, limit=None, before=None, from_time=None, to_time=None):
        """ClickHouse chart_candles에 저장된 요청 interval row를 그대로 조회합니다."""
        interval = normalize_chart_interval(interval)
        limit = resolve_candle_limit(interval, limit)
        include_live = include_live_stored_candles(interval)
        time_filter = ""
        params = {"symbol": symbol, "limit": int(limit)}
        interval_filter = "interval IN ('1D', '1d')" if interval == "1D" else "interval = {interval:String}"
        if interval != "1D":
            params["interval"] = interval
        bucket_policy_filter = ""
        if interval in INTRADAY_DERIVED_INTERVALS and not is_crypto_symbol(symbol):
            bucket_policy_filter = "\n          AND bucket_policy = {bucketPolicy:String}"
            params["bucketPolicy"] = BUCKET_POLICY_REGULAR_SESSION
        if from_time:
            time_filter += "\n          AND event_time >= parseDateTime64BestEffort({fromTime:String})"
            params["fromTime"] = from_time
        if to_time:
            time_filter += "\n          AND event_time <= parseDateTime64BestEffort({toTime:String})"
            params["toTime"] = to_time
        if before:
            time_filter += "\n          AND event_time < parseDateTime64BestEffort({before:String})"
            params["before"] = before

        session_filter = self.market_session_filter_sql(symbol)
        source_query = self.latest_chart_candles_source(f"""
            symbol = {{symbol:String}}
            AND {interval_filter}
            AND {session_filter}
            {bucket_policy_filter}
            {time_filter}
        """, include_live=include_live)
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
        for row in rows:
            row["interval"] = interval
        return list(reversed(rows))

    def daily_candles(self, symbol, interval="1D", limit=None, before=None, from_time=None, to_time=None):
        """저장된 일봉을 차트 응답용 일봉으로 조회합니다."""
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

        source_query = self.latest_canonical_daily_source(f"""
            symbol = {{symbol:String}}
            AND interval IN ('1D', '1d')
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
          correction_type AS correctionType,
          source,
          feed,
          feed_profile AS feedProfile,
          'regular' AS marketSession,
          source_event_id AS sourceEventId
        FROM (
          {source_query}
        )
        ORDER BY event_time DESC
        LIMIT {{limit:UInt32}}
        FORMAT JSONEachRow
        """
        rows = self.query_json_each_row(query, params)
        for row in rows:
            row["interval"] = "1D"
        return attach_moving_averages(list(reversed(rows)), overwrite=True)

    def aggregated_minute_candles(self, symbol, interval, limit=None, before=None, from_time=None, to_time=None):
        """선호 intraday 원본 봉을 파생 주기로 묶고, 기존 1분봉을 fallback으로 사용합니다."""
        interval = normalize_chart_interval(interval)
        limit = resolve_candle_limit(interval, limit)
        if is_crypto_symbol(symbol):
            return self._clock_aligned_minute_candles(
                symbol, interval, limit, before=before, from_time=from_time, to_time=to_time,
            )
        source_interval = historical_source_interval_for(interval)
        preferred = self._aggregated_regular_session_candles_from_source(
            symbol,
            interval,
            source_interval,
            limit,
            before=before,
            from_time=from_time,
            to_time=to_time,
        )
        if source_interval == "1m" or len(preferred) >= limit:
            return attach_moving_averages(preferred[-limit:], overwrite=True)
        fallback = self._aggregated_regular_session_candles_from_source(
            symbol,
            interval,
            "1m",
            limit,
            before=before,
            from_time=from_time,
            to_time=to_time,
        )
        return attach_moving_averages(
            merge_candle_rows(fallback, preferred, interval=interval)[-limit:],
            overwrite=True,
        )

    def _aggregated_regular_session_candles_from_source(
        self,
        symbol,
        interval,
        source_interval,
        limit,
        *,
        before=None,
        from_time=None,
        to_time=None,
    ):
        bucket_minutes = INTRADAY_INTERVAL_MINUTES[interval]
        source_minutes = INTRADAY_INTERVAL_MINUTES[source_interval]
        time_filter = ""
        source_bars_per_target = max(1, (bucket_minutes + source_minutes - 1) // source_minutes)
        session_padding = (390 + source_minutes - 1) // source_minutes
        source_limit = min(
            max_request_bars(source_interval),
            max(int(limit) * source_bars_per_target + session_padding, int(limit)),
        )
        params = {"symbol": symbol, "limit": int(source_limit)}
        if source_interval != "1m":
            params["sourceInterval"] = source_interval
        if from_time:
            time_filter += "\n          AND event_time >= parseDateTime64BestEffort({fromTime:String})"
            params["fromTime"] = from_time
        if to_time:
            time_filter += "\n          AND event_time <= parseDateTime64BestEffort({toTime:String})"
            params["toTime"] = to_time
        if before:
            time_filter += "\n          AND event_time < parseDateTime64BestEffort({before:String})"
            params["before"] = before

        session_filter = self.market_session_filter_sql(symbol)
        interval_filter = "interval = '1m'" if source_interval == "1m" else "interval = {sourceInterval:String}"
        source_query = self.latest_chart_candles_source(f"""
            symbol = {{symbol:String}}
            AND {interval_filter}
            AND {session_filter}
            {time_filter}
        """, include_live=include_live_stored_candles("1m"))
        query = f"""
        SELECT
          formatDateTime(event_time, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS timestamp,
          symbol,
          open,
          high,
          low,
          close,
          volume,
          is_closed AS isClosed,
          correction_type AS correctionType,
          source,
          feed,
          feed_profile AS feedProfile,
          market_session AS marketSession,
          price_adjustment AS priceAdjustment,
          canonical_version AS canonicalVersion,
          source_event_id AS sourceEventId
        FROM (
          {source_query}
        )
        ORDER BY event_time DESC
        LIMIT {{limit:UInt32}}
        FORMAT JSONEachRow
        """
        rows = list(reversed(self.query_json_each_row(query, params)))
        for row in rows:
            row["interval"] = source_interval
        return aggregate_regular_session_candles(
            rows,
            interval,
            now=self.now_provider(),
            source_interval=source_interval,
        )[-limit:]

    def _clock_aligned_minute_candles(self, symbol, interval, limit, *, before=None, from_time=None, to_time=None):
        bucket_minutes = INTRADAY_INTERVAL_MINUTES[interval]
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
            {time_filter}
        """, include_live=include_live_stored_candles("1m"))
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
          anyLast(market_session) AS marketSession
        FROM (
          SELECT
            toStartOfInterval(event_time, INTERVAL {bucket_minutes} minute) AS bucket,
            event_time, symbol, open, high, low, close, volume, source, feed, feed_profile, market_session
          FROM ({source_query})
        )
        GROUP BY symbol, bucket
        ORDER BY bucket DESC
        LIMIT {{limit:UInt32}}
        FORMAT JSONEachRow
        """
        rows = self.query_json_each_row(query, params)
        for row in rows:
            row["interval"] = interval
        return attach_moving_averages(list(reversed(rows)), overwrite=True)

    def aggregated_daily_candles(self, symbol, interval, limit=None, before=None, from_time=None, to_time=None, limit_buffer=0):
        """일봉을 주봉/월봉으로 묶어 차트용 캔들을 만듭니다."""
        # V1 serves higher timeframes as query-time aggregation from stored daily candles.
        # The long-term contract is to materialize these interval candles into chart_candles.
        interval = normalize_chart_interval(interval)
        limit = resolve_candle_limit(interval, limit)
        limit = min(limit + max(0, int(limit_buffer or 0)), max_request_bars(interval) + 1)
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

        source_query = self.latest_canonical_daily_source(f"""
            symbol = {{symbol:String}}
            AND interval IN ('1D', '1d')
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
          'regular' AS marketSession,
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
        rows = with_higher_timeframe_closed_state(rows, interval, now=self.now_provider())
        return attach_moving_averages(list(reversed(rows)), overwrite=True)

    def candles_since(self, symbol, interval, timestamp, limit=500, include_from=False):
        """WebSocket gap 보정용으로 특정 timestamp 이후의 캔들을 조회합니다."""
        interval = normalize_chart_interval(interval)
        if interval in {*INTRADAY_DERIVED_INTERVALS, "1W", "1M"}:
            return self.candles(symbol, interval, limit, from_time=timestamp)
        operator = ">=" if include_from else ">"
        interval_filter = "interval IN ('1D', '1d')" if interval == "1D" else "interval = {interval:String}"
        params = {"symbol": symbol, "timestamp": timestamp, "limit": int(limit)}
        if interval != "1D":
            params["interval"] = interval
        session_filter = "1 = 1" if interval == "1D" else self.market_session_filter_sql(symbol)
        source_factory = self.latest_canonical_daily_source if interval == "1D" else self.latest_chart_candles_source
        source_query = source_factory(f"""
            symbol = {{symbol:String}}
            AND {interval_filter}
            AND {session_filter}
            AND event_time {operator} parseDateTime64BestEffort({{timestamp:String}})
        """, include_live=include_live_stored_candles(interval))
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
        candles = attach_moving_averages(self.candles(symbol, interval, limit), overwrite=True)
        feed = first_value(candles, "feed", "sip")
        source = first_value(candles, "source", "alpaca")
        return snapshot(symbol=symbol, interval=interval, candles=candles, source=source, feed=feed)

    def candle_coverage(self, symbol, interval):
        """backfill 판단에 필요한 저장 캔들 개수와 가용 기간을 계산합니다."""
        interval = normalize_chart_interval(interval)
        if interval in {*INTRADAY_DERIVED_INTERVALS, "1W", "1M"}:
            direct_bucket_policy = (
                BUCKET_POLICY_REGULAR_SESSION
                if interval in INTRADAY_DERIVED_INTERVALS and not is_crypto_symbol(symbol)
                else None
            )
            direct = self.stored_interval_coverage(symbol, interval, bucket_policy=direct_bucket_policy)
            if int(direct.get("rowCount") or 0) > 0:
                return {**direct, "sourceInterval": interval}
        stored_interval = (
            "1m"
            if is_crypto_symbol(symbol) and interval in INTRADAY_DERIVED_INTERVALS
            else historical_source_interval_for(interval)
        )
        source_coverage = self.stored_interval_coverage(symbol, stored_interval)
        if int(source_coverage.get("rowCount") or 0) > 0 or stored_interval == "1m":
            return {**source_coverage, "sourceInterval": stored_interval}
        fallback = self.stored_interval_coverage(symbol, "1m")
        return {**fallback, "sourceInterval": "1m"}

    def stored_interval_coverage(self, symbol, stored_interval, bucket_policy=None):
        stored_interval = normalize_chart_interval(stored_interval)
        interval_filter = "interval IN ('1D', '1d')" if stored_interval == "1D" else "interval = {interval:String}"
        params = {"symbol": symbol}
        if stored_interval != "1D":
            params["interval"] = stored_interval
        bucket_policy_filter = ""
        if bucket_policy:
            params["bucketPolicy"] = bucket_policy
            bucket_policy_filter = "\n            AND bucket_policy = {bucketPolicy:String}"
        if is_crypto_symbol(symbol):
            if stored_interval == "1D":
                row_count_expr = "uniqExact(toDate(event_time))"
            else:
                row_count_expr = "count()"
            invalid_row_count_expr = "0"
            available_from_expr = "min(event_time)"
            available_to_expr = "max(event_time)"
        elif stored_interval == "1D":
            row_count_expr = "uniqExactIf(toDate(event_time), toDayOfWeek(event_time) BETWEEN 1 AND 5)"
            invalid_row_count_expr = "countIf(toDayOfWeek(event_time) NOT BETWEEN 1 AND 5)"
            available_from_expr = "minIf(event_time, toDayOfWeek(event_time) BETWEEN 1 AND 5)"
            available_to_expr = "maxIf(event_time, toDayOfWeek(event_time) BETWEEN 1 AND 5)"
        else:
            row_count_expr = "countIf(toDayOfWeek(event_time) BETWEEN 1 AND 5)"
            invalid_row_count_expr = "countIf(toDayOfWeek(event_time) NOT BETWEEN 1 AND 5)"
            available_from_expr = "minIf(event_time, toDayOfWeek(event_time) BETWEEN 1 AND 5)"
            available_to_expr = "maxIf(event_time, toDayOfWeek(event_time) BETWEEN 1 AND 5)"

        source_query = self.latest_chart_candles_source(f"""
            symbol = {{symbol:String}}
            AND {interval_filter}
            {bucket_policy_filter}
        """, include_live=include_live_stored_candles(stored_interval))
        query = f"""
        SELECT
          {row_count_expr} AS rowCount,
          {invalid_row_count_expr} AS invalidRowCount,
          formatDateTime({available_from_expr}, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS availableFrom,
          formatDateTime({available_to_expr}, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS availableTo
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
        """gapfill 비교에 사용할 저장 candle timestamp 목록을 조회합니다."""
        interval = normalize_chart_interval(interval)
        stored_interval = interval
        interval_filter = "interval IN ('1D', '1d')" if stored_interval == "1D" else "interval = {interval:String}"
        params = {
            "symbol": symbol,
            "from": from_time,
            "to": to_time,
            "limit": int(limit),
        }
        if stored_interval != "1D":
            params["interval"] = stored_interval
        session_filter = "1 = 1" if stored_interval == "1D" else self.market_session_filter_sql(symbol)
        source_query = self.latest_chart_candles_source(f"""
            symbol = {{symbol:String}}
            AND {interval_filter}
            AND event_time >= parseDateTimeBestEffort({{from:String}})
            AND event_time < parseDateTimeBestEffort({{to:String}})
        """, include_live=include_live_stored_candles(stored_interval))
        query = f"""
        SELECT
          formatDateTime(event_time, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS timestamp
        FROM (
          {source_query}
        )
        WHERE {session_filter}
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

    def rank_symbols(self, symbols, kind="dollar-volume", limit=10):
        symbols = [symbol for symbol in symbols if isinstance(symbol, str) and symbol.strip()]
        if not symbols:
            return []
        rows = self._rank_symbols_by_interval(symbols, kind=kind, limit=limit, interval="1m")
        if rows:
            return rows
        return self._rank_symbols_by_interval(symbols, kind=kind, limit=limit, interval="1D")

    def latest_quotes(self, symbols, limit=None):
        symbols = list(dict.fromkeys(symbol for symbol in symbols if isinstance(symbol, str) and symbol.strip()))
        if not symbols:
            return []
        limit = len(symbols) if limit is None else max(0, int(limit))
        if limit == 0:
            return []

        rows = self._latest_quotes_by_interval(symbols, limit=limit, interval="1m")
        by_symbol = {
            str(row.get("symbol")).strip().upper(): row
            for row in rows
            if isinstance(row, dict) and row.get("symbol")
        }
        remaining = max(0, limit - len(by_symbol))
        missing_symbols = [symbol for symbol in symbols if symbol.strip().upper() not in by_symbol]
        if missing_symbols and remaining:
            fallback_rows = self._latest_quotes_by_interval(missing_symbols, limit=remaining, interval="1D")
            for row in fallback_rows:
                symbol = str(row.get("symbol") or "").strip().upper()
                if symbol and symbol not in by_symbol:
                    by_symbol[symbol] = row
        return [by_symbol[symbol.strip().upper()] for symbol in symbols if symbol.strip().upper() in by_symbol][:limit]

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
          AND {self.canonical_candle_filter_sql(include_live=normalized_interval == "1m")}
          AND event_time >= subtractDays(now(), {{lookbackDays:UInt32}})
          AND toDayOfWeek(event_time) BETWEEN 1 AND 5
        """
        session_source_query = self.latest_chart_candles_source(f"""
            symbol IN {{symbols:Array(String)}}
            AND {interval_filter}
            AND event_time >= subtractDays(now(), {{lookbackDays:UInt32}})
            AND toDate(event_time) = latest_session_date
            AND toDayOfWeek(event_time) BETWEEN 1 AND 5
        """, include_live=normalized_interval == "1m")
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

    def _rank_symbols_by_interval(self, symbols, kind="dollar-volume", limit=10, interval="1m"):
        normalized_interval = "1D" if interval in {"1D", "1d"} else "1m"
        interval_filter = "interval IN ('1D', '1d')" if normalized_interval == "1D" else "interval = '1m'"
        normalized_kind = str(kind or "dollar-volume").strip().lower().replace("_", "-")
        order_by = {
            "dollar-volume": "sessionDollarVolume DESC, symbol ASC",
            "volume": "volume DESC, symbol ASC",
            "gainers": "ifNull(changePercent, -1000000) DESC, symbol ASC",
            "losers": "ifNull(changePercent, 1000000) ASC, symbol ASC",
        }.get(normalized_kind)
        if not order_by:
            return []
        try:
            lookback_days = max(1, int(os.getenv("HOT_TIER_LOOKBACK_DAYS", "14")))
        except ValueError:
            lookback_days = 14
        latest_source_query = f"""
        SELECT event_time
        FROM {self.table('chart_candles')}
        WHERE symbol IN {{symbols:Array(String)}}
          AND {interval_filter}
          AND {self.canonical_candle_filter_sql(include_live=normalized_interval == "1m")}
          AND event_time >= subtractDays(now(), {{lookbackDays:UInt32}})
          AND toDayOfWeek(event_time) BETWEEN 1 AND 5
        """
        session_source_query = self.latest_chart_candles_source(f"""
            symbol IN {{symbols:Array(String)}}
            AND {interval_filter}
            AND event_time >= subtractDays(now(), {{lookbackDays:UInt32}})
            AND toDate(event_time) = latest_session_date
            AND toDayOfWeek(event_time) BETWEEN 1 AND 5
        """, include_live=normalized_interval == "1m")
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
          {clickhouse_string_literal(f'clickhouse_{normalized_interval}_{normalized_kind}_rank')} AS rankReason
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
        ORDER BY {order_by}
        LIMIT {{limit:UInt32}}
        FORMAT JSONEachRow
        """
        return self.query_json_each_row(query, {"symbols": symbols, "limit": int(limit), "lookbackDays": lookback_days})

    def _latest_quotes_by_interval(self, symbols, limit=None, interval="1m"):
        normalized_interval = "1D" if interval in {"1D", "1d"} else "1m"
        interval_filter = "interval IN ('1D', '1d')" if normalized_interval == "1D" else "interval = '1m'"
        limit = len(symbols) if limit is None else int(limit)
        try:
            lookback_days = max(1, int(os.getenv("HOT_TIER_LOOKBACK_DAYS", "14")))
        except ValueError:
            lookback_days = 14
        latest_source_query = f"""
        SELECT
          symbol,
          event_time
        FROM {self.table('chart_candles')}
        WHERE symbol IN {{symbols:Array(String)}}
          AND {interval_filter}
          AND {self.canonical_candle_filter_sql(include_live=normalized_interval == "1m")}
          AND event_time >= subtractDays(now(), {{lookbackDays:UInt32}})
          AND toDayOfWeek(event_time) BETWEEN 1 AND 5
        """
        session_source_query = self.latest_chart_candles_source(f"""
            symbol IN {{symbols:Array(String)}}
            AND {interval_filter}
            AND event_time >= subtractDays(now(), {{lookbackDays:UInt32}})
            AND toDayOfWeek(event_time) BETWEEN 1 AND 5
        """, include_live=normalized_interval == "1m")
        query = f"""
        WITH latest_sessions AS (
          SELECT
            symbol,
            max(toDate(event_time)) AS latest_session_date
          FROM ({latest_source_query})
          GROUP BY symbol
        )
        SELECT
          c.symbol AS symbol,
          argMax(c.close, c.event_time) AS lastPrice,
          if(argMin(c.open, c.event_time) = 0, NULL, round(((argMax(c.close, c.event_time) - argMin(c.open, c.event_time)) / argMin(c.open, c.event_time)) * 100, 2)) AS changePercent,
          sum(toFloat64(c.volume) * c.close) AS sessionDollarVolume,
          sum(c.volume) AS volume,
          formatDateTime(max(c.event_time), '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS sourceUpdatedAt,
          {clickhouse_string_literal('latest_available_session')} AS rankingWindow,
          {clickhouse_string_literal(f'clickhouse_{normalized_interval}_latest_quote')} AS rankReason
        FROM ({session_source_query}) AS c
        INNER JOIN latest_sessions AS latest
          ON c.symbol = latest.symbol
         AND toDate(c.event_time) = latest.latest_session_date
        GROUP BY c.symbol
        ORDER BY c.symbol ASC
        LIMIT {{limit:UInt32}}
        FORMAT JSONEachRow
        """
        return self.query_json_each_row(query, {"symbols": symbols, "limit": limit, "lookbackDays": lookback_days})

    def latest_chart_candles_source(self, where_sql, *, include_live=False):
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
          price_adjustment,
          canonical_version,
          bucket_policy,
          source_event_id,
          inserted_at
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
            price_adjustment,
            canonical_version,
            bucket_policy,
            source_event_id,
            inserted_at,
            row_number() OVER (
              PARTITION BY symbol, if(interval = '1d', '1D', interval), event_time
              ORDER BY
                multiIf(price_adjustment = 'split', 1, 0) DESC,
                inserted_at DESC,
                ifNull(source_event_id, '') DESC
            ) AS rn
          FROM {self.table('chart_candles')}
          WHERE {where_sql}
            AND {self.canonical_candle_filter_sql(include_live=include_live)}
        )
        WHERE rn = 1
        """

    def latest_canonical_daily_source(self, where_sql):
        """Choose one canonical 1D row per NY trading-date identity."""
        source = self.latest_chart_candles_source(where_sql)
        return f"""
        SELECT
          toDateTime(trading_date, 'America/New_York') AS event_time,
          symbol,
          '1D' AS interval,
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
          'regular' AS market_session,
          price_adjustment,
          canonical_version,
          source_event_id,
          inserted_at
        FROM (
          SELECT
            normalized.*,
            row_number() OVER (
              PARTITION BY symbol, trading_date
              ORDER BY inserted_at DESC, event_time DESC, ifNull(source_event_id, '') DESC
            ) AS daily_rn
          FROM (
            SELECT
              latest.*,
              if(
                formatDateTime(event_time, '%H:%i:%S', 'UTC') = '00:00:00',
                toDate(event_time, 'UTC'),
                toDate(event_time, 'America/New_York')
              ) AS trading_date
            FROM (
              {source}
            ) AS latest
          ) AS normalized
        )
        WHERE daily_rn = 1
        """

    def order_flow_daily_profiles(self, symbol, from_date, to_date, limit=100000):
        query = f"""
        SELECT
          toString(session_date) AS sessionDate,
          price_bin AS priceBin,
          price_bin_size AS priceBinSize,
          ask_volume AS askVolume,
          bid_volume AS bidVolume,
          unknown_volume AS unknownVolume,
          ask_trade_count AS askTradeCount,
          bid_trade_count AS bidTradeCount,
          unknown_trade_count AS unknownTradeCount,
          trade_count AS tradeCount,
          volume
        FROM {self.table('order_flow_profile_daily')} FINAL
        WHERE symbol = {{symbol:String}}
          AND session_date >= toDate({{fromDate:String}})
          AND session_date <= toDate({{toDate:String}})
        ORDER BY session_date DESC, price_bin ASC
        LIMIT {{limit:UInt32}}
        FORMAT JSONEachRow
        """
        return self.query_json_each_row(query, {
            "symbol": symbol,
            "fromDate": from_date,
            "toDate": to_date,
            "limit": int(limit),
        })

    def ensure_market_data_schema(self):
        """조회 전에 필요한 ClickHouse 컬럼과 타입이 준비되어 있는지 보정합니다."""
        for table in ("trade_ticks", "chart_candles", "market_status_events"):
            self.execute(
                f"ALTER TABLE {self.table(table)} "
                "ADD COLUMN IF NOT EXISTS feed_profile LowCardinality(String) DEFAULT feed AFTER feed, "
                "ADD COLUMN IF NOT EXISTS market_session LowCardinality(String) DEFAULT 'unknown' AFTER feed_profile"
            )
        self.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.table('news_company_daily_summaries')}
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
            f"""
            CREATE TABLE IF NOT EXISTS {self.table('order_flow_profile_daily')}
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
            """
        )
        self.execute(
            f"ALTER TABLE {self.table('news_article_localizations')} "
            "ADD COLUMN IF NOT EXISTS target_symbol LowCardinality(String) DEFAULT symbol AFTER symbols, "
            "ADD COLUMN IF NOT EXISTS subject_relevance LowCardinality(String) DEFAULT 'mention' AFTER target_symbol, "
            "ADD COLUMN IF NOT EXISTS relevance_score_v2 Float32 DEFAULT 0 AFTER subject_relevance, "
            "ADD COLUMN IF NOT EXISTS relevance_reason String DEFAULT '' AFTER relevance_score_v2, "
            "ADD COLUMN IF NOT EXISTS direct_signals Array(String) DEFAULT [] AFTER relevance_reason"
        )
        self.execute(
            f"ALTER TABLE {self.table('chart_candles')} "
            "ADD COLUMN IF NOT EXISTS price_adjustment LowCardinality(String) DEFAULT 'unknown' AFTER market_session, "
            "ADD COLUMN IF NOT EXISTS canonical_version LowCardinality(String) DEFAULT 'legacy' AFTER price_adjustment, "
            "ADD COLUMN IF NOT EXISTS bucket_policy LowCardinality(String) DEFAULT 'clock_aligned' AFTER canonical_version"
        )
        self.execute(
            f"ALTER TABLE {self.table('chart_candles')} "
            "ADD COLUMN IF NOT EXISTS bucket_policy_key LowCardinality(String) AFTER bucket_policy, "
            "MODIFY ORDER BY (symbol, interval, event_time, feed_profile, market_session, bucket_policy_key)"
        )
        # Crypto 체결/거래량은 소수 단위가 자연스럽기 때문에 조회 스키마도 Float64로 맞춥니다.
        for table, column, column_type in (
            ("trade_ticks", "size", "Nullable(Float64)"),
            ("quote_ticks", "bid_size", "Nullable(Float64)"),
            ("quote_ticks", "ask_size", "Nullable(Float64)"),
            ("chart_candles", "volume", "Float64"),
        ):
            self.execute(f"ALTER TABLE {self.table(table)} MODIFY COLUMN IF EXISTS {column} {column_type}")

    def market_session_filter_sql(self, symbol, column="event_time", now=None):
        """주식은 정규장과 현재 live extended window만, crypto는 24/7 캔들을 통과시킵니다."""
        if is_crypto_symbol(symbol):
            return "1 = 1"
        windows = visible_extended_session_windows(now)
        if not windows:
            return "market_session = 'regular'"
        clauses = ["market_session = 'regular'"]
        for session, start, end in windows:
            start_literal = clickhouse_string_literal(start.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
            end_literal = clickhouse_string_literal(end.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
            session_literal = clickhouse_string_literal(session)
            clauses.append(
                f"(market_session = {session_literal} "
                f"AND {column} >= parseDateTime64BestEffort({start_literal}) "
                f"AND {column} < parseDateTime64BestEffort({end_literal}))"
            )
        return f"({' OR '.join(clauses)})"

    def canonical_candle_filter_sql(self, *, include_live=False):
        if os.getenv("CLICKHOUSE_REQUIRE_CANONICAL_CANDLES", "true").lower() not in {"1", "true", "yes"}:
            return "1 = 1"
        allowed_adjustments = SERVING_PRICE_ADJUSTMENTS if include_live else HISTORICAL_SERVING_PRICE_ADJUSTMENTS
        adjustments = ", ".join(clickhouse_string_literal(value) for value in allowed_adjustments)
        return f"canonical_version = {clickhouse_string_literal(CANONICAL_VERSION)} AND price_adjustment IN ({adjustments})"

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

    def news_articles(self, symbol, limit=10, days=7):
        query = f"""
        SELECT
          formatDateTime(published_at, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS publishedAt,
          symbol,
          article_id AS articleId,
          headline,
          summary,
          content,
          url,
          source,
          author,
          formatDateTime(updated_at, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS updatedAt,
          formatDateTime(received_at, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS receivedAt
        FROM {self.table('news_articles')}
        WHERE symbol = {{symbol:String}}
          AND published_at >= now64(3) - INTERVAL {{days:UInt32}} DAY
        ORDER BY published_at DESC, inserted_at DESC
        LIMIT {{limit:UInt32}}
        FORMAT JSONEachRow
        """
        return self.query_json_each_row(query, {"symbol": symbol, "limit": int(limit), "days": int(days)})

    def localized_news_articles(self, symbol, limit=10, days=7, locale="ko-KR"):
        return self.localized_news_articles_for_symbols([symbol], limit=limit, days=days, locale=locale)

    def localized_news_articles_for_symbols(self, symbols, limit=10, days=7, locale="ko-KR"):
        normalized_symbols = []
        for value in symbols or []:
            symbol = str(value or "").strip().upper()
            if symbol and symbol not in normalized_symbols:
                normalized_symbols.append(symbol)
        if not normalized_symbols:
            return []
        direct_rows = self.localized_news_articles_for_symbols_by_relevance(
            normalized_symbols,
            limit=limit,
            days=days,
            locale=locale,
            relevance_levels=["primary", "secondary"],
        )
        if direct_rows:
            return direct_rows
        return self.localized_news_articles_for_symbols_by_relevance(
            normalized_symbols,
            limit=min(int(limit), 3),
            days=days,
            locale=locale,
            relevance_levels=["mention"],
        )

    def localized_news_articles_for_symbols_by_relevance(self, symbols, limit=10, days=7, locale="ko-KR", relevance_levels=None):
        normalized_symbols = []
        for value in symbols or []:
            symbol = str(value or "").strip().upper()
            if symbol and symbol not in normalized_symbols:
                normalized_symbols.append(symbol)
        if not normalized_symbols:
            return []
        levels = [str(level or "").strip().lower() for level in (relevance_levels or ["primary", "secondary"]) if str(level or "").strip()]
        query = f"""
        SELECT
          formatDateTime(published_at, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS publishedAt,
          symbol,
          article_id AS articleId,
          locale,
          symbols,
          target_symbol AS targetSymbol,
          subject_relevance AS subjectRelevance,
          relevance_score_v2 AS relevanceScoreV2,
          relevance_reason AS relevanceReason,
          direct_signals AS directSignals,
          headline,
          summary,
          url,
          source,
          localized_headline AS localizedHeadline,
          localized_summary AS localizedSummary,
          key_points AS keyPoints,
          positive_points AS positivePoints,
          concerns,
          event_type AS eventType,
          sentiment,
          impact_direction AS impactDirection,
          why_it_matters AS whyItMatters,
          model,
          formatDateTime(localized_at, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS localizedAt
        FROM {self.table('news_article_localizations')}
        WHERE target_symbol IN {{symbols:Array(String)}}
          AND locale = {{locale:String}}
          AND subject_relevance IN {{relevanceLevels:Array(String)}}
          AND published_at >= now64(3) - INTERVAL {{days:UInt32}} DAY
        ORDER BY relevance_score_v2 DESC, published_at DESC, localized_at DESC
        LIMIT {{limit:UInt32}}
        FORMAT JSONEachRow
        """
        return self.query_json_each_row(
            query,
            {
                "symbols": normalized_symbols,
                "relevanceLevels": levels,
                "locale": locale,
                "limit": int(limit),
                "days": int(days),
            },
        )

    def company_daily_news_summaries(self, symbol, limit=5, days=30, locale="ko-KR"):
        query = f"""
        SELECT
          toString(date) AS date,
          symbol,
          locale,
          summary,
          key_points AS keyPoints,
          positive_points AS positivePoints,
          concerns,
          impact_direction AS impactDirection,
          sentiment,
          article_ids AS articleIds,
          article_ids_hash AS articleIdsHash,
          article_count AS articleCount,
          mention_count AS mentionCount,
          status,
          model,
          formatDateTime(generated_at, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS generatedAt,
          version,
          raw
        FROM {self.table('news_company_daily_summaries')}
        WHERE symbol = {{symbol:String}}
          AND locale = {{locale:String}}
          AND date >= toDate(now('UTC') - INTERVAL {{days:UInt32}} DAY)
        ORDER BY date DESC, generated_at DESC
        LIMIT {{limit:UInt32}}
        FORMAT JSONEachRow
        """
        return self.query_json_each_row(
            query,
            {
                "symbol": str(symbol or "").strip().upper(),
                "locale": locale,
                "limit": int(limit),
                "days": int(days),
            },
        )

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

        timeout = float(os.getenv("CLICKHOUSE_PROVIDER_TIMEOUT_SECONDS", "8"))
        attempts = max(1, int(os.getenv("CLICKHOUSE_PROVIDER_RETRY_ATTEMPTS", "2")))
        response = None
        for attempt in range(attempts):
            try:
                response = requests.post(
                    self.url,
                    params=params,
                    timeout=timeout,
                )
                break
            except requests.exceptions.Timeout:
                if attempt >= attempts - 1:
                    raise
                time.sleep(min(0.25, 0.05 * (attempt + 1)))
        if response is None:
            raise RuntimeError("ClickHouse query failed without a response")
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


def include_live_stored_candles(interval):
    """Serve closed realtime 1m bars without treating live daily/derived rows as historical."""
    return normalize_chart_interval(interval) == "1m"


def merge_candle_rows(*groups, interval=None):
    """같은 candle identity는 뒤쪽 그룹 값을 우선하는 방식으로 합칩니다."""
    normalized_interval = normalize_chart_interval(interval) if interval else None
    by_timestamp = {}
    for group in groups:
        for row in group or []:
            normalized = row
            identity_key = None
            if normalized_interval in {"1D", "1W", "1M"}:
                from alfaka.analytics.analysis_candles import canonicalize_candle_identity

                normalized = canonicalize_candle_identity(row, normalized_interval)
                identity_key = normalized.get("candleKey") if normalized else None
            timestamp = normalized.get("timestamp") if normalized else None
            if not timestamp:
                continue
            key = identity_key or timestamp
            by_timestamp[key] = {**normalized, "timestamp": timestamp}
    return sorted(by_timestamp.values(), key=lambda row: row["timestamp"])


def with_higher_timeframe_closed_state(rows, interval, *, now=None):
    normalized_interval = normalize_chart_interval(interval)
    if normalized_interval not in {"1W", "1M"}:
        return list(rows or [])
    from alfaka.analytics.analysis_candles import is_analysis_candle_bucket_complete

    reference = now or datetime.now(timezone.utc)
    return [
        {
            **row,
            "isClosed": is_analysis_candle_bucket_complete(
                row.get("timestamp"),
                normalized_interval,
                now=reference,
            ),
        }
        for row in rows or []
    ]


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
