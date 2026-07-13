from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
import time
from typing import Any

from ..contracts import MarketEvent


@dataclass
class MarketEventThresholds:
    price_change_percent: float = 3.0
    volume_spike_multiplier: float = 3.0
    volatility_percent: float = 4.0
    volume_baseline_window: int = 20
    volume_min_samples: int = 5
    volume_event_cooldown_seconds: int = 1800


@dataclass
class MarketEventDetector:
    thresholds: MarketEventThresholds = field(default_factory=MarketEventThresholds)
    previous_price_by_symbol: dict[str, float] = field(default_factory=dict)
    previous_volume_by_symbol: dict[str, float] = field(default_factory=dict)
    volume_history_by_stream: dict[tuple[str, str], deque[float]] = field(default_factory=dict)
    last_volume_event_at_by_stream: dict[tuple[str, str], float] = field(default_factory=dict)

    def detect(self, payload: dict[str, Any], source_topic: str) -> list[MarketEvent]:
        symbol = str(payload.get("symbol") or "UNKNOWN").upper()
        events: list[MarketEvent] = []
        price = first_float(payload, "price", "close", "lastPrice")
        timestamp = payload.get("timestamp") or payload.get("eventTime") or payload.get("updatedAt")

        if price is not None:
            previous = self.previous_price_by_symbol.get(symbol)
            if previous and previous > 0:
                change_percent = ((price - previous) / previous) * 100
                if abs(change_percent) >= self.thresholds.price_change_percent:
                    event_type = "price_surge" if change_percent > 0 else "price_drop"
                    severity = severity_for_change(abs(change_percent))
                    events.append(MarketEvent.from_payload(
                        symbol=symbol,
                        event_type=event_type,
                        severity=severity,
                        source_topic=source_topic,
                        summary=f"{symbol} moved {change_percent:.2f}% from the previous observed price.",
                        observed_at=str(timestamp) if timestamp else None,
                        metrics={"price": price, "previousPrice": previous, "changePercent": round(change_percent, 4)},
                    ))
            self.previous_price_by_symbol[symbol] = price

        open_price = first_float(payload, "open")
        high = first_float(payload, "high")
        low = first_float(payload, "low")
        if open_price and high is not None and low is not None and open_price > 0:
            range_percent = ((high - low) / open_price) * 100
            if range_percent >= self.thresholds.volatility_percent:
                events.append(MarketEvent.from_payload(
                    symbol=symbol,
                    event_type="volatility_expansion",
                    severity=severity_for_change(range_percent),
                    source_topic=source_topic,
                    summary=f"{symbol} candle range expanded to {range_percent:.2f}% of open.",
                    observed_at=str(timestamp) if timestamp else None,
                    metrics={"open": open_price, "high": high, "low": low, "rangePercent": round(range_percent, 4)},
                ))

        interval = closed_candle_interval(payload, source_topic)
        volume = first_float(payload, "volume") if interval else None
        if interval and volume is not None and volume > 0:
            stream_key = (symbol, interval)
            window = max(1, int(self.thresholds.volume_baseline_window))
            minimum_samples = max(1, min(window, int(self.thresholds.volume_min_samples)))
            history = self.volume_history_by_stream.get(stream_key)
            if history is None or history.maxlen != window:
                history = deque(history or (), maxlen=window)
                self.volume_history_by_stream[stream_key] = history

            if len(history) >= minimum_samples:
                baseline_volume = sum(history) / len(history)
                if baseline_volume > 0 and volume >= baseline_volume * self.thresholds.volume_spike_multiplier:
                    multiplier = volume / baseline_volume
                    observed_seconds = timestamp_seconds(timestamp)
                    if observed_seconds is None:
                        observed_seconds = time.time()
                    last_event_seconds = self.last_volume_event_at_by_stream.get(stream_key)
                    cooldown_seconds = max(0, int(self.thresholds.volume_event_cooldown_seconds))
                    cooldown_elapsed = (
                        last_event_seconds is None
                        or observed_seconds - last_event_seconds >= cooldown_seconds
                    )
                    if cooldown_elapsed:
                        events.append(MarketEvent.from_payload(
                            symbol=symbol,
                            event_type="volume_spike",
                            severity="alert" if multiplier >= 5 else "watch",
                            source_topic=source_topic,
                            summary=(
                                f"{symbol} {interval} candle volume rose {multiplier:.2f}x "
                                "above its rolling baseline."
                            ),
                            observed_at=str(timestamp) if timestamp else None,
                            metrics={
                                "volume": volume,
                                "previousVolume": round(baseline_volume, 4),
                                "baselineVolume": round(baseline_volume, 4),
                                "baselineSamples": len(history),
                                "interval": interval,
                                "multiplier": round(multiplier, 4),
                            },
                        ))
                        self.last_volume_event_at_by_stream[stream_key] = observed_seconds

            history.append(volume)
            self.previous_volume_by_symbol[symbol] = volume

        return events


def first_float(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key not in payload:
            continue
        try:
            return float(payload[key])
        except (TypeError, ValueError):
            continue
    return None


_CLOSED_CANDLE_TOPIC = re.compile(r"(?:^|\.)candles\.([^.]+)\.closed(?:\.|$)", re.IGNORECASE)


def closed_candle_interval(payload: dict[str, Any], source_topic: str) -> str | None:
    match = _CLOSED_CANDLE_TOPIC.search(str(source_topic or ""))
    if match is None:
        return None

    state = str(payload.get("state") or "").strip().lower()
    if state and state != "closed":
        return None
    if payload.get("isClosed") is False or payload.get("is_closed") is False:
        return None

    return normalize_interval(payload.get("interval") or match.group(1))


def normalize_interval(value: Any) -> str:
    interval = str(value or "").strip()
    if interval in {"1D", "1W", "1M"}:
        return interval
    normalized = interval.lower()
    return {
        "1d": "1D",
        "1w": "1W",
        "1mo": "1M",
        "1month": "1M",
    }.get(normalized, normalized)


def timestamp_seconds(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def severity_for_change(value: float) -> str:
    if value >= 10:
        return "critical"
    if value >= 5:
        return "alert"
    if value >= 3:
        return "watch"
    return "info"
