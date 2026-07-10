from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter, defaultdict, deque
from datetime import date, datetime, time as date_time, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from alfaka.alpaca.feed_profiles import market_session_for_datetime
from alfaka.orderflow.classification import (
    ORDER_FLOW_CLASSIFICATION_VERSION,
    classify_trade_side,
    iter_normalized_quotes,
    iter_normalized_trades,
    merge_trades_with_quotes,
    normalize_quotes,
    normalize_trades,
)
from alfaka.orderflow.config import (
    pinned_symbols_from_env,
    price_bin_size_from_env,
    quote_future_tolerance_ms_from_env,
    quote_max_age_ms_from_env,
)
from alfaka.serving.time_utils import canonical_utc_timestamp, parse_utc_time
from alfaka.storage.clickhouse_loader import ClickHouseHttpClient


MARKET_TIMEZONE = ZoneInfo("America/New_York")
REGULAR_OPEN = date_time(9, 30)
REGULAR_CLOSE = date_time(16, 0)
QUOTE_WARMUP_MINUTES = 5
ROLLUP_TABLE = "order_flow_profile_daily"


def rollup_session(
    client,
    symbol: str,
    session_date: str | date,
    *,
    price_bin_size: float = 0.01,
    dry_run: bool = False,
    source: str = "ticks",
) -> dict[str, Any]:
    started = time.monotonic()
    symbol = str(symbol or "").strip().upper()
    session_day = parse_session_date(session_date)
    bounds = regular_session_bounds_utc(session_day)
    if bounds is None:
        return {
            "symbol": symbol,
            "sessionDate": session_day.isoformat(),
            "status": "closed",
            "rows": [],
            "insertedRows": 0,
            "durationMs": int((time.monotonic() - started) * 1000),
        }
    if source == "alpaca":
        return rollup_session_from_alpaca(
            client,
            symbol,
            session_day,
            bounds,
            price_bin_size=price_bin_size,
            dry_run=dry_run,
            started=started,
        )
    return rollup_session_from_ticks(
        client,
        symbol,
        session_day,
        bounds,
        price_bin_size=price_bin_size,
        dry_run=dry_run,
        started=started,
    )


def rollup_session_from_ticks(
    client,
    symbol: str,
    session_day: date,
    bounds: tuple[datetime, datetime, datetime],
    *,
    price_bin_size: float,
    dry_run: bool,
    started: float | None = None,
) -> dict[str, Any]:
    session_start, session_end, quote_warmup_start = bounds
    aggregate = OrderFlowDailyAggregate(symbol, session_day, price_bin_size)
    trade_deduper = RecentIdDeduper()
    quote_deduper = RecentIdDeduper()
    carry_quote = None
    duplicate_count = 0

    for win_start, win_end in hourly_windows(session_start, session_end):
        quote_start = quote_warmup_start if win_start == session_start else win_start
        trade_rows = query_trade_rows(client, symbol, win_start, win_end)
        quote_rows = query_quote_rows(client, symbol, quote_start, win_end)
        trade_rows, trade_dupes = dedupe_rows(trade_rows, trade_deduper)
        quote_rows, quote_dupes = dedupe_rows(quote_rows, quote_deduper)
        duplicate_count += trade_dupes + quote_dupes
        trades = normalize_trades([normalize_trade_tick_row(row) for row in trade_rows])
        quotes = normalize_quotes([normalize_quote_tick_row(row) for row in quote_rows])
        carry_quote = accumulate_order_flow(aggregate, trades, quotes, initial_quote=carry_quote)

    return finish_rollup(client, aggregate, duplicate_count, dry_run=dry_run, started=started)


def rollup_session_from_alpaca(
    client,
    symbol: str,
    session_day: date,
    bounds: tuple[datetime, datetime, datetime],
    *,
    price_bin_size: float,
    dry_run: bool,
    started: float | None = None,
) -> dict[str, Any]:
    session_start, session_end, quote_warmup_start = bounds
    aggregate = OrderFlowDailyAggregate(symbol, session_day, price_bin_size)
    trades = iter_normalized_trades(fetch_alpaca_rows(symbol, "trades", session_start, session_end))
    quotes = iter_normalized_quotes(fetch_alpaca_rows(symbol, "quotes", quote_warmup_start, session_end))
    accumulate_order_flow(aggregate, trades, quotes)
    return finish_rollup(client, aggregate, 0, dry_run=dry_run, started=started)


