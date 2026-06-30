from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


DEFAULT_HOT_LIMIT = 10
HOT_RANKING_METHOD = "current_session_dollar_volume"


def dollar_volume_from_candle(candle: dict[str, Any]) -> float:
    volume = read_float(candle.get("volume"))
    if volume is None:
        return 0.0
    price = read_float(candle.get("vwap")) or read_float(candle.get("vw")) or read_float(candle.get("close"))
    if price is None:
        return 0.0
    return max(0.0, volume * price)


def build_hot_symbols_payload(records: list[dict[str, Any]], limit: int = DEFAULT_HOT_LIMIT, as_of: str | None = None) -> dict[str, Any]:
    as_of = as_of or utc_now_iso()
    ranked = rank_hot_symbol_records(records, limit)
    return {
        "ranking": {
            "method": HOT_RANKING_METHOD,
            "universe": "gops20",
            "limit": limit,
            "asOf": as_of,
            "refreshSeconds": 60,
            "sourceUpdatedAt": as_of,
        },
        "symbols": ranked,
    }


def rank_hot_symbol_records(records: list[dict[str, Any]], limit: int = DEFAULT_HOT_LIMIT) -> list[dict[str, Any]]:
    prepared = []
    for record in records:
        symbol = record.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            continue
        session_dollar_volume = read_float(record.get("sessionDollarVolume"))
        if session_dollar_volume is None:
            candles = record.get("candles") if isinstance(record.get("candles"), list) else []
            session_dollar_volume = sum(dollar_volume_from_candle(candle) for candle in candles if isinstance(candle, dict))
        prepared.append({
            **record,
            "symbol": symbol.strip().upper(),
            "sessionDollarVolume": round(session_dollar_volume or 0.0, 4),
        })

    prepared.sort(key=lambda item: (-float(item.get("sessionDollarVolume") or 0), item["symbol"]))
    ranked = []
    for index, record in enumerate(prepared[:limit], start=1):
        ranked.append({
            "rank": index,
            "symbol": record["symbol"],
            "name": record.get("name") or record["symbol"],
            "market": record.get("market") or "US",
            "lastPrice": read_float(record.get("lastPrice")),
            "changePercent": read_float(record.get("changePercent")),
            "volume": read_float(record.get("volume")),
            "sessionDollarVolume": record["sessionDollarVolume"],
            "sourceUpdatedAt": record.get("sourceUpdatedAt"),
            "rankingWindow": record.get("rankingWindow") or "current_session",
            "rankReason": record.get("rankReason") or "volume_x_vwap_or_close",
        })
    return ranked


def read_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
