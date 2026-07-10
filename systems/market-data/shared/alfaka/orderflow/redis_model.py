from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from alfaka.orderflow.classification import (
    ORDER_FLOW_CLASSIFICATION_VERSION,
    ORDER_FLOW_SIDE_CLASSIFICATION,
)


ORDER_FLOW_MINUTE_BLOB_VERSION = 1


def order_flow_minute_score(event_minute: str) -> int:
    return int(datetime.fromisoformat(str(event_minute).replace("Z", "+00:00")).timestamp())


def order_flow_minute_blob(symbol: str, event_minute: str, bins: list[dict[str, Any]]) -> dict[str, Any]:
    normalized_symbol = str(symbol or "").upper()
    sorted_bins = sorted(bins, key=lambda item: float(item.get("priceBin") or 0))
    first = sorted_bins[0] if sorted_bins else {}
    return {
        "version": ORDER_FLOW_MINUTE_BLOB_VERSION,
        "symbol": normalized_symbol,
        "eventMinute": event_minute,
        "sessionDate": first.get("sessionDate"),
        "priceBinSize": first.get("priceBinSize", 0.01),
        "sideClassification": first.get("sideClassification") or ORDER_FLOW_SIDE_CLASSIFICATION,
        "classificationVersion": first.get("classificationVersion") or ORDER_FLOW_CLASSIFICATION_VERSION,
        "marketSession": "regular",
        "source": first.get("source") or "alpaca",
        "feed": first.get("feed") or "unknown",
        "updatedAt": first.get("updatedAt"),
        "bins": [_minute_level_payload(item) for item in sorted_bins],
    }


def encode_order_flow_minute_blob(blob: dict[str, Any]) -> str:
    return json.dumps(blob, ensure_ascii=False, separators=(",", ":"))


def parse_order_flow_minute_blob(value: Any) -> dict[str, Any] | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def order_flow_blob_to_bins(blob: dict[str, Any]) -> list[dict[str, Any]]:
    event_minute = str(blob.get("eventMinute") or "")
    if not event_minute:
        return []
    symbol = str(blob.get("symbol") or "").upper()
    session_date = blob.get("sessionDate")
    price_bin_size = blob.get("priceBinSize", 0.01)
    side_classification = blob.get("sideClassification") or ORDER_FLOW_SIDE_CLASSIFICATION
    classification_version = blob.get("classificationVersion") or ORDER_FLOW_CLASSIFICATION_VERSION
    source = blob.get("source") or "alpaca"
    feed = blob.get("feed") or "unknown"
    rows = []
    for item in blob.get("bins") or []:
        if not isinstance(item, dict):
            continue
        ask_volume = _number_or_zero(item.get("askVolume"))
        bid_volume = _number_or_zero(item.get("bidVolume"))
        unknown_volume = _number_or_zero(item.get("unknownVolume"))
        ask_count = int(item.get("askTradeCount") or 0)
        bid_count = int(item.get("bidTradeCount") or 0)
        unknown_count = int(item.get("unknownTradeCount") or 0)
        rows.append({
            "eventType": "ORDER_FLOW_BIN",
            "eventMinute": event_minute,
            "sessionDate": session_date,
            "symbol": symbol,
            "priceBin": item.get("priceBin"),
            "priceBinSize": price_bin_size,
            "askVolume": ask_volume,
            "bidVolume": bid_volume,
            "unknownVolume": unknown_volume,
            "askTradeCount": ask_count,
            "bidTradeCount": bid_count,
            "unknownTradeCount": unknown_count,
            "volume": _number_or_zero(item.get("volume"), default=ask_volume + bid_volume + unknown_volume),
            "tradeCount": int(item.get("tradeCount") or ask_count + bid_count + unknown_count),
            "sideClassification": side_classification,
            "classificationVersion": classification_version,
            "source": source,
            "feed": item.get("feed") or feed,
            "marketSession": "regular",
            "updatedAt": item.get("updatedAt") or blob.get("updatedAt"),
        })
    rows.sort(key=lambda row: float(row.get("priceBin") or 0))
    return rows


def _minute_level_payload(bin_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "priceBin": bin_payload.get("priceBin"),
        "askVolume": _number_or_zero(bin_payload.get("askVolume")),
        "bidVolume": _number_or_zero(bin_payload.get("bidVolume")),
        "unknownVolume": _number_or_zero(bin_payload.get("unknownVolume")),
        "askTradeCount": int(bin_payload.get("askTradeCount") or 0),
        "bidTradeCount": int(bin_payload.get("bidTradeCount") or 0),
        "unknownTradeCount": int(bin_payload.get("unknownTradeCount") or 0),
        "volume": _number_or_zero(bin_payload.get("volume")),
        "tradeCount": int(bin_payload.get("tradeCount") or 0),
        "feed": bin_payload.get("feed") or "unknown",
        "updatedAt": bin_payload.get("updatedAt"),
    }


def _number_or_zero(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)
