from __future__ import annotations

import argparse
import json
import os
import time

from alfaka.common.env import load_dotenv
from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider, clickhouse_identifier


def main() -> None:
    load_dotenv()
    args = parse_args()
    provider = ClickHouseMarketDataProvider(
        url=args.clickhouse_url or os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
        database=args.database or os.getenv("CLICKHOUSE_DATABASE", "market_data"),
        user=args.user or os.getenv("CLICKHOUSE_USER", "alfaka"),
        password=args.password or os.getenv("CLICKHOUSE_PASSWORD", "alfaka"),
    )
    table = provider.table("chart_candles")
    symbols = parse_symbols(args.symbols)
    before = query_summary(provider, table, symbols)
    result = {"apply": args.apply, "before": before}
    if args.apply and int(before.get("nonMidnightRows") or 0) > 0:
        provider.execute(canonical_insert_sql(table, symbols))
        provider.execute(delete_non_midnight_sql(table, symbols))
        if args.wait:
            wait_for_mutations(provider, provider.database, "chart_candles", timeout_seconds=args.wait_timeout)
        result["after"] = query_summary(provider, table, symbols)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repair legacy 1D ClickHouse candle rows stored at New York midnight offsets."
    )
    parser.add_argument("--apply", action="store_true", help="Insert canonical UTC-midnight rows and delete legacy rows.")
    parser.add_argument("--symbols", default="", help="Optional comma-separated symbol allowlist.")
    parser.add_argument("--wait", action="store_true", help="Wait for ClickHouse DELETE mutations to finish.")
    parser.add_argument("--wait-timeout", type=float, default=120.0)
    parser.add_argument("--clickhouse-url", default=None)
    parser.add_argument("--database", default=None)
    parser.add_argument("--user", default=None)
    parser.add_argument("--password", default=None)
    return parser.parse_args()


def parse_symbols(value: str) -> list[str]:
    return [item.strip().upper() for item in (value or "").split(",") if item.strip()]


def query_summary(provider: ClickHouseMarketDataProvider, table: str, symbols: list[str]) -> dict:
    rows = provider.query_json_each_row(summary_sql(table, symbols), {"symbols": symbols})
    return rows[0] if rows else {}


def summary_sql(table: str, symbols: list[str]) -> str:
    symbol_filter = symbol_filter_sql(symbols)
    return f"""
    SELECT
      count() AS nonMidnightRows,
      uniqExact(symbol) AS affectedSymbols,
      min(formatDateTime(event_time, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC')) AS minTimestamp,
      max(formatDateTime(event_time, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC')) AS maxTimestamp
    FROM {table}
    WHERE {non_midnight_daily_predicate()}
      {symbol_filter}
    FORMAT JSONEachRow
    """


def canonical_insert_sql(table: str, symbols: list[str]) -> str:
    symbol_filter = symbol_filter_sql(symbols)
    return f"""
    INSERT INTO {table}
    (
      event_time,
      symbol,
      interval,
      open,
      high,
      low,
      close,
      volume,
      trade_count,
      vwap,
      ma5,
      ma20,
      ma60,
      is_closed,
      correction_type,
      source,
      feed,
      feed_profile,
      market_session,
      source_event_id,
      created_at
    )
    SELECT
      parseDateTime64BestEffort(concat(toString(bucket_date), 'T00:00:00.000Z')) AS event_time,
      symbol,
      '1D' AS interval,
      tupleElement(latest, 1) AS open,
      tupleElement(latest, 2) AS high,
      tupleElement(latest, 3) AS low,
      tupleElement(latest, 4) AS close,
      tupleElement(latest, 5) AS volume,
      tupleElement(latest, 6) AS trade_count,
      tupleElement(latest, 7) AS vwap,
      tupleElement(latest, 8) AS ma5,
      tupleElement(latest, 9) AS ma20,
      tupleElement(latest, 10) AS ma60,
      tupleElement(latest, 11) AS is_closed,
      tupleElement(latest, 12) AS correction_type,
      tupleElement(latest, 13) AS source,
      tupleElement(latest, 14) AS feed,
      feed_profile,
      market_session,
      tupleElement(latest, 15) AS source_event_id,
      tupleElement(latest, 16) AS created_at
    FROM (
      SELECT
        symbol,
        toDate(event_time) AS bucket_date,
        feed_profile,
        market_session,
        argMax(
          tuple(
            open,
            high,
            low,
            close,
            volume,
            trade_count,
            vwap,
            ma5,
            ma20,
            ma60,
            is_closed,
            correction_type,
            source,
            feed,
            source_event_id,
            created_at
          ),
          inserted_at
        ) AS latest
      FROM {table}
      WHERE {non_midnight_daily_predicate()}
        {symbol_filter}
      GROUP BY symbol, bucket_date, feed_profile, market_session
    )
    """


def delete_non_midnight_sql(table: str, symbols: list[str]) -> str:
    symbol_filter = symbol_filter_sql(symbols)
    return f"""
    ALTER TABLE {table}
    DELETE WHERE {non_midnight_daily_predicate()}
      {symbol_filter}
    """


def non_midnight_daily_predicate() -> str:
    return "interval IN ('1D', '1d') AND formatDateTime(event_time, '%H:%i:%S', 'UTC') != '00:00:00'"


def symbol_filter_sql(symbols: list[str]) -> str:
    return "AND symbol IN {symbols:Array(String)}" if symbols else ""


def wait_for_mutations(provider: ClickHouseMarketDataProvider, database: str, table: str, timeout_seconds: float) -> None:
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    database = clickhouse_identifier(database)
    table = clickhouse_identifier(table)
    while time.monotonic() < deadline:
        rows = provider.query_json_each_row(
            f"""
            SELECT count() AS pending
            FROM system.mutations
            WHERE database = {{database:String}}
              AND table = {{table:String}}
              AND is_done = 0
            FORMAT JSONEachRow
            """,
            {"database": database, "table": table},
        )
        pending = int((rows[0] if rows else {}).get("pending") or 0)
        if pending <= 0:
            return
        time.sleep(0.5)
    raise TimeoutError("Timed out waiting for ClickHouse mutations to finish.")


if __name__ == "__main__":
    main()
