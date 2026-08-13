"""Dry-run audit for duplicate or non-canonical chart candle rows."""

import json
import os

from market_data.common.canonical import CANONICAL_VERSION, HISTORICAL_SERVING_PRICE_ADJUSTMENTS
from market_data.common.env import load_dotenv
from market_data.serving.intervals import normalize_chart_interval
from market_data.storage.clickhouse_loader import (
    ClickHouseHttpClient,
    clickhouse_identifier,
    clickhouse_string_literal,
    should_ensure_schema_on_start,
)


def canonical_candle_audit_query(database="market_data", symbol=None, interval=None, limit=100):
    table = f"{clickhouse_identifier(database)}.chart_candles"
    where = ["1 = 1"]
    if symbol:
        where.append(f"symbol = {clickhouse_string_literal(str(symbol).upper())}")
    if interval:
        normalized_interval = normalize_chart_interval(interval)
        if normalized_interval == "1D":
            where.append("interval IN ('1D', '1d')")
        else:
            where.append(f"interval = {clickhouse_string_literal(normalized_interval)}")

    canonical_filter = canonical_filter_sql()
    return f"""
    SELECT
      symbol,
      if(interval = '1d', '1D', interval) AS interval,
      formatDateTime(event_time, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS timestamp,
      count() AS rowCount,
      countIf({canonical_filter}) AS canonicalRowCount,
      countIf(NOT ({canonical_filter})) AS nonCanonicalRowCount,
      countIf(open <= 0 OR high < low OR high < greatest(open, close) OR low > least(open, close)) AS invalidOhlcCount,
      groupUniqArray(price_adjustment) AS priceAdjustments,
      groupUniqArray(canonical_version) AS canonicalVersions,
      groupUniqArray(source) AS sources
    FROM {table}
    WHERE {' AND '.join(where)}
    GROUP BY symbol, interval, event_time
    HAVING rowCount > 1 OR nonCanonicalRowCount > 0 OR invalidOhlcCount > 0
    ORDER BY event_time DESC, symbol ASC, interval ASC
    LIMIT {int(limit)}
    FORMAT JSONEachRow
    """


def canonical_filter_sql():
    adjustments = ", ".join(clickhouse_string_literal(value) for value in HISTORICAL_SERVING_PRICE_ADJUSTMENTS)
    return f"canonical_version = {clickhouse_string_literal(CANONICAL_VERSION)} AND price_adjustment IN ({adjustments})"


def run_audit(client, database="market_data", symbol=None, interval=None, limit=100):
    return client.query_json_each_row(canonical_candle_audit_query(database=database, symbol=symbol, interval=interval, limit=limit))


def main():
    load_dotenv()
    client = ClickHouseHttpClient(
        url=os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
        database=os.getenv("CLICKHOUSE_DATABASE", "market_data"),
        user=os.getenv("CLICKHOUSE_USER", "alfaka"),
        password=os.getenv("CLICKHOUSE_PASSWORD", "alfaka"),
    )
    if should_ensure_schema_on_start():
        client.ensure_market_data_schema()
    rows = run_audit(
        client,
        database=os.getenv("CLICKHOUSE_DATABASE", "market_data"),
        symbol=os.getenv("CANONICAL_AUDIT_SYMBOL"),
        interval=os.getenv("CANONICAL_AUDIT_INTERVAL"),
        limit=int(os.getenv("CANONICAL_AUDIT_LIMIT", "100")),
    )
    print(json.dumps({"rowCount": len(rows), "rows": rows}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