class CountingIterator:
    def __init__(self, rows: Iterable[dict[str, Any]]):
        self._iterator = iter(rows)
        self.count = 0
        self.last: dict[str, Any] | None = None

    def __iter__(self):
        return self

    def __next__(self):
        row = next(self._iterator)
        self.count += 1
        self.last = row
        return row


def accumulate_order_flow(
    aggregate: "OrderFlowDailyAggregate",
    trades: Iterable[dict[str, Any]],
    quotes: Iterable[dict[str, Any]],
    *,
    initial_quote: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    quote_counter = CountingIterator(quotes)
    for trade, quote in merge_trades_with_quotes(trades, quote_counter, initial_quote=initial_quote):
        aggregate.add_trade(trade, quote)
    for _quote in quote_counter:
        pass
    aggregate.quote_count += quote_counter.count
    return quote_counter.last or initial_quote


def finish_rollup(client, aggregate, duplicate_count: int, *, dry_run: bool, started: float | None) -> dict[str, Any]:
    rows = aggregate.rows()
    if rows and not dry_run:
        client.insert_json_each_row(ROLLUP_TABLE, rows)
    summary = aggregate.summary(rows, duplicate_count=duplicate_count)
    summary["insertedRows"] = 0 if dry_run else len(rows)
    summary["dryRun"] = bool(dry_run)
    summary["durationMs"] = int((time.monotonic() - (started or time.monotonic())) * 1000)
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, ensure_ascii=False, sort_keys=True), flush=True)
    return summary


class OrderFlowDailyAggregate:
    def __init__(self, symbol: str, session_day: date, price_bin_size: float):
        self.symbol = symbol
        self.session_day = session_day
        self.price_bin_size = float(price_bin_size)
        self.bins = {}
        self.trade_count = 0
        self.quote_count = 0
        self.side_counts = Counter()
        self.market_session_counts = Counter()
        self.feed_profile_counts = Counter()
        self.feed_counts = Counter()
        self.quote_max_age_ms = quote_max_age_ms_from_env()
        self.quote_future_tolerance_ms = quote_future_tolerance_ms_from_env()

    def add_trade(self, trade: dict[str, Any], quote: dict[str, Any] | None) -> None:
        try:
            price = float(trade["price"])
            size = float(trade["size"])
        except (KeyError, TypeError, ValueError):
            return
        if size <= 0:
            return
        side = classify_trade_side(
            trade,
            quote,
            max_quote_age_ms=self.quote_max_age_ms,
            future_tolerance_ms=self.quote_future_tolerance_ms,
        )
        price_bin = round(round(price / self.price_bin_size) * self.price_bin_size, 6)
        bucket = self.bins.setdefault(price_bin, new_daily_bucket(price_bin, self.price_bin_size))
        volume_key, count_key = side_keys(side)
        bucket[volume_key] += size
        bucket[count_key] += 1
        bucket["volume"] += size
        bucket["trade_count"] += 1
        self.trade_count += 1
        self.side_counts[side] += 1
        self.market_session_counts[str(trade.get("marketSession") or "unknown")] += 1
        self.feed_profile_counts[str(trade.get("feedProfile") or trade.get("feed") or "unknown")] += 1
        self.feed_counts[str(trade.get("feed") or "unknown")] += 1

    def rows(self) -> list[dict[str, Any]]:
        feed = dominant(self.feed_counts) or "sip"
        feed_profile = dominant(self.feed_profile_counts) or feed
        rows = []
        for price_bin in sorted(self.bins):
            bucket = self.bins[price_bin]
            rows.append({
                "session_date": self.session_day.isoformat(),
                "symbol": self.symbol,
                "price_bin": price_bin,
                "price_bin_size": self.price_bin_size,
                "ask_volume": bucket["ask_volume"],
                "bid_volume": bucket["bid_volume"],
                "unknown_volume": bucket["unknown_volume"],
                "ask_trade_count": bucket["ask_trade_count"],
                "bid_trade_count": bucket["bid_trade_count"],
                "unknown_trade_count": bucket["unknown_trade_count"],
                "trade_count": bucket["trade_count"],
                "volume": bucket["volume"],
                "classification_version": ORDER_FLOW_CLASSIFICATION_VERSION,
                "source": "clickhouse-rollup",
                "feed": feed,
                "feed_profile": feed_profile,
                "market_session": "regular",
            })
        return rows

    def summary(self, rows: list[dict[str, Any]], *, duplicate_count: int) -> dict[str, Any]:
        unknown_count = self.side_counts.get("unknown", 0)
        unknown_ratio = unknown_count / self.trade_count if self.trade_count else 0.0
        if self.trade_count and self.market_session_counts.get("regular", 0) / self.trade_count < 0.99:
            print(
                f"WARNING order-flow regular-session share below 99%: symbol={self.symbol} "
                f"sessionDate={self.session_day.isoformat()} distribution={dict(self.market_session_counts)}",
                flush=True,
            )
        return {
            "symbol": self.symbol,
            "sessionDate": self.session_day.isoformat(),
            "status": "ready" if rows else "empty",
            "bins": len(rows),
            "tradeCount": self.trade_count,
            "quoteCount": self.quote_count,
            "duplicateCount": duplicate_count,
            "unknownRatio": unknown_ratio,
            "marketSessionDistribution": dict(self.market_session_counts),
            "feedProfileDistribution": dict(self.feed_profile_counts),
            "sideDistribution": dict(self.side_counts),
            "rows": rows,
        }


