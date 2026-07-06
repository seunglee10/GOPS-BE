from __future__ import annotations

import math
from collections import OrderedDict
from datetime import timedelta
from typing import Any

from alfaka.serving.time_utils import canonical_utc_timestamp, parse_utc_time


FOOTPRINT_CALCULATION_VERSION = "footprint-estimated-v1"
FOOTPRINT_TIME_BUCKET = "1m"
FOOTPRINT_SIDE_CLASSIFICATION = "estimated"


def compute_footprint_payload(
    raw_payload: dict[str, Any],
    *,
    symbol: str,
    from_time: str,
    to_time: str,
) -> dict[str, Any]:
    trades = normalize_trades(raw_payload.get("trades") if isinstance(raw_payload, dict) else [])
    quotes = normalize_quotes(raw_payload.get("quotes") if isinstance(raw_payload, dict) else [])
    buckets = aggregate_footprint_buckets(trades, quotes)
    source = first_string(raw_payload, trades, "source") or "clickhouse"
    feed = first_string(raw_payload, trades, "feed") or first_string(raw_payload, quotes, "feed") or "unknown"
    return {
        "symbol": symbol,
        "interval": "footprint",
        "sourceInterval": FOOTPRINT_TIME_BUCKET,
        "from": from_time,
        "to": to_time,
        "timeBucket": FOOTPRINT_TIME_BUCKET,
        "source": source,
        "feed": feed,
        "dataStatus": "ready" if buckets else "empty",
        "sideClassification": FOOTPRINT_SIDE_CLASSIFICATION,
        "classificationVersion": FOOTPRINT_CALCULATION_VERSION,
        "calculationVersion": FOOTPRINT_CALCULATION_VERSION,
        "tradeCount": len(trades),
        "quoteCount": len(quotes),
        "buckets": buckets,
    }


def normalize_trades(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        timestamp = canonical_utc_timestamp(row.get("timestamp"))
        parsed = parse_utc_time(timestamp)
        price = number_or_none(row.get("price"))
        size = number_or_none(row.get("size")) or 0
        if not timestamp or parsed is None or price is None or size <= 0:
            continue
        normalized.append({
            **row,
            "timestamp": timestamp,
            "_time": parsed,
            "price": price,
            "size": size,
        })
    normalized.sort(key=lambda item: item["_time"])
    return normalized


def normalize_quotes(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        timestamp = canonical_utc_timestamp(row.get("timestamp"))
        parsed = parse_utc_time(timestamp)
        if not timestamp or parsed is None:
            continue
        normalized.append({
            **row,
            "timestamp": timestamp,
            "_time": parsed,
            "bidPrice": number_or_none(row.get("bidPrice")),
            "askPrice": number_or_none(row.get("askPrice")),
            "bidSize": number_or_none(row.get("bidSize")),
            "askSize": number_or_none(row.get("askSize")),
        })
    normalized.sort(key=lambda item: item["_time"])
    return normalized


def aggregate_footprint_buckets(trades: list[dict[str, Any]], quotes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    quote_index = 0
    active_quote: dict[str, Any] | None = None
    for trade in trades:
        while quote_index < len(quotes) and quotes[quote_index]["_time"] <= trade["_time"]:
            active_quote = quotes[quote_index]
            quote_index += 1
        side = classify_trade_side(trade, active_quote)
        timestamp = minute_bucket_timestamp(trade["_time"])
        bucket = buckets.setdefault(timestamp, new_bucket(timestamp))
        update_bucket(bucket, trade, side)
    return [finalize_bucket(bucket) for bucket in buckets.values()]


def classify_trade_side(trade: dict[str, Any], quote: dict[str, Any] | None) -> str:
    if not quote:
        return "unknown"
    price = trade["price"]
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


def new_bucket(timestamp: str) -> dict[str, Any]:
    start = parse_utc_time(timestamp)
    end = (start + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z") if start else timestamp
    return {
        "timestamp": timestamp,
        "from": timestamp,
        "to": end,
        "open": None,
        "high": None,
        "low": None,
        "close": None,
        "volume": 0.0,
        "tradeCount": 0,
        "askVolume": 0.0,
        "bidVolume": 0.0,
        "unknownVolume": 0.0,
        "priceLevels": OrderedDict(),
    }


def update_bucket(bucket: dict[str, Any], trade: dict[str, Any], side: str) -> None:
    price = trade["price"]
    size = trade["size"]
    bucket["open"] = price if bucket["open"] is None else bucket["open"]
    bucket["high"] = price if bucket["high"] is None else max(bucket["high"], price)
    bucket["low"] = price if bucket["low"] is None else min(bucket["low"], price)
    bucket["close"] = price
    bucket["volume"] += size
    bucket["tradeCount"] += 1
    volume_key = side_volume_key(side)
    bucket[volume_key] += size
    level_key = format_price_key(price)
    level = bucket["priceLevels"].setdefault(level_key, {
        "price": rounded(price),
        "askVolume": 0.0,
        "bidVolume": 0.0,
        "unknownVolume": 0.0,
        "totalVolume": 0.0,
        "tradeCount": 0,
    })
    level[volume_key] += size
    level["totalVolume"] += size
    level["tradeCount"] += 1


def finalize_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    price_levels = list(bucket["priceLevels"].values())
    price_levels.sort(key=lambda item: item["price"], reverse=True)
    for level in price_levels:
        level["askVolume"] = rounded(level["askVolume"])
        level["bidVolume"] = rounded(level["bidVolume"])
        level["unknownVolume"] = rounded(level["unknownVolume"])
        level["totalVolume"] = rounded(level["totalVolume"])
        level["delta"] = rounded(level["askVolume"] - level["bidVolume"])
    ask = bucket["askVolume"]
    bid = bucket["bidVolume"]
    unknown = bucket["unknownVolume"]
    return {
        "timestamp": bucket["timestamp"],
        "from": bucket["from"],
        "to": bucket["to"],
        "open": rounded(bucket["open"]),
        "high": rounded(bucket["high"]),
        "low": rounded(bucket["low"]),
        "close": rounded(bucket["close"]),
        "volume": rounded(bucket["volume"]),
        "tradeCount": bucket["tradeCount"],
        "askVolume": rounded(ask),
        "bidVolume": rounded(bid),
        "unknownVolume": rounded(unknown),
        "delta": rounded(ask - bid),
        "priceLevels": price_levels,
    }


def side_volume_key(side: str) -> str:
    if side == "ask":
        return "askVolume"
    if side == "bid":
        return "bidVolume"
    return "unknownVolume"


def minute_bucket_timestamp(value) -> str:
    return value.replace(second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def format_price_key(price: float) -> str:
    return f"{price:.4f}"


def first_string(raw_payload: dict[str, Any], rows: list[dict[str, Any]], key: str) -> str | None:
    value = raw_payload.get(key) if isinstance(raw_payload, dict) else None
    if isinstance(value, str) and value:
        return value
    for row in rows:
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None


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


def rounded(value: float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 8)
