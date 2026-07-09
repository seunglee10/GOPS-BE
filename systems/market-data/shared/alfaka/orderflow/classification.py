from __future__ import annotations

import math
from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Any

from alfaka.serving.time_utils import canonical_utc_timestamp, parse_utc_time


ORDER_FLOW_CLASSIFICATION_VERSION = "orderflow-estimated-v1"
ORDER_FLOW_SIDE_CLASSIFICATION = "estimated"


def normalize_trade(row: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    timestamp = canonical_utc_timestamp(row.get("timestamp") or row.get("event_time"))
    parsed = parse_utc_time(timestamp)
    price = number_or_none(row.get("price"))
    size = number_or_none(row.get("size")) or 0
    if not timestamp or parsed is None or price is None or size <= 0:
        return None
    return {
        **row,
        "timestamp": timestamp,
        "_time": parsed,
        "price": price,
        "size": size,
    }


def normalize_quote(row: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    timestamp = canonical_utc_timestamp(row.get("timestamp") or row.get("event_time"))
    parsed = parse_utc_time(timestamp)
    if not timestamp or parsed is None:
        return None
    return {
        **row,
        "timestamp": timestamp,
        "_time": parsed,
        "bidPrice": number_or_none(row.get("bidPrice", row.get("bid_price"))),
        "askPrice": number_or_none(row.get("askPrice", row.get("ask_price"))),
        "bidSize": number_or_none(row.get("bidSize", row.get("bid_size"))),
        "askSize": number_or_none(row.get("askSize", row.get("ask_size"))),
    }


def iter_normalized_trades(rows: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for row in rows:
        normalized = normalize_trade(row)
        if normalized is not None:
            yield normalized


def iter_normalized_quotes(rows: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for row in rows:
        normalized = normalize_quote(row)
        if normalized is not None:
            yield normalized


def normalize_trades(rows: Any) -> list[dict[str, Any]]:
    if rows is None:
        return []
    normalized: list[dict[str, Any]] = []
    for row in rows:
        item = normalize_trade(row)
        if item is not None:
            normalized.append(item)
    normalized.sort(key=lambda item: item["_time"])
    return normalized


def normalize_quotes(rows: Any) -> list[dict[str, Any]]:
    if rows is None:
        return []
    normalized: list[dict[str, Any]] = []
    for row in rows:
        item = normalize_quote(row)
        if item is not None:
            normalized.append(item)
    normalized.sort(key=lambda item: item["_time"])
    return normalized


def merge_trades_with_quotes(
    trades_iter: Iterable[dict[str, Any]],
    quotes_iter: Iterable[dict[str, Any]],
    *,
    initial_quote: dict[str, Any] | None = None,
) -> Iterator[tuple[dict[str, Any], dict[str, Any] | None]]:
    quote_iterator = iter(quotes_iter)
    active_quote = initial_quote
    pending_quote = _next_quote(quote_iterator)
    for trade in trades_iter:
        trade_time = row_time(trade)
        if trade_time is None:
            yield trade, active_quote
            continue
        while pending_quote is not None:
            quote_time = row_time(pending_quote)
            if quote_time is None:
                pending_quote = _next_quote(quote_iterator)
                continue
            if quote_time > trade_time:
                break
            active_quote = pending_quote
            pending_quote = _next_quote(quote_iterator)
        yield trade, active_quote


def classify_trade_side(trade: dict[str, Any], quote: dict[str, Any] | None) -> str:
    if not quote:
        return "unknown"
    price = trade.get("price")
    if price is None or not isinstance(price, (int, float)):
        return "unknown"
    bid = quote.get("bidPrice")
    ask = quote.get("askPrice")
    if ask is not None and price >= ask:
        return "ask"
    if bid is not None and price <= bid:
        return "bid"
    if bid is not None and ask is not None and ask >= bid:
        mid = (bid + ask) / 2
        if price > mid:
            return "ask"
        if price < mid:
            return "bid"
    return "unknown"


def row_time(row: dict[str, Any] | None) -> datetime | None:
    if not row:
        return None
    value = row.get("_time")
    if isinstance(value, datetime):
        return value
    timestamp = row.get("timestamp") or row.get("event_time")
    parsed = parse_utc_time(canonical_utc_timestamp(timestamp))
    if parsed is not None:
        row["_time"] = parsed
    return parsed


def number_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def _next_quote(quotes: Iterator[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        return next(quotes)
    except StopIteration:
        return None