def new_daily_bucket(price_bin: float, price_bin_size: float) -> dict[str, Any]:
    return {
        "price_bin": price_bin,
        "price_bin_size": price_bin_size,
        "ask_volume": 0.0,
        "bid_volume": 0.0,
        "unknown_volume": 0.0,
        "ask_trade_count": 0,
        "bid_trade_count": 0,
        "unknown_trade_count": 0,
        "trade_count": 0,
        "volume": 0.0,
    }


def side_keys(side: str) -> tuple[str, str]:
    if side == "ask":
        return "ask_volume", "ask_trade_count"
    if side == "bid":
        return "bid_volume", "bid_trade_count"
    return "unknown_volume", "unknown_trade_count"


class RecentIdDeduper:
    def __init__(self, max_seen=50000):
        self.max_seen = max_seen
        self.seen = set()
        self.order = deque()

    def duplicate(self, source_event_id):
        if not source_event_id:
            return False
        if source_event_id in self.seen:
            return True
        self.seen.add(source_event_id)
        self.order.append(source_event_id)
        if len(self.order) > self.max_seen:
            self.seen.discard(self.order.popleft())
        return False


def dedupe_rows(rows: Iterable[dict[str, Any]], deduper: RecentIdDeduper) -> tuple[list[dict[str, Any]], int]:
    output = []
    duplicates = 0
    for row in rows:
        if deduper.duplicate(row.get("source_event_id") or row.get("sourceEventId")):
            duplicates += 1
            continue
        output.append(row)
    return output, duplicates


def normalize_trade_tick_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": canonical_utc_timestamp(row.get("timestamp") or row.get("event_time")),
        "price": row.get("price"),
        "size": row.get("size"),
        "exchange": row.get("exchange"),
        "conditions": row.get("conditions") or [],
        "tape": row.get("tape"),
        "source": row.get("source") or "clickhouse",
        "feed": row.get("feed") or "sip",
        "feedProfile": row.get("feedProfile") or row.get("feed_profile") or row.get("feed") or "sip",
        "marketSession": row.get("marketSession") or row.get("market_session") or "unknown",
        "sourceEventId": row.get("sourceEventId") or row.get("source_event_id"),
    }


def normalize_quote_tick_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": canonical_utc_timestamp(row.get("timestamp") or row.get("event_time")),
        "bidPrice": row.get("bidPrice", row.get("bid_price")),
        "bidSize": row.get("bidSize", row.get("bid_size")),
        "askPrice": row.get("askPrice", row.get("ask_price")),
        "askSize": row.get("askSize", row.get("ask_size")),
        "source": row.get("source") or "clickhouse",
        "feed": row.get("feed") or "sip",
        "feedProfile": row.get("feedProfile") or row.get("feed_profile") or row.get("feed") or "sip",
        "marketSession": row.get("marketSession") or row.get("market_session") or "unknown",
        "sourceEventId": row.get("sourceEventId") or row.get("source_event_id"),
    }


