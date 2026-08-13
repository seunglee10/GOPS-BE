"""Bootstrap one year of canonical S&P 500 candles into ClickHouse.

The job is intentionally operator-run and dry-run by default.  It reuses the
same Alpaca normalization and ClickHouse row conversion functions as the live
storage path so historical rows keep the current ``chart_candles`` contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from market_data.backfill.gapfill import TradingCalendar, parse_time, to_iso
from market_data.backfill.runner import (
    fetch_alpaca_bars,
    historical_feed_for_symbol,
    raw_bar_to_processed_candle,
)
from market_data.common.env import load_dotenv, utc_now_iso
from market_data.serving.intervals import (
    INTRADAY_DERIVED_INTERVALS,
    INTRADAY_INTERVAL_MINUTES,
    alpaca_timeframe_for_interval,
    normalize_chart_interval,
)
from market_data.serving.moving_average import MA_WINDOWS, attach_moving_averages
from market_data.serving.session_buckets import aggregate_regular_session_candles
from market_data.storage.candle_validation import invalid_candle_reason
from market_data.storage.clickhouse_loader import (
    ClickHouseHttpClient,
    candle_to_clickhouse_row,
    should_ensure_schema_on_start,
)


DEFAULT_INTERVALS = ("1m", "5m", "10m", "1h", "4h", "1D", "1W", "1M")
INTRADAY_INTERVALS = frozenset({"1m", "5m", "10m", "1h", "4h"})
DEFAULT_ALLOWED_SESSIONS = frozenset({"pre", "regular", "after"})
CORE_CLICKHOUSE_COLUMNS = frozenset({
    "event_time",
    "symbol",
    "interval",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "ma5",
    "ma20",
    "ma60",
    "is_closed",
    "source",
    "feed",
    "feed_profile",
    "market_session",
    "price_adjustment",
    "canonical_version",
    "bucket_policy",
})
MARKET_TIMEZONE = ZoneInfo("America/New_York")
EXTENDED_SESSION_CLOSE = time(20, 0)
REGULAR_SESSION_MINUTES = 390
WARMUP_BARS = max(MA_WINDOWS) - 1


def parse_intervals(value: str | None) -> tuple[str, ...]:
    """Normalize CLI aliases such as ``1mo`` to the canonical ``1M`` value."""
    values = [item.strip() for item in (value or "").split(",") if item.strip()]
    if not values:
        values = list(DEFAULT_INTERVALS)
    result: list[str] = []
    for value_item in values:
        interval = normalize_chart_interval(value_item)
        if interval not in result:
            result.append(interval)
    return tuple(result)


def parse_symbols(value: str | None, *, universe_path: Path | None = None) -> tuple[str, ...]:
    """Read explicit symbols or the repository's canonical S&P 500 universe."""
    explicit = [item.strip().upper() for item in (value or "").split(",") if item.strip()]
    if explicit:
        return tuple(dict.fromkeys(explicit))
    path = universe_path or default_universe_path()
    payload = json.loads(path.read_text(encoding="utf-8"))
    symbols = [str(item).strip().upper() for item in payload.get("symbols") or [] if str(item).strip()]
    if not symbols:
        raise ValueError(f"No symbols were found in {path}")
    return tuple(dict.fromkeys(symbols))


def default_universe_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "sp500-universe.json"


def default_bootstrap_range(
    now: datetime | None = None,
    *,
    lookback_days: int = 365,
    calendar: TradingCalendar | None = None,
) -> tuple[str, str]:
    """Return a one-year range ending at the last completed 20:00 ET session."""
    if lookback_days <= 0:
        raise ValueError("lookback_days must be positive")
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    calendar = calendar or TradingCalendar.from_environment()
    local = reference.astimezone(MARKET_TIMEZONE)
    session_date = local.date()
    if local.time() < EXTENDED_SESSION_CLOSE or not calendar.is_session_date(session_date):
        session_date -= timedelta(days=1)
    while not calendar.is_session_date(session_date):
        session_date -= timedelta(days=1)
    end = datetime.combine(session_date, EXTENDED_SESSION_CLOSE, MARKET_TIMEZONE).astimezone(timezone.utc)
    start = end - timedelta(days=lookback_days)
    return to_iso(start), to_iso(end)


