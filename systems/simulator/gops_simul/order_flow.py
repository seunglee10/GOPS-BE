"""Point-in-time order-flow projection over immutable simulator replay events."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from alfaka.alpaca.feed_profiles import market_session_for_datetime
from alfaka.orderflow import (
    ORDER_FLOW_CLASSIFICATION_VERSION,
    ORDER_FLOW_SIDE_CLASSIFICATION,
    classify_trade_side,
    price_bin_size_from_env,
    quote_future_tolerance_ms_from_env,
    quote_max_age_ms_from_env,
)
from alfaka.orderflow.classification import normalize_quote, normalize_trade

from gops_simul.dataset import DATASET_ID, REPLAY_SYMBOL_SET, REPLAY_SYMBOLS


MARKET_TIMEZONE = ZoneInfo("America/New_York")
DEFAULT_CACHE_SYMBOL_LIMIT = 8
DEFAULT_PAGE_SIZE = 50_000


class ReplayOrderFlowEvent(Protocol):
    sequence: int
    timestamp: datetime
    feed: str
    payload: dict[str, object]


class ReplayOrderFlowSource(Protocol):
    dataset_id: str

    def events_for_symbol_after(
        self,
        symbol: str,
        sequence: int,
        through: datetime,
        limit: int,
    ) -> list[ReplayOrderFlowEvent]: ...


@dataclass
class _MinuteProfile:
    updated_at: str
    updated_sequence: int
    bins: dict[float, list[float | int]] = field(default_factory=dict)


@dataclass
class _SymbolProjection:
    last_sequence: int = 0
    latest_quote: dict[str, object] | None = None
    session_date: str | None = None
    minutes: dict[str, _MinuteProfile] = field(default_factory=dict)


class ReplayOrderFlowProjection:
    """Lazily reconstructs and incrementally advances bounded symbol profiles."""

    def __init__(
        self,
        source: ReplayOrderFlowSource,
        *,
        cache_symbol_limit: int = DEFAULT_CACHE_SYMBOL_LIMIT,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        self.source = source
        self.cache_symbol_limit = max(1, int(cache_symbol_limit))
        self.page_size = max(1, int(page_size))
        self.price_bin_size = price_bin_size_from_env()
        self.quote_max_age_ms = quote_max_age_ms_from_env()
        self.quote_future_tolerance_ms = quote_future_tolerance_ms_from_env()
        self._run_id: str | None = None
        self._states: "OrderedDict[str, _SymbolProjection]" = OrderedDict()

    def reset(self, run_id: str | None = None) -> None:
        self._run_id = run_id
        self._states.clear()

    def snapshot(
        self,
        symbol: str,
        *,
        through: datetime,
        run_id: str,
        after_sequence: int | None = None,
        latest_only: bool = False,
    ) -> dict[str, object]:
        normalized = str(symbol or "").strip().upper()
        virtual_session_date = through.astimezone(MARKET_TIMEZONE).date().isoformat()
        if normalized not in REPLAY_SYMBOL_SET:
            return self._payload(
                normalized,
                virtual_session_date,
                through,
                run_id,
                data_status="unsupported",
                minutes=[],
                live_quote=None,
                next_sequence=0,
            )

        if self._run_id != run_id:
            self.reset(run_id)
        state = self._states.pop(normalized, None) or _SymbolProjection()
        self._advance(normalized, state, through)
        self._states[normalized] = state
        while len(self._states) > self.cache_symbol_limit:
            self._states.popitem(last=False)

        selected_minutes = sorted(state.minutes.items())
        if latest_only:
            selected_minutes = selected_minutes[-1:]
        elif after_sequence is not None:
            selected_minutes = [
                item for item in selected_minutes
                if item[1].updated_sequence > max(0, int(after_sequence))
            ]
        minutes = [
            {
                "eventMinute": event_minute,
                "updatedAt": profile.updated_at,
                "bins": [self._level_payload(price_bin, values) for price_bin, values in sorted(profile.bins.items())],
            }
            for event_minute, profile in selected_minutes
        ]
        return self._payload(
            normalized,
            state.session_date or virtual_session_date,
            through,
            run_id,
            data_status="ready" if minutes else "empty",
            minutes=minutes,
            live_quote=self._live_quote_payload(state.latest_quote),
            next_sequence=state.last_sequence,
        )

    def _advance(self, symbol: str, state: _SymbolProjection, through: datetime) -> None:
        while True:
            events = self.source.events_for_symbol_after(
                symbol,
                state.last_sequence,
                through,
                self.page_size,
            )
            if not events:
                return
            for event in events:
                self._apply_event(state, event)
                state.last_sequence = max(state.last_sequence, int(event.sequence))
            if len(events) < self.page_size:
                return

    def _apply_event(self, state: _SymbolProjection, event: ReplayOrderFlowEvent) -> None:
        payload = event.payload
        event_type = str(payload.get("T") or "")
        timestamp = _isoformat_milliseconds(event.timestamp)
        if event_type == "q":
            quote = normalize_quote({
                "timestamp": timestamp,
                "bidPrice": payload.get("bp"),
                "askPrice": payload.get("ap"),
                "bidSize": payload.get("bs"),
                "askSize": payload.get("as"),
                "feed": event.feed,
            })
            if quote is None or not _positive_quote(quote):
                state.latest_quote = None
            else:
                state.latest_quote = quote
            return

        if event_type != "t" or market_session_for_datetime(event.timestamp) != "regular":
            return
        trade = normalize_trade({
            "timestamp": timestamp,
            "price": payload.get("p"),
            "size": payload.get("s"),
            "feed": event.feed,
            "marketSession": "regular",
        })
        if trade is None:
            return
        session_date = event.timestamp.astimezone(MARKET_TIMEZONE).date().isoformat()
        if state.session_date != session_date:
            state.session_date = session_date
            state.minutes.clear()
        side = classify_trade_side(
            trade,
            state.latest_quote,
            max_quote_age_ms=self.quote_max_age_ms,
            future_tolerance_ms=self.quote_future_tolerance_ms,
        )
        event_minute = _isoformat_minute(event.timestamp)
        price = float(trade["price"])
        size = float(trade["size"])
        price_bin = round(round(price / self.price_bin_size) * self.price_bin_size, 6)
        minute = state.minutes.setdefault(
            event_minute,
            _MinuteProfile(updated_at=timestamp, updated_sequence=int(event.sequence)),
        )
        minute.updated_at = timestamp
        minute.updated_sequence = int(event.sequence)
        values = minute.bins.setdefault(price_bin, [0.0, 0.0, 0.0, 0, 0, 0])
        if side == "ask":
            values[0] = float(values[0]) + size
            values[3] = int(values[3]) + 1
        elif side == "bid":
            values[1] = float(values[1]) + size
            values[4] = int(values[4]) + 1
        else:
            values[2] = float(values[2]) + size
            values[5] = int(values[5]) + 1

    def _payload(
        self,
        symbol: str,
        session_date: str,
        through: datetime,
        run_id: str,
        *,
        data_status: str,
        minutes: list[dict[str, object]],
        live_quote: dict[str, object] | None,
        next_sequence: int,
    ) -> dict[str, object]:
        return {
            "symbol": symbol,
            "sessionDate": session_date,
            "priceBinSize": self.price_bin_size,
            "sideClassification": ORDER_FLOW_SIDE_CLASSIFICATION,
            "classificationVersion": ORDER_FLOW_CLASSIFICATION_VERSION,
            "marketSession": "regular",
            "dataStatus": data_status,
            "minutes": minutes,
            "liveQuote": live_quote,
            "supportedSymbols": list(REPLAY_SYMBOLS),
            "source": "simulation_replay",
            "simulation": True,
            "datasetId": getattr(self.source, "dataset_id", DATASET_ID),
            "runId": run_id,
            "virtualTime": _isoformat_milliseconds(through),
            "nextSequence": max(0, int(next_sequence)),
        }

    @staticmethod
    def _level_payload(price_bin: float, values: list[float | int]) -> dict[str, object]:
        return {
            "priceBin": price_bin,
            "askVolume": float(values[0]),
            "bidVolume": float(values[1]),
            "unknownVolume": float(values[2]),
            "askTradeCount": int(values[3]),
            "bidTradeCount": int(values[4]),
            "unknownTradeCount": int(values[5]),
        }

    @staticmethod
    def _live_quote_payload(quote: dict[str, object] | None) -> dict[str, object] | None:
        if not quote:
            return None
        return {
            "bidPrice": quote.get("bidPrice"),
            "askPrice": quote.get("askPrice"),
            "bidSize": quote.get("bidSize"),
            "askSize": quote.get("askSize"),
            "timestamp": quote.get("timestamp"),
        }


def _positive_quote(quote: dict[str, object]) -> bool:
    try:
        bid = float(quote.get("bidPrice") or 0)
        ask = float(quote.get("askPrice") or 0)
    except (TypeError, ValueError):
        return False
    return bid > 0 and ask > 0 and ask >= bid


def _isoformat_milliseconds(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _isoformat_minute(value: datetime) -> str:
    return value.astimezone(UTC).replace(second=0, microsecond=0).isoformat().replace("+00:00", "Z")