def query_trade_rows(client, symbol: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    query = """
    SELECT event_time, price, size, exchange, conditions, tape, source, feed, feed_profile,
           market_session, source_event_id
    FROM market_data.trade_ticks
    WHERE symbol = {symbol:String}
      AND event_time >= parseDateTime64BestEffort({fromTime:String})
      AND event_time < parseDateTime64BestEffort({toTime:String})
    ORDER BY event_time ASC
    FORMAT JSONEachRow
    """
    return client.query_json_each_row(query, {"symbol": symbol, "fromTime": iso(start), "toTime": iso(end)})


def query_quote_rows(client, symbol: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    query = """
    SELECT event_time, bid_price, ask_price, bid_size, ask_size, source, feed, feed_profile,
           market_session, source_event_id
    FROM market_data.quote_ticks
    WHERE symbol = {symbol:String}
      AND event_time >= parseDateTime64BestEffort({fromTime:String})
      AND event_time < parseDateTime64BestEffort({toTime:String})
    ORDER BY event_time ASC
    FORMAT JSONEachRow
    """
    return client.query_json_each_row(query, {"symbol": symbol, "fromTime": iso(start), "toTime": iso(end)})


def count_trade_ticks(client, symbol: str, session_day: date) -> int:
    bounds = regular_session_bounds_utc(session_day)
    if bounds is None:
        return 0
    start, end, _warmup = bounds
    query = """
    SELECT count() AS n
    FROM market_data.trade_ticks
    WHERE symbol = {symbol:String}
      AND event_time >= parseDateTime64BestEffort({fromTime:String})
      AND event_time < parseDateTime64BestEffort({toTime:String})
    FORMAT JSONEachRow
    """
    rows = client.query_json_each_row(query, {"symbol": symbol, "fromTime": iso(start), "toTime": iso(end)})
    return int((rows[0] if rows else {}).get("n") or 0)


def fetch_alpaca_rows(symbol: str, kind: str, start: datetime, end: datetime):
    import requests

    key_id = os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY_ID")
    secret = os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_API_SECRET_KEY")
    if not key_id or not secret:
        raise RuntimeError("Alpaca historical order-flow source requires APCA_API_KEY_ID/APCA_API_SECRET_KEY.")
    endpoint_kind = "trades" if kind == "trades" else "quotes"
    url = f"{os.getenv('ALPACA_DATA_BASE_URL', 'https://data.alpaca.markets').rstrip('/')}/v2/stocks/{symbol}/{endpoint_kind}"
    headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}
    params = {"start": iso(start), "end": iso(end), "limit": 10000, "feed": os.getenv("ORDER_FLOW_ALPACA_FEED", "sip")}
    last_time = None
    while True:
        response = requests.get(url, headers=headers, params=params, timeout=float(os.getenv("ALPACA_REST_TIMEOUT_SECONDS", "20")))
        if response.status_code == 429:
            time.sleep(1)
            continue
        if response.status_code >= 400:
            raise RuntimeError(f"Alpaca {kind} fetch failed: status={response.status_code}, body={response.text[:500]}")
        payload = response.json()
        for item in payload.get(endpoint_kind, []):
            row = normalize_alpaca_item(symbol, kind, item)
            row_time = parse_utc_time(canonical_utc_timestamp(row.get("timestamp")))
            if last_time is not None and row_time is not None and row_time < last_time:
                print(
                    f"WARNING Alpaca {kind} rows not monotonic: symbol={symbol} previous={last_time.isoformat()} current={row_time.isoformat()}",
                    flush=True,
                )
            if row_time is not None:
                last_time = row_time
            yield row
        token = payload.get("next_page_token")
        if not token:
            return
        params["page_token"] = token


def normalize_alpaca_item(symbol: str, kind: str, item: dict[str, Any]) -> dict[str, Any]:
    if kind == "trades":
        return {
            "symbol": symbol,
            "timestamp": item.get("t"),
            "price": item.get("p"),
            "size": item.get("s"),
            "exchange": item.get("x"),
            "conditions": item.get("c") or [],
            "tape": item.get("z"),
            "source": "alpaca-rest",
            "feed": os.getenv("ORDER_FLOW_ALPACA_FEED", "sip"),
            "feedProfile": os.getenv("ORDER_FLOW_ALPACA_FEED", "sip"),
            "marketSession": "regular",
            "sourceEventId": item.get("i"),
        }
    return {
        "symbol": symbol,
        "timestamp": item.get("t"),
        "bidPrice": item.get("bp"),
        "bidSize": item.get("bs"),
        "askPrice": item.get("ap"),
        "askSize": item.get("as"),
        "source": "alpaca-rest",
        "feed": os.getenv("ORDER_FLOW_ALPACA_FEED", "sip"),
        "feedProfile": os.getenv("ORDER_FLOW_ALPACA_FEED", "sip"),
        "marketSession": "regular",
        "sourceEventId": item.get("i") or f"{symbol}/quote/{item.get('t')}",
    }


def regular_session_bounds_utc(session_day: date) -> tuple[datetime, datetime, datetime] | None:
    session_start_local = datetime.combine(session_day, REGULAR_OPEN, tzinfo=MARKET_TIMEZONE)
    if market_session_for_datetime(session_start_local.astimezone(timezone.utc)) == "closed":
        return None
    session_end_local = datetime.combine(session_day, REGULAR_CLOSE, tzinfo=MARKET_TIMEZONE)
    warmup_local = session_start_local - timedelta(minutes=QUOTE_WARMUP_MINUTES)
    return (
        session_start_local.astimezone(timezone.utc),
        session_end_local.astimezone(timezone.utc),
        warmup_local.astimezone(timezone.utc),
    )


def hourly_windows(start: datetime, end: datetime):
    cursor = start
    while cursor < end:
        next_cursor = min(cursor + timedelta(hours=1), end)
        yield cursor, next_cursor
        cursor = next_cursor


def parse_session_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def trading_sessions_ending(end_date: date, count: int) -> list[date]:
    sessions = []
    current = end_date
    while len(sessions) < max(1, count):
        if regular_session_bounds_utc(current) is not None:
            sessions.append(current)
        current = current - timedelta(days=1)
    return list(reversed(sessions))


def default_rollup_date(now: datetime | None = None) -> date:
    current = (now or datetime.now(timezone.utc)).astimezone(MARKET_TIMEZONE)
    today = current.date()
    close = datetime.combine(today, REGULAR_CLOSE, tzinfo=MARKET_TIMEZONE)
    candidate = today if current >= close else today - timedelta(days=1)
    return trading_sessions_ending(candidate, 1)[-1]


def dominant(counter: Counter) -> str | None:
    if not counter:
        return None
    return counter.most_common(1)[0][0]


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def create_clickhouse_client_from_env() -> ClickHouseHttpClient:
    return ClickHouseHttpClient(
        os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
        os.getenv("CLICKHOUSE_DATABASE", "market_data"),
        os.getenv("CLICKHOUSE_USER", "alfaka"),
        os.getenv("CLICKHOUSE_PASSWORD", ""),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Roll up daily order-flow profile rows from raw ticks.")
    parser.add_argument("--date", dest="session_date", default=None)
    parser.add_argument("--symbols", default=None)
    parser.add_argument("--backfill-days", type=int, default=1)
    parser.add_argument("--source", choices=("ticks", "alpaca"), default="ticks")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    client = create_clickhouse_client_from_env()
    end_date = parse_session_date(args.session_date) if args.session_date else default_rollup_date()
    symbols = [item.strip().upper() for item in (args.symbols.split(",") if args.symbols else sorted(pinned_symbols_from_env())) if item.strip()]
    sessions = trading_sessions_ending(end_date, args.backfill_days)
    for session_day in sessions:
        for symbol in symbols:
            source = args.source
            if source == "alpaca" and count_trade_ticks(client, symbol, session_day) > 0:
                source = "ticks"
            rollup_session(
                client,
                symbol,
                session_day,
                price_bin_size=price_bin_size_from_env(),
                dry_run=args.dry_run,
                source=source,
            )
    return 0