def resolve_bootstrap_range(
    *,
    start: str | None,
    end: str | None,
    lookback_days: int,
    now: datetime | None = None,
) -> tuple[str, str]:
    if start and end:
        start_dt, end_dt = parse_time(start), parse_time(end)
        if start_dt >= end_dt:
            raise ValueError("start must be earlier than end")
        return to_iso(start_dt), to_iso(end_dt)
    if start or end:
        raise ValueError("start and end must be provided together")
    return default_bootstrap_range(now, lookback_days=lookback_days)


def moving_average_warmup_start(interval: str, target_start: str) -> str:
    """Return a conservative fetch start that supplies 59 pre-range candles."""
    interval = normalize_chart_interval(interval)
    if interval in INTRADAY_INTERVALS:
        interval_minutes = INTRADAY_INTERVAL_MINUTES[interval]
        trading_days = math.ceil(WARMUP_BARS * interval_minutes / REGULAR_SESSION_MINUTES)
        calendar_days = math.ceil(trading_days * 7 / 5) + 14
    elif interval == "1D":
        calendar_days = math.ceil(WARMUP_BARS * 7 / 5) + 14
    elif interval == "1W":
        calendar_days = WARMUP_BARS * 7 + 21
    else:
        calendar_days = WARMUP_BARS * 31 + 62
    return to_iso(parse_time(target_start) - timedelta(days=calendar_days))


def prepare_clickhouse_rows(
    symbol: str,
    interval: str,
    raw_bars: Iterable[dict[str, Any]],
    *,
    feed: str,
    table_columns: set[str] | frozenset[str],
    received_at: str | None = None,
    allowed_sessions: frozenset[str] = DEFAULT_ALLOWED_SESSIONS,
    store_start: str | None = None,
    store_end: str | None = None,
) -> list[dict[str, Any]]:
    """Convert Alpaca bars through the existing canonical ClickHouse contract."""
    interval = normalize_chart_interval(interval)
    source_interval = "1m" if interval in INTRADAY_DERIVED_INTERVALS else interval
    received_at = received_at or utc_now_iso()
    source_candles = []
    for raw_bar in raw_bars:
        candle = raw_bar_to_processed_candle(
            symbol,
            raw_bar,
            feed=feed,
            received_at=received_at,
            interval=source_interval,
            price_adjustment="split",
        )
        if source_interval not in INTRADAY_INTERVALS:
            candle["marketSession"] = "regular"
        elif candle.get("marketSession") not in allowed_sessions:
            continue
        if invalid_candle_reason(candle):
            continue
        source_candles.append(candle)
    source_candles.sort(key=lambda candle: parse_time(candle["timestamp"]))
    if interval in INTRADAY_DERIVED_INTERVALS:
        aggregation_now = parse_time(store_end) if store_end else datetime.now(timezone.utc)
        candles = aggregate_regular_session_candles(source_candles, interval, now=aggregation_now)
    else:
        candles = source_candles
    store_start_time = parse_time(store_start) if store_start else None
    store_end_time = parse_time(store_end) if store_end else None
    rows = []
    for candle in attach_moving_averages(candles, overwrite=True):
        candle_time = parse_time(candle["timestamp"])
        if store_start_time is not None and candle_time < store_start_time:
            continue
        if store_end_time is not None and candle_time >= store_end_time:
            continue
        source_row = candle_to_clickhouse_row(candle)
        rows.append({key: value for key, value in source_row.items() if key in table_columns})
    return rows


def read_chart_candle_columns(client: ClickHouseHttpClient) -> set[str]:
    query = f"DESCRIBE TABLE {client.database}.chart_candles FORMAT JSONEachRow"
    columns = {str(row.get("name")) for row in client.query_json_each_row(query) if row.get("name")}
    missing = CORE_CLICKHOUSE_COLUMNS.difference(columns)
    if missing:
        raise RuntimeError(f"chart_candles is missing required columns: {','.join(sorted(missing))}")
    return columns


