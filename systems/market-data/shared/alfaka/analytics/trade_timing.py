from __future__ import annotations

from typing import Any


TRADE_TIMING_VERSION = "pattern-trade-timing-v1"
DEFAULT_BREAKOUT_BUFFER_ATR = 0.25
DEFAULT_STOP_DISTANCE_ATR = 1.0
DEFAULT_MINIMUM_REWARD_RISK = 2.0
DEFAULT_PROJECTION_BARS = 10

_BULLISH_KINDS = {
    "ascending_triangle",
    "bullish_flag",
    "bullish_pennant",
    "bullish_rectangle",
    "falling_wedge",
    "descending_channel_breakout",
}
_BEARISH_KINDS = {
    "descending_triangle",
    "bearish_flag",
    "bearish_pennant",
    "bearish_rectangle",
    "rising_wedge",
    "ascending_channel_breakdown",
}
_POLE_TARGET_KINDS = {"bullish_flag", "bearish_flag", "bullish_pennant", "bearish_pennant"}


def evaluate_pattern_trade_timing(
    candles: list[dict[str, Any]],
    pattern: dict[str, Any] | None,
    *,
    atr: float,
    symbol: str,
    interval: str,
    minimum_reward_risk: float = DEFAULT_MINIMUM_REWARD_RISK,
    long_only: bool = True,
    projection_bars: int = DEFAULT_PROJECTION_BARS,
) -> dict[str, Any] | None:
    """Convert one detected chart pattern into a non-executable trade scenario.

    The result is deliberately a chart annotation plan, not an order. Only closed
    candles are considered, and an actionable entry is emitted only for a detector
    pattern whose state is already ``confirmed``.
    """

    if pattern is None:
        return None
    rows = [dict(row) for row in candles if row.get("isClosed", row.get("is_closed", True)) is not False]
    if not rows:
        return _empty_plan(pattern, minimum_reward_risk, projection_bars, "no_completed_candles")

    kind = str(pattern.get("kind") or "")
    state = str(pattern.get("state") or "")
    if state == "forming":
        return _empty_plan(pattern, minimum_reward_risk, projection_bars, "pattern_not_confirmed", action="watch")
    if state != "confirmed":
        return _empty_plan(pattern, minimum_reward_risk, projection_bars, "pattern_not_active")

    expected_direction = _expected_direction(kind, pattern.get("breakoutDirection"))
    actual_direction = pattern.get("breakoutDirection")
    if expected_direction is None or actual_direction != expected_direction:
        return _empty_plan(pattern, minimum_reward_risk, projection_bars, "breakout_direction_mismatch")

    atr_value = float(atr)
    if atr_value <= 0:
        return _empty_plan(pattern, minimum_reward_risk, projection_bars, "invalid_atr")
    if not pattern.get("upper") or not pattern.get("lower"):
        return _empty_plan(pattern, minimum_reward_risk, projection_bars, "missing_pattern_boundaries")

    buffer = DEFAULT_BREAKOUT_BUFFER_ATR * atr_value
    signal = _find_current_breakout(rows, pattern, direction=actual_direction, buffer=buffer)
    if signal is None:
        return _empty_plan(pattern, minimum_reward_risk, projection_bars, "confirmed_state_without_current_breakout")
    signal_index, breakout_level = signal
    signal_row = rows[signal_index]
    entry_price = float(signal_row["close"])
    upper_price = _boundary_price(pattern["upper"], rows, signal_index)
    lower_price = _boundary_price(pattern["lower"], rows, signal_index)
    measured_move = _measured_move(pattern)
    if upper_price is None or lower_price is None or measured_move <= 0:
        return _empty_plan(pattern, minimum_reward_risk, projection_bars, "invalid_pattern_geometry")

    if actual_direction == "up":
        structural_stop = lower_price - buffer
        tactical_stop = breakout_level - DEFAULT_STOP_DISTANCE_ATR * atr_value
        stop_price = max(structural_stop, tactical_stop)
        target_price = breakout_level + measured_move
        direction = "long"
        action = "buy_candidate"
        reasons = ["confirmed_upward_breakout"]
    else:
        structural_stop = upper_price + buffer
        tactical_stop = breakout_level + DEFAULT_STOP_DISTANCE_ATR * atr_value
        stop_price = min(structural_stop, tactical_stop)
        target_price = breakout_level - measured_move
        direction = "exit_long" if long_only else "short"
        action = "sell_candidate" if long_only else "short_candidate"
        reasons = ["confirmed_downward_breakout"]

    risk = abs(entry_price - stop_price)
    reward = max(0.0, abs(target_price - entry_price))
    valid_orientation = (
        stop_price < entry_price <= target_price
        if actual_direction == "up"
        else target_price <= entry_price < stop_price
    )
    if not valid_orientation or risk <= 0:
        return _empty_plan(pattern, minimum_reward_risk, projection_bars, "invalid_risk_geometry")

    reward_risk = reward / risk
    if action in {"buy_candidate", "short_candidate"}:
        if reward_risk < minimum_reward_risk:
            action = "no_trade"
            direction = None
            reasons.append("reward_risk_below_minimum")
        else:
            reasons.append("reward_risk_passed")
    else:
        reasons.append("long_position_exit_only")

    return {
        "version": TRADE_TIMING_VERSION,
        "symbol": symbol.strip().upper(),
        "interval": interval,
        "patternId": str(pattern.get("id") or pattern.get("geometryHash") or kind),
        "patternKind": kind,
        "patternState": state,
        "action": action,
        "direction": direction,
        "signalAt": str(signal_row["timestamp"]),
        "entryTrigger": _rounded(breakout_level + buffer if actual_direction == "up" else breakout_level - buffer),
        "entryPrice": _rounded(entry_price),
        "stopPrice": _rounded(stop_price),
        "targetPrice": _rounded(target_price),
        "riskPerShare": _rounded(risk),
        "rewardPerShare": _rounded(reward),
        "rewardRiskRatio": round(reward_risk, 4),
        "minimumRewardRisk": float(minimum_reward_risk),
        "projectionBars": max(1, int(projection_bars)),
        "reasons": reasons,
    }


