from __future__ import annotations

import math

from backend.schemas import Candle, MarketSignal, MarketSummary


def calculate_market_summary(symbol: str, timeframe: str, candles: list[Candle]) -> MarketSummary:
    if len(candles) < 2:
        latest = candles[-1] if candles else None
        timestamp = latest.timestamp if latest else ""
        price = latest.close if latest else 0.0
        return MarketSummary(
            symbol=symbol,
            timeframe=timeframe,  # type: ignore[arg-type]
            latestPrice=price,
            latestTimestamp=timestamp,
            changePercentFromFirstVisible=0.0,
            visibleHigh=price,
            visibleLow=price,
            visibleVolume=latest.volume if latest else 0.0,
            averageVolume=latest.volume if latest else 0.0,
            realizedVolatility=0.0,
            trend="insufficient_data",
            notableSignals=[],
        )

    first = candles[0]
    latest = candles[-1]
    visible_high = max(candle.high for candle in candles)
    visible_low = min(candle.low for candle in candles)
    visible_volume = sum(candle.volume for candle in candles)
    average_volume = visible_volume / len(candles)
    change_percent = ((latest.close - first.close) / first.close) * 100 if first.close else 0.0
    returns = []
    for previous, current in zip(candles, candles[1:]):
        if previous.close > 0 and current.close > 0:
            returns.append(math.log(current.close / previous.close))
    realized_volatility = math.sqrt(sum(value * value for value in returns) / len(returns)) * 100 if returns else 0.0

    if change_percent >= 2.5:
        trend = "strong_up"
    elif change_percent >= 0.45:
        trend = "up"
    elif change_percent <= -2.5:
        trend = "strong_down"
    elif change_percent <= -0.45:
        trend = "down"
    else:
        trend = "sideways"

    signals: list[MarketSignal] = []
    if latest.volume > average_volume * 1.8:
        signals.append(
            MarketSignal(
                type="volume_spike",
                severity="high" if latest.volume > average_volume * 2.5 else "medium",
                message=f"{symbol} volume is elevated versus the visible average.",
                timestamp=latest.timestamp,
            )
        )
    if latest.high >= visible_high:
        signals.append(
            MarketSignal(
                type="new_visible_high",
                severity="medium",
                message=f"{symbol} is pressing the visible-range high.",
                timestamp=latest.timestamp,
            )
        )
    if latest.low <= visible_low:
        signals.append(
            MarketSignal(
                type="new_visible_low",
                severity="medium",
                message=f"{symbol} is testing the visible-range low.",
                timestamp=latest.timestamp,
            )
        )

    return MarketSummary(
        symbol=symbol,
        timeframe=timeframe,  # type: ignore[arg-type]
        latestPrice=latest.close,
        latestTimestamp=latest.timestamp,
        changePercentFromFirstVisible=round(change_percent, 4),
        visibleHigh=visible_high,
        visibleLow=visible_low,
        visibleVolume=visible_volume,
        averageVolume=average_volume,
        realizedVolatility=round(realized_volatility, 4),
        trend=trend,  # type: ignore[arg-type]
        notableSignals=signals[:5],
    )