def load_existing_timestamps(
    client: ClickHouseHttpClient,
    *,
    symbol: str,
    interval: str,
    start: str,
    end: str,
    limit: int = 500_000,
) -> set[str]:
    interval = normalize_chart_interval(interval)
    interval_filter = "interval IN ('1D', '1d')" if interval == "1D" else "interval = {interval:String}"
    parameters: dict[str, Any] = {"symbol": symbol, "start": start, "end": end, "limit": int(limit)}
    bucket_policy_filter = ""
    if interval != "1D":
        parameters["interval"] = interval
    if interval in INTRADAY_DERIVED_INTERVALS:
        bucket_policy_filter = "AND bucket_policy = 'us_equity_regular_session'"
    query = f"""
    SELECT formatDateTime(event_time, '%Y-%m-%d %H:%i:%S.000', 'UTC') AS eventTime
    FROM (
      SELECT
        event_time,
        ma5,
        ma20,
        ma60,
        row_number() OVER (
          PARTITION BY symbol, if(interval = '1d', '1D', interval), event_time
          ORDER BY inserted_at DESC, ifNull(source_event_id, '') DESC
        ) AS rn
      FROM {client.database}.chart_candles
      WHERE symbol = {{symbol:String}}
        AND {interval_filter}
        AND event_time >= parseDateTime64BestEffort({{start:String}})
        AND event_time < parseDateTime64BestEffort({{end:String}})
        AND canonical_version = 'v2'
        AND price_adjustment = 'split'
        {bucket_policy_filter}
    )
    WHERE rn = 1
      AND ma5 IS NOT NULL
      AND ma20 IS NOT NULL
      AND ma60 IS NOT NULL
    ORDER BY event_time ASC
    LIMIT {{limit:UInt32}}
    FORMAT JSONEachRow
    """
    rows = client.query_json_each_row(query, parameters)
    if len(rows) >= limit:
        raise RuntimeError(f"existing timestamp limit reached for {symbol}:{interval}; raise --timestamp-limit")
    return {str(row["eventTime"]) for row in rows if row.get("eventTime")}


def insert_missing_rows(
    client: ClickHouseHttpClient,
    rows: Iterable[dict[str, Any]],
    *,
    existing_timestamps: set[str],
    batch_size: int,
    token_prefix: str,
) -> int:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    pending = []
    seen = set(existing_timestamps)
    inserted = 0
    for row in rows:
        timestamp = str(row.get("event_time") or "")
        if not timestamp or timestamp in seen:
            continue
        seen.add(timestamp)
        pending.append(row)
        if len(pending) >= batch_size:
            _insert_batch(client, pending, token_prefix)
            inserted += len(pending)
            pending = []
    if pending:
        _insert_batch(client, pending, token_prefix)
        inserted += len(pending)
    existing_timestamps.update(seen)
    return inserted


def _insert_batch(client: ClickHouseHttpClient, rows: list[dict[str, Any]], token_prefix: str) -> None:
    identity = "|".join(str(row.get("event_time") or "") for row in rows)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    client.insert_json_each_row(
        "chart_candles",
        rows,
        deduplication_token=f"{token_prefix}:{digest}",
    )


def build_clickhouse_client() -> ClickHouseHttpClient:
    client = ClickHouseHttpClient(
        url=os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
        database=os.getenv("CLICKHOUSE_DATABASE", "market_data"),
        user=os.getenv("CLICKHOUSE_USER", "alfaka"),
        password=os.getenv("CLICKHOUSE_PASSWORD", "alfaka"),
    )
    if should_ensure_schema_on_start():
        client.ensure_market_data_schema()
    return client