def _expected_direction(kind: str, breakout_direction: Any) -> str | None:
    if kind in _BULLISH_KINDS:
        return "up"
    if kind in _BEARISH_KINDS:
        return "down"
    if kind == "symmetrical_triangle" and breakout_direction in {"up", "down"}:
        return str(breakout_direction)
    return None


def _find_current_breakout(
    rows: list[dict[str, Any]], pattern: dict[str, Any], *, direction: str, buffer: float,
) -> tuple[int, float] | None:
    for index in range(max(0, len(rows) - 2), len(rows)):
        boundary_name = "upper" if direction == "up" else "lower"
        boundary_price = _boundary_price(pattern[boundary_name], rows, index)
        if boundary_price is None:
            continue
        close = float(rows[index]["close"])
        if (direction == "up" and close > boundary_price + buffer) or (
            direction == "down" and close < boundary_price - buffer
        ):
            return index, boundary_price
    return None


def _boundary_price(boundary: dict[str, Any], rows: list[dict[str, Any]], index: int) -> float | None:
    start = boundary.get("start") or {}
    end = boundary.get("end") or {}
    try:
        start_price = float(start["price"])
        end_price = float(end["price"])
    except (KeyError, TypeError, ValueError):
        return None
    indexes = {str(row.get("timestamp")): position for position, row in enumerate(rows)}
    start_index = indexes.get(str(start.get("timestamp")))
    end_index = indexes.get(str(end.get("timestamp")))
    if start_index is None or end_index is None or end_index <= start_index:
        return end_price
    slope = (end_price - start_price) / (end_index - start_index)
    return start_price + slope * (index - start_index)


def _measured_move(pattern: dict[str, Any]) -> float:
    if pattern.get("kind") in _POLE_TARGET_KINDS and pattern.get("pole"):
        pole = pattern["pole"]
        try:
            return abs(float(pole["end"]["price"]) - float(pole["start"]["price"]))
        except (KeyError, TypeError, ValueError):
            return 0.0
    try:
        upper, lower = pattern["upper"], pattern["lower"]
        start_width = abs(float(upper["start"]["price"]) - float(lower["start"]["price"]))
        end_width = abs(float(upper["end"]["price"]) - float(lower["end"]["price"]))
        return max(start_width, end_width)
    except (KeyError, TypeError, ValueError):
        return 0.0


def _empty_plan(
    pattern: dict[str, Any], minimum_reward_risk: float, projection_bars: int, reason: str, *, action: str = "no_trade",
) -> dict[str, Any]:
    return {
        "version": TRADE_TIMING_VERSION,
        "symbol": None,
        "interval": None,
        "patternId": str(pattern.get("id") or pattern.get("geometryHash") or pattern.get("kind") or "unknown"),
        "patternKind": str(pattern.get("kind") or "unknown"),
        "patternState": str(pattern.get("state") or "unknown"),
        "action": action,
        "direction": None,
        "signalAt": None,
        "entryTrigger": None,
        "entryPrice": None,
        "stopPrice": None,
        "targetPrice": None,
        "riskPerShare": None,
        "rewardPerShare": None,
        "rewardRiskRatio": None,
        "minimumRewardRisk": float(minimum_reward_risk),
        "projectionBars": max(1, int(projection_bars)),
        "reasons": [reason],
    }


def _rounded(value: float) -> float:
    return round(float(value), 6)
