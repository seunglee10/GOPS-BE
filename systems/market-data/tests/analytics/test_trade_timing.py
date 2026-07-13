from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
if str(MARKET_SHARED) not in sys.path:
    sys.path.insert(0, str(MARKET_SHARED))

from alfaka.analytics.trade_timing import evaluate_pattern_trade_timing  # noqa: E402


def test_confirmed_bullish_flag_becomes_buy_candidate_with_measured_move():
    rows = _rows([96.0, 96.5, 97.0, 97.5, 98.5])
    pattern = _pattern(
        "bullish_flag",
        rows,
        breakout_direction="up",
        upper=(98.0, 98.0),
        lower=(94.0, 96.0),
        pole=(88.0, 98.0),
    )

    plan = evaluate_pattern_trade_timing(rows, pattern, atr=1.0, symbol="aapl", interval="5m")

    assert plan is not None
    assert plan["action"] == "buy_candidate"
    assert plan["direction"] == "long"
    assert plan["signalAt"] == rows[-1]["timestamp"]
    assert plan["entryTrigger"] == 98.25
    assert plan["entryPrice"] == 98.5
    assert plan["stopPrice"] == 97.0
    assert plan["targetPrice"] == 108.0
    assert plan["rewardRiskRatio"] == 6.3333
    assert plan["reasons"] == ["confirmed_upward_breakout", "reward_risk_passed"]


def test_forming_pattern_is_watch_only_and_never_marks_an_entry():
    rows = _rows([96.0, 96.5, 97.0, 97.5, 97.8])
    pattern = _pattern(
        "ascending_triangle",
        rows,
        state="forming",
        breakout_direction=None,
        upper=(98.0, 98.0),
        lower=(94.0, 96.0),
    )

    plan = evaluate_pattern_trade_timing(rows, pattern, atr=1.0, symbol="AAPL", interval="5m")

    assert plan is not None
    assert plan["action"] == "watch"
    assert plan["signalAt"] is None
    assert plan["reasons"] == ["pattern_not_confirmed"]


def test_bearish_pattern_defaults_to_long_position_exit_instead_of_short_entry():
    rows = _rows([104.0, 103.5, 103.0, 102.5, 101.5])
    pattern = _pattern(
        "bearish_flag",
        rows,
        breakout_direction="down",
        upper=(106.0, 104.0),
        lower=(102.0, 102.0),
        pole=(112.0, 102.0),
    )

    plan = evaluate_pattern_trade_timing(rows, pattern, atr=1.0, symbol="AAPL", interval="5m")
    short_plan = evaluate_pattern_trade_timing(
        rows,
        pattern,
        atr=1.0,
        symbol="AAPL",
        interval="5m",
        long_only=False,
    )

    assert plan is not None and short_plan is not None
    assert plan["action"] == "sell_candidate"
    assert plan["direction"] == "exit_long"
    assert short_plan["action"] == "short_candidate"
    assert short_plan["direction"] == "short"
    assert short_plan["stopPrice"] == 103.0
    assert short_plan["targetPrice"] == 92.0


def test_low_reward_risk_rejects_new_long_entry():
    rows = _rows([96.0, 96.5, 97.0, 97.5, 98.5])
    pattern = _pattern(
        "bullish_flag",
        rows,
        breakout_direction="up",
        upper=(98.0, 98.0),
        lower=(94.0, 96.0),
        pole=(97.5, 98.0),
    )

    plan = evaluate_pattern_trade_timing(rows, pattern, atr=1.0, symbol="AAPL", interval="5m")

    assert plan is not None
    assert plan["action"] == "no_trade"
    assert plan["rewardRiskRatio"] == 0.0
    assert plan["reasons"][-1] == "reward_risk_below_minimum"