def bootstrap(
    *,
    symbols: tuple[str, ...],
    intervals: tuple[str, ...],
    start: str,
    end: str,
    feed: str,
    apply: bool,
    insert_batch_size: int,
    timestamp_limit: int,
    continue_on_error: bool,
    client: ClickHouseHttpClient | None = None,
    fetcher=fetch_alpaca_bars,
) -> dict[str, Any]:
    client = client or build_clickhouse_client()
    table_columns = read_chart_candle_columns(client)
    summary: dict[str, Any] = {
        "mode": "apply" if apply else "dry-run",
        "range": {"start": start, "end": end},
        "symbolCount": len(symbols),
        "intervals": list(intervals),
        "fetchedRows": 0,
        "insertedRows": 0,
        "insertedSourceRows": 0,
        "skippedExistingRows": 0,
        "failures": [],
    }
    for symbol in symbols:
        symbol_feed = historical_feed_for_symbol(symbol, feed)
        source_requirements: dict[str, str] = {}
        for interval in intervals:
            source_interval = "1m" if interval in INTRADAY_DERIVED_INTERVALS else interval
            fetch_start = moving_average_warmup_start(interval, start)
            current = source_requirements.get(source_interval)
            if current is None or parse_time(fetch_start) < parse_time(current):
                source_requirements[source_interval] = fetch_start
        raw_by_source: dict[str, list[dict[str, Any]]] = {}
        existing_cache: dict[str, set[str]] = {}
        stored_sources: set[str] = set()

        def existing_for(target_interval: str) -> set[str]:
            if target_interval not in existing_cache:
                existing_cache[target_interval] = load_existing_timestamps(
                    client,
                    symbol=symbol,
                    interval=target_interval,
                    start=start,
                    end=end,
                    limit=timestamp_limit,
                )
            return existing_cache[target_interval]

        for interval in intervals:
            item = {"symbol": symbol, "interval": interval, "status": "planned"}
            if not apply:
                print(json.dumps(item, ensure_ascii=False), flush=True)
                continue
            try:
                source_interval = "1m" if interval in INTRADAY_DERIVED_INTERVALS else interval
                if source_interval not in raw_by_source:
                    raw_by_source[source_interval] = fetcher(
                        symbol,
                        source_requirements[source_interval],
                        end,
                        symbol_feed,
                        alpaca_timeframe_for_interval(source_interval),
                    )
                raw_bars = raw_by_source[source_interval]
                source_inserted = 0
                if source_interval != interval and source_interval not in stored_sources:
                    source_rows = prepare_clickhouse_rows(
                        symbol,
                        source_interval,
                        raw_bars,
                        feed=symbol_feed,
                        table_columns=table_columns,
                        allowed_sessions=frozenset({"regular"}),
                        store_start=start,
                        store_end=end,
                    )
                    source_inserted = insert_missing_rows(
                        client,
                        source_rows,
                        existing_timestamps=existing_for(source_interval),
                        batch_size=insert_batch_size,
                        token_prefix=f"candle-bootstrap:{symbol}:{source_interval}:{start}:{end}",
                    )
                    stored_sources.add(source_interval)
                rows = prepare_clickhouse_rows(
                    symbol,
                    interval,
                    raw_bars,
                    feed=symbol_feed,
                    table_columns=table_columns,
                    store_start=start,
                    store_end=end,
                )
                inserted = insert_missing_rows(
                    client,
                    rows,
                    existing_timestamps=existing_for(interval),
                    batch_size=insert_batch_size,
                    token_prefix=f"candle-bootstrap:{symbol}:{interval}:{start}:{end}",
                )
                summary["fetchedRows"] += len(rows)
                summary["insertedRows"] += inserted + source_inserted
                summary["insertedSourceRows"] += source_inserted
                summary["skippedExistingRows"] += len(rows) - inserted
                item.update({
                    "status": "completed",
                    "fetchedRows": len(rows),
                    "insertedRows": inserted,
                    "insertedSourceRows": source_inserted,
                    "skippedExistingRows": len(rows) - inserted,
                })
                print(json.dumps(item, ensure_ascii=False), flush=True)
            except Exception as exc:
                failure = {"symbol": symbol, "interval": interval, "error": str(exc)}
                summary["failures"].append(failure)
                print(json.dumps({**failure, "status": "failed"}, ensure_ascii=False), file=sys.stderr, flush=True)
                if not continue_on_error:
                    raise
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bootstrap one year of canonical candles into ClickHouse.")
    parser.add_argument("--symbols", help="Comma-separated symbols. Defaults to the repository S&P 500 universe.")
    parser.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS))
    parser.add_argument("--lookback-days", type=int, default=365)
    parser.add_argument("--start", help="Explicit inclusive ISO start; must be paired with --end.")
    parser.add_argument("--end", help="Explicit exclusive ISO end; must be paired with --start.")
    parser.add_argument("--feed", default=os.getenv("HISTORICAL_FEED", os.getenv("ALPACA_FEED", "sip")))
    parser.add_argument("--max-symbols", type=int, default=0, help="Limit symbols for smoke runs; zero means all.")
    parser.add_argument("--insert-batch-size", type=int, default=5_000)
    parser.add_argument("--timestamp-limit", type=int, default=500_000)
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Actually fetch and insert. Without this flag the job is a dry-run.")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    intervals = parse_intervals(args.intervals)
    symbols = parse_symbols(args.symbols)
    if args.max_symbols > 0:
        symbols = symbols[: args.max_symbols]
    start, end = resolve_bootstrap_range(
        start=args.start,
        end=args.end,
        lookback_days=args.lookback_days,
    )
    summary = bootstrap(
        symbols=symbols,
        intervals=intervals,
        start=start,
        end=end,
        feed=args.feed,
        apply=args.apply,
        insert_batch_size=args.insert_batch_size,
        timestamp_limit=args.timestamp_limit,
        continue_on_error=args.continue_on_error,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 1 if summary["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
