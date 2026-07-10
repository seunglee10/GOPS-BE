from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from alfaka.orderflow.classification import (
    ORDER_FLOW_CLASSIFICATION_VERSION,
    ORDER_FLOW_SIDE_CLASSIFICATION,
)
from alfaka.orderflow.config import pinned_symbols_from_env
from alfaka.streaming.transforms import floor_minute, to_iso


MARKET_TIMEZONE = ZoneInfo("America/New_York")


class OrderFlowBinBuilder:
    def __init__(self, price_bin_size: float = 0.01, pinned_symbols: frozenset[str] | None = None):
        self.price_bin_size = float(price_bin_size)
        self.pinned_symbols = frozenset(symbol.upper() for symbol in (pinned_symbols if pinned_symbols is not None else pinned_symbols_from_env()))
        self.bins: dict[tuple[str, int, float], dict[str, Any]] = {}
        self.last_session_date_by_symbol: dict[str, str] = {}
        self.pending_session_rollovers: dict[str, str] = {}
        self.last_seen_minute_epoch: int | None = None
        self.sweep_count = 0

    def update(self, trade: dict[str, Any], side: str) -> dict[str, Any] | None:
        symbol = str(trade.get("symbol") or "").upper()
        if symbol not in self.pinned_symbols:
            return None
        if trade.get("marketSession") != "regular":
            return None
        try:
            price = float(trade["price"])
            size = float(trade["size"])
            minute_dt = floor_minute(trade["timestamp"])
        except (KeyError, TypeError, ValueError):
            return None
        if size <= 0:
            return None

        event_minute = to_iso(minute_dt)
        minute_epoch = int(minute_dt.timestamp())
        session_date = minute_dt.astimezone(MARKET_TIMEZONE).date().isoformat()
        price_bin = round(round(price / self.price_bin_size) * self.price_bin_size, 6)
        previous_session_date = self.last_session_date_by_symbol.get(symbol)
        if previous_session_date and previous_session_date != session_date:
            self.pending_session_rollovers[symbol] = session_date
        key = (symbol, minute_epoch, price_bin)
        current = self.bins.get(key)
        if current is None:
            current = self._new_bin(trade, symbol, event_minute, session_date, price_bin)
        volume_key, count_key = _side_keys(side)
        current[volume_key] += size
        current[count_key] += 1
        current["volume"] += size
        current["tradeCount"] += 1
        current["feed"] = trade.get("feed") or "unknown"
        current["updatedAt"] = _now_iso()
        self.bins[key] = current
        self.last_session_date_by_symbol[symbol] = session_date
        self._drop_stale(minute_epoch)
        return dict(current)

    def bins_for_minute(self, symbol: str, event_minute: str) -> list[dict[str, Any]]:
        normalized = str(symbol or "").upper()
        bins = [
            dict(value)
            for (bin_symbol, _minute_epoch, _price_bin), value in self.bins.items()
            if bin_symbol == normalized and value.get("eventMinute") == event_minute
        ]
        bins.sort(key=lambda item: item["priceBin"])
        return bins

    def restore_minute(self, symbol: str, event_minute: str, bins: list[dict[str, Any]]) -> int:
        normalized_symbol = str(symbol or "").upper()
        if normalized_symbol not in self.pinned_symbols or not event_minute:
            return 0
        try:
            minute_dt = datetime.fromisoformat(str(event_minute).replace("Z", "+00:00")).astimezone(timezone.utc)
        except (TypeError, ValueError):
            return 0
        minute_epoch = int(minute_dt.timestamp())
        restored = 0
        session_date = minute_dt.astimezone(MARKET_TIMEZONE).date().isoformat()
        for source in bins or []:
            if not isinstance(source, dict):
                continue
            if str(source.get("symbol") or normalized_symbol).upper() != normalized_symbol:
                continue
            if str(source.get("eventMinute") or event_minute) != event_minute:
                continue
            try:
                price_bin = round(float(source["priceBin"]), 6)
            except (KeyError, TypeError, ValueError):
                continue
            ask_volume = _nonnegative_number(source.get("askVolume"))
            bid_volume = _nonnegative_number(source.get("bidVolume"))
            unknown_volume = _nonnegative_number(source.get("unknownVolume"))
            ask_count = _nonnegative_int(source.get("askTradeCount"))
            bid_count = _nonnegative_int(source.get("bidTradeCount"))
            unknown_count = _nonnegative_int(source.get("unknownTradeCount"))
            value = {
                "eventType": "ORDER_FLOW_BIN",
                "eventMinute": event_minute,
                "sessionDate": str(source.get("sessionDate") or session_date),
                "symbol": normalized_symbol,
                "priceBin": price_bin,
                "priceBinSize": self.price_bin_size,
                "askVolume": ask_volume,
                "bidVolume": bid_volume,
                "unknownVolume": unknown_volume,
                "askTradeCount": ask_count,
                "bidTradeCount": bid_count,
                "unknownTradeCount": unknown_count,
                "volume": ask_volume + bid_volume + unknown_volume,
                "tradeCount": ask_count + bid_count + unknown_count,
                "sideClassification": source.get("sideClassification") or ORDER_FLOW_SIDE_CLASSIFICATION,
                "classificationVersion": source.get("classificationVersion") or ORDER_FLOW_CLASSIFICATION_VERSION,
                "source": source.get("source") or "alpaca",
                "feed": source.get("feed") or "unknown",
                "marketSession": "regular",
                "updatedAt": source.get("updatedAt") or _now_iso(),
            }
            self.bins[(normalized_symbol, minute_epoch, price_bin)] = value
            restored += 1
        if restored:
            self.last_session_date_by_symbol[normalized_symbol] = session_date
            self._drop_stale(minute_epoch)
        return restored

    def current_session_date(self, symbol: str) -> str | None:
        return self.last_session_date_by_symbol.get(str(symbol or "").upper())

    def consume_session_rollover(self, symbol: str) -> str | None:
        return self.pending_session_rollovers.pop(str(symbol or "").upper(), None)

    def _new_bin(self, trade: dict[str, Any], symbol: str, event_minute: str, session_date: str, price_bin: float) -> dict[str, Any]:
        return {
            "eventType": "ORDER_FLOW_BIN",
            "eventMinute": event_minute,
            "sessionDate": session_date,
            "symbol": symbol,
            "priceBin": price_bin,
            "priceBinSize": self.price_bin_size,
            "askVolume": 0.0,
            "bidVolume": 0.0,
            "unknownVolume": 0.0,
            "askTradeCount": 0,
            "bidTradeCount": 0,
            "unknownTradeCount": 0,
            "volume": 0.0,
            "tradeCount": 0,
            "sideClassification": ORDER_FLOW_SIDE_CLASSIFICATION,
            "classificationVersion": ORDER_FLOW_CLASSIFICATION_VERSION,
            "source": "alpaca",
            "feed": trade.get("feed") or "unknown",
            "marketSession": "regular",
            "updatedAt": _now_iso(),
        }

    def _drop_stale(self, minute_epoch: int) -> None:
        if self.last_seen_minute_epoch == minute_epoch:
            return
        self.last_seen_minute_epoch = minute_epoch
        self.sweep_count += 1
        cutoff = minute_epoch - 180
        stale_keys = [key for key in self.bins if key[1] < cutoff]
        for key in stale_keys:
            self.bins.pop(key, None)


def _side_keys(side: str) -> tuple[str, str]:
    if side == "ask":
        return "askVolume", "askTradeCount"
    if side == "bid":
        return "bidVolume", "bidTradeCount"
    return "unknownVolume", "unknownTradeCount"


def _nonnegative_number(value: Any) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