def test_unclosed_candle_is_never_used_as_signal_time_or_entry_price():
    rows = _rows([96.0, 96.5, 97.0, 97.5, 98.5])
    open_row = dict(rows[-1])
    open_row.update({
        "timestamp": _timestamp(5),
        "candleKey": _timestamp(5),
        "close": 150.0,
        "high": 151.0,
        "low": 149.0,
        "isClosed": False,
    })
    pattern = _pattern(
        "bullish_flag",
        rows,
        breakout_direction="up",
        upper=(98.0, 98.0),
        lower=(94.0, 96.0),
        pole=(88.0, 98.0),
    )

    plan = evaluate_pattern_trade_timing([*rows, open_row], pattern, atr=1.0, symbol="AAPL", interval="5m")

    assert plan is not None
    assert plan["signalAt"] == rows[-1]["timestamp"]
    assert plan["entryPrice"] == 98.5


def test_every_supported_directional_pattern_maps_to_the_expected_entry_side():
    bullish_kinds = (
        "ascending_triangle",
        "bullish_flag",
        "bullish_pennant",
        "bullish_rectangle",
        "falling_wedge",
        "descending_channel_breakout",
    )
    bearish_kinds = (
        "descending_triangle",
        "bearish_flag",
        "bearish_pennant",
        "bearish_rectangle",
        "rising_wedge",
        "ascending_channel_breakdown",
    )
    bullish_rows = _rows([96.0, 96.5, 97.0, 97.5, 98.5])
    bearish_rows = _rows([104.0, 103.5, 103.0, 102.5, 101.5])

    for kind in bullish_kinds:
        pole = (88.0, 98.0) if kind in {"bullish_flag", "bullish_pennant"} else None
        plan = evaluate_pattern_trade_timing(
            bullish_rows,
            _pattern(kind, bullish_rows, breakout_direction="up", upper=(98.0, 98.0), lower=(94.0, 96.0), pole=pole),
            atr=1.0,
            symbol="AAPL",
            interval="5m",
        )
        assert plan is not None and plan["action"] == "buy_candidate", kind

    for kind in bearish_kinds:
        pole = (112.0, 102.0) if kind in {"bearish_flag", "bearish_pennant"} else None
        plan = evaluate_pattern_trade_timing(
            bearish_rows,
            _pattern(kind, bearish_rows, breakout_direction="down", upper=(106.0, 104.0), lower=(102.0, 102.0), pole=pole),
            atr=1.0,
            symbol="AAPL",
            interval="5m",
            long_only=False,
        )
        assert plan is not None and plan["action"] == "short_candidate", kind


def _pattern(
    kind: str,
    rows: list[dict],
    *,
    state: str = "confirmed",
    breakout_direction: str | None,
    upper: tuple[float, float],
    lower: tuple[float, float],
    pole: tuple[float, float] | None = None,
) -> dict:
    pattern = {
        "id": f"pattern-{kind}",
        "kind": kind,
        "state": state,
        "breakoutDirection": breakout_direction,
        "score": 0.91,
        "geometryHash": f"hash-{kind}",
        "upper": {
            "start": {"timestamp": rows[0]["timestamp"], "price": upper[0]},
            "end": {"timestamp": rows[-1]["timestamp"], "price": upper[1]},
        },
        "lower": {
            "start": {"timestamp": rows[0]["timestamp"], "price": lower[0]},
            "end": {"timestamp": rows[-1]["timestamp"], "price": lower[1]},
        },
    }
    if pole:
        pattern["pole"] = {
            "start": {"timestamp": rows[0]["timestamp"], "price": pole[0]},
            "end": {"timestamp": rows[-2]["timestamp"], "price": pole[1]},
        }
    return pattern


def _rows(closes: list[float]) -> list[dict]:
    return [
        {
            "timestamp": _timestamp(index),
            "candleKey": _timestamp(index),
            "barIndex": index,
            "open": close - 0.2,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1_000,
            "isClosed": True,
            "interval": "5m",
        }
        for index, close in enumerate(closes)
    ]


def _timestamp(index: int) -> str:
    value = datetime(2026, 7, 13, 13, 30, tzinfo=timezone.utc) + timedelta(minutes=5 * index)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
