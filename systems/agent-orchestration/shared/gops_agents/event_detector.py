from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import MarketEvent


@dataclass
class MarketEventThresholds:
    price_change_percent: float = 3.0
    volume_spike_multiplier: float = 3.0
    volatility_percent: float = 4.0


@dataclass
class MarketEventDetector:
    thresholds: MarketEventThresholds = field(default_factory=MarketEventThresholds)
    previous_price_by_symbol: dict[str, float] = field(default_factory=dict)
    previous_volume_by_symbol: dict[str, float] = field(default_factory=dict)

    def detect(self, payload: dict[str, Any], source_topic: str) -> list[MarketEvent]:
        symbol = str(payload.get("symbol") or "UNKNOWN").upper()
        events: list[MarketEvent] = []
        price = first_float(payload, "price", "close", "lastPrice")
        volume = first_float(payload, "volume", "size")
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

        if volume is not None:
            previous_volume = self.previous_volume_by_symbol.get(symbol)
            if previous_volume and previous_volume > 0 and volume >= previous_volume * self.thresholds.volume_spike_multiplier:
                multiplier = volume / previous_volume
                events.append(MarketEvent.from_payload(
                    symbol=symbol,
                    event_type="volume_spike",
                    severity="alert" if multiplier >= 5 else "watch",
                    source_topic=source_topic,
                    summary=f"{symbol} volume rose {multiplier:.2f}x from the previous observed volume.",
                    observed_at=str(timestamp) if timestamp else None,
                    metrics={"volume": volume, "previousVolume": previous_volume, "multiplier": round(multiplier, 4)},
                ))
            if volume > 0:
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


def severity_for_change(value: float) -> str:
    if value >= 10:
        return "critical"
    if value >= 5:
        return "alert"
    if value >= 3:
        return "watch"
    return "info"
