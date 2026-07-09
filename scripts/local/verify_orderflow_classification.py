#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path


repo_root = Path(__file__).resolve().parents[2]
shared_path = repo_root / "systems" / "market-data" / "shared"
if str(shared_path) not in sys.path:
    sys.path.insert(0, str(shared_path))

from alfaka.orderflow.config import price_bin_size_from_env
from alfaka.orderflow.rollup import (  # noqa: E402
    OrderFlowDailyAggregate,
    RecentIdDeduper,
    accumulate_order_flow,
    dedupe_rows,
    hourly_windows,
    normalize_quote_tick_row,
    normalize_trade_tick_row,
    query_quote_rows,
    query_trade_rows,
    regular_session_bounds_utc,
)
from alfaka.orderflow.classification import normalize_quotes, normalize_trades  # noqa: E402
from alfaka.storage.clickhouse_loader import ClickHouseHttpClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Report order-flow side-classification quality for a local ClickHouse symbol-day.")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--date", required=True, help="Session date in YYYY-MM-DD.")
    parser.add_argument("--price-bin-size", type=float, default=price_bin_size_from_env())
    args = parser.parse_args()

    try:
        payload = classify_day(args.symbol, args.date, args.price_bin_size)
        print_warnings(payload)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    except Exception as exc:
        print(f"ERROR order-flow classification verification failed: {exc}", file=sys.stderr)
        print(json.dumps({"symbol": args.symbol.upper(), "sessionDate": args.date, "error": str(exc)}, sort_keys=True))
    return 0


def classify_day(symbol: str, session_date: str, price_bin_size: float) -> dict[str, object]:
    from datetime import date

    client = clickhouse_client_from_env()
    session_day = date.fromisoformat(session_date)
    bounds = regular_session_bounds_utc(session_day)
    if bounds is None:
        return empty_payload(symbol, session_date)
    session_start, session_end, quote_warmup_start = bounds
    aggregate = OrderFlowDailyAggregate(symbol.upper(), session_day, price_bin_size)
    first_hour = OrderFlowDailyAggregate(symbol.upper(), session_day, price_bin_size)
    trade_deduper = RecentIdDeduper()
    quote_deduper = RecentIdDeduper()
    duplicate_count = 0
    carry_quote = None
    first_hour_end = session_start + timedelta(hours=1)
    started = time.monotonic()

    for win_start, win_end in hourly_windows(session_start, session_end):
        quote_start = quote_warmup_start if win_start == session_start else win_start
        trade_rows = query_trade_rows(client, symbol.upper(), win_start, win_end)
        quote_rows = query_quote_rows(client, symbol.upper(), quote_start, win_end)
        trade_rows, trade_dupes = dedupe_rows(trade_rows, trade_deduper)
        quote_rows, quote_dupes = dedupe_rows(quote_rows, quote_deduper)
        duplicate_count += trade_dupes + quote_dupes
        trades = normalize_trades([normalize_trade_tick_row(row) for row in trade_rows])
        quotes = normalize_quotes([normalize_quote_tick_row(row) for row in quote_rows])
        carry_quote = accumulate_order_flow(aggregate, trades, quotes, initial_quote=carry_quote)
        if win_start < first_hour_end:
            first_trades = [trade for trade in trades if trade.get("_time") and trade["_time"] < first_hour_end]
            first_quotes = [quote for quote in quotes if quote.get("_time") and quote["_time"] < first_hour_end]
            accumulate_order_flow(first_hour, first_trades, first_quotes)

    side_total = max(1, aggregate.trade_count)
    first_total = max(1, first_hour.trade_count)
    return {
        "symbol": symbol.upper(),
        "sessionDate": session_date,
        "tradeCount": aggregate.trade_count,
        "quoteCount": aggregate.quote_count,
        "duplicateCount": duplicate_count,
        "askShare": aggregate.side_counts.get("ask", 0) / side_total,
        "bidShare": aggregate.side_counts.get("bid", 0) / side_total,
        "unknownShare": aggregate.side_counts.get("unknown", 0) / side_total,
        "firstHourUnknownShare": first_hour.side_counts.get("unknown", 0) / first_total,
        "durationMs": int((time.monotonic() - started) * 1000),
    }


def clickhouse_client_from_env() -> ClickHouseHttpClient:
    return ClickHouseHttpClient(
        url=os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
        database=os.getenv("CLICKHOUSE_DATABASE", "market_data"),
        user=os.getenv("CLICKHOUSE_USER", "alfaka"),
        password=os.getenv("CLICKHOUSE_PASSWORD", "alfaka"),
    )


def empty_payload(symbol: str, session_date: str) -> dict[str, object]:
    return {
        "symbol": symbol.upper(),
        "sessionDate": session_date,
        "tradeCount": 0,
        "quoteCount": 0,
        "duplicateCount": 0,
        "askShare": 0.0,
        "bidShare": 0.0,
        "unknownShare": 0.0,
        "firstHourUnknownShare": 0.0,
    }


def print_warnings(payload: dict[str, object]) -> None:
    unknown = float(payload.get("unknownShare") or 0.0)
    first_hour = float(payload.get("firstHourUnknownShare") or 0.0)
    if unknown > 0.05:
        print(f"WARN unknownShare={unknown:.4f} exceeds 0.05; inspect quote feed coverage.", file=sys.stderr)
    if first_hour > 0.15:
        print(f"WARN firstHourUnknownShare={first_hour:.4f} exceeds 0.15; inspect NBBO warmup/staleness.", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
