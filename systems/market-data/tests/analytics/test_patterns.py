from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
if str(MARKET_SHARED) not in sys.path:
    sys.path.insert(0, str(MARKET_SHARED))

from alfaka.analytics.patterns import compute_patterns  # noqa: E402
from alfaka.analytics import compute_feature_pack  # noqa: E402


def _row(index: int, *, high: float, low: float, close: float, volume: float = 1_000) -> dict:
    timestamp = datetime(2025, 1, 2, tzinfo=timezone.utc) + timedelta(minutes=5 * index)
    return {
        "timestamp": timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "candleKey": timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "barIndex": index,
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "isClosed": True,
        "interval": "5m",
    }


def _pivot(rows: list[dict], index: int, kind: str, price: float) -> dict:
    return {
        "id": f"5m:pivot:{kind}:{index}",
        "timestamp": rows[index]["timestamp"],
        "candleKey": rows[index]["candleKey"],
        "barIndex": index,
        "price": price,
        "kind": kind,
        "grade": "structural",
        "strength": 0.9,
    }


def _triangle(kind: str, *, confirmed: bool = False, wrong_break: bool = False) -> tuple[list[dict], list[dict]]:
    rows: list[dict] = []
    for index in range(120):
        distance = index - 20
        if kind == "ascending_triangle":
            upper, lower = 100.0, 90.0 + 0.08 * distance
        elif kind == "descending_triangle":
            upper, lower = 110.0 - 0.08 * distance, 100.0
        else:
            upper, lower = 110.0 - 0.05 * distance, 90.0 + 0.05 * distance
        close = (upper + lower) / 2
        rows.append(_row(index, high=upper, low=lower, close=close))

    if confirmed:
        upper = rows[-1]["high"]
        lower = rows[-1]["low"]
        expected_up = kind != "descending_triangle"
        if wrong_break:
            expected_up = not expected_up
        close = upper + 0.6 if expected_up else lower - 0.6
        rows[-1].update({"open": close, "high": max(upper, close), "low": min(lower, close), "close": close, "volume": 2_000})

    high_indexes = (20, 50, 80)
    low_indexes = (30, 60, 90)
    pivots = [
        *[_pivot(rows, index, "H", rows[index]["high"]) for index in high_indexes],
        *[_pivot(rows, index, "L", rows[index]["low"]) for index in low_indexes],
    ]
    return rows, pivots


def _flag(direction: str, *, confirmed: bool = False) -> tuple[list[dict], list[dict]]:
    rows = [_row(index, high=101, low=99, close=100) for index in range(50)]
    sign = 1 if direction == "bullish_flag" else -1
    for step in range(10):
        close = 100 + sign * (step + 1)
        rows.append(_row(len(rows), high=close + 0.4, low=close - 0.4, close=close))
    pole_end = len(rows) - 1
    for step in range(20):
        center = 110 - 0.12 * step if sign > 0 else 90 + 0.12 * step
        rows.append(_row(len(rows), high=center + 1, low=center - 1, close=center))
    if confirmed:
        upper, lower = rows[-1]["high"], rows[-1]["low"]
        close = upper + 0.6 if sign > 0 else lower - 0.6
        rows[-1].update({"open": close, "high": max(upper, close), "low": min(lower, close), "close": close, "volume": 2_000})
    pivots = [
        _pivot(rows, 49, "L" if sign > 0 else "H", rows[49]["low" if sign > 0 else "high"]),
        _pivot(rows, pole_end, "H" if sign > 0 else "L", rows[pole_end]["high" if sign > 0 else "low"]),
    ]
    return rows, pivots


def _continuation(kind: str, *, confirmed: bool = False) -> tuple[list[dict], list[dict]]:
    rows = [_row(index, high=101, low=99, close=100) for index in range(50)]
    bullish = kind.startswith("bullish_")
    sign = 1 if bullish else -1
    for step in range(10):
        close = 100 + sign * (step + 1)
        rows.append(_row(len(rows), high=close + 0.4, low=close - 0.4, close=close))
    pole_end = len(rows) - 1
    for step in range(20):
        if kind.endswith("pennant"):
            upper = 109.6 - 0.07 * step if bullish else 94.2 - 0.07 * step
            lower = 105.8 + 0.07 * step if bullish else 90.4 + 0.07 * step
        else:
            upper = 109.6 if bullish else 94.0
            lower = 106.0 if bullish else 90.4
        rows.append(_row(len(rows), high=upper, low=lower, close=(upper + lower) / 2))
    if confirmed:
        boundary = rows[-2]["high" if bullish else "low"]
        close = boundary + 0.6 if bullish else boundary - 0.6
        rows[-1].update({
            "open": close,
            "high": max(float(rows[-1]["high"]), close),
            "low": min(float(rows[-1]["low"]), close),
            "close": close,
            "volume": 2_000,
        })
    pivots = [
        _pivot(rows, 49, "L" if bullish else "H", rows[49]["low" if bullish else "high"]),
        _pivot(rows, pole_end, "H" if bullish else "L", rows[pole_end]["high" if bullish else "low"]),
    ]
    return rows, pivots


def _wedge(kind: str) -> tuple[list[dict], list[dict]]:
    rows = []
    for index in range(120):
        distance = index - 20
        if kind == "rising_wedge":
            upper, lower = 110.0 + 0.10 * distance, 100.0 + 0.14 * distance
        else:
            upper, lower = 110.0 - 0.14 * distance, 100.0 - 0.10 * distance
        rows.append(_row(index, high=upper, low=lower, close=(upper + lower) / 2))
    high_indexes = (20, 50, 80)
    low_indexes = (30, 60, 90)
    return rows, [
        *[_pivot(rows, index, "H", rows[index]["high"]) for index in high_indexes],
        *[_pivot(rows, index, "L", rows[index]["low"]) for index in low_indexes],
    ]


def _channel_break(kind: str) -> tuple[list[dict], list[dict]]:
    rows = []
    descending = kind == "descending_channel_breakout"
    slope = -0.08 if descending else 0.08
    for index in range(120):
        distance = index - 20
        upper = 110.0 + slope * distance
        lower = 104.0 + slope * distance
        rows.append(_row(index, high=upper, low=lower, close=(upper + lower) / 2))
    for index in (118, 119):
        distance = index - 20
        upper = 110.0 + slope * distance
        lower = 104.0 + slope * distance
        close = upper + 0.6 if descending else lower - 0.6
        rows[index].update({
            "open": close,
            "high": max(upper, close + 0.2),
            "low": min(lower, close - 0.2),
            "close": close,
            "volume": 2_000,
        })
    high_indexes = (20, 50, 80)
    low_indexes = (30, 60, 90)
    return rows, [
        *[_pivot(rows, index, "H", rows[index]["high"]) for index in high_indexes],
        *[_pivot(rows, index, "L", rows[index]["low"]) for index in low_indexes],
    ]


def _best(rows: list[dict], pivots: list[dict]) -> dict:
    patterns = compute_patterns(rows, pivots, atr=1.0, interval="5m")
    return next(item for item in patterns if item["hardPass"])


def test_detects_three_forming_triangle_kinds() -> None:
    for expected in ("ascending_triangle", "descending_triangle", "symmetrical_triangle"):
        rows, pivots = _triangle(expected)

        pattern = _best(rows, pivots)

        assert pattern["kind"] == expected
        assert pattern["state"] == "forming"
        assert pattern["touches"] >= 5
        assert pattern["containment"] >= 0.85


def test_detects_recent_triangle_when_older_pivots_distort_full_window_fit() -> None:
    rows, pivots = _triangle("ascending_triangle")
    distractors = (
        (2, "H", 130.0),
        (8, "H", 115.0),
        (14, "H", 125.0),
        (5, "L", 65.0),
        (11, "L", 80.0),
        (17, "L", 70.0),
    )
    for index, kind, price in distractors:
        rows[index]["high" if kind == "H" else "low"] = price
        pivots.append(_pivot(rows, index, kind, price))

    pattern = _best(rows, pivots)

    assert pattern["kind"] == "ascending_triangle"
    assert pattern["state"] == "forming"
    assert pattern["geometry"]["lower"]["start"]["timestamp"] > rows[17]["timestamp"]
    assert pattern["convergenceRatio"] <= 0.80


def test_detects_triangle_from_contiguous_pivot_subset_with_recent_wick_outliers() -> None:
    rows, pivots = _triangle("ascending_triangle")
    outliers = ((100, "H", 120.0), (105, "L", 70.0))
    for index, kind, price in outliers:
        rows[index]["high" if kind == "H" else "low"] = price
        pivots.append(_pivot(rows, index, kind, price))

    pattern = _best(rows, pivots)

    assert pattern["kind"] == "ascending_triangle"
    assert pattern["upperTouches"] == 3
    assert pattern["lowerTouches"] == 3
    assert all(not ref.endswith((":100", ":105")) for ref in pattern["evidenceRefs"])


def test_accepts_slightly_slower_triangle_convergence() -> None:
    rows, pivots = _triangle("ascending_triangle")
    for index, row in enumerate(rows):
        lower = 90.0 + 0.017 * (index - 20)
        row["low"] = lower
        row["close"] = (row["high"] + lower) / 2
    for pivot in pivots:
        if pivot["kind"] == "L":
            pivot["price"] = rows[pivot["barIndex"]]["low"]

    pattern = _best(rows, pivots)

    assert pattern["kind"] == "ascending_triangle"
    assert 0.80 < pattern["convergenceRatio"] <= 0.85


def test_expected_triangle_breakout_is_confirmed_but_wrong_direction_is_rejected() -> None:
    rows, pivots = _triangle("ascending_triangle", confirmed=True)
    confirmed = _best(rows, pivots)
    assert confirmed["state"] == "confirmed"
    assert confirmed["breakoutDirection"] == "up"

    wrong_rows, wrong_pivots = _triangle("ascending_triangle", confirmed=True, wrong_break=True)
    assert not any(item["hardPass"] for item in compute_patterns(wrong_rows, wrong_pivots, atr=1.0, interval="5m"))


def test_detects_bullish_and_bearish_flags_in_both_states() -> None:
    for expected in ("bullish_flag", "bearish_flag"):
        forming_rows, forming_pivots = _flag(expected)
        forming = _best(forming_rows, forming_pivots)
        assert forming["kind"] == expected
        assert forming["state"] == "forming"
        assert 0.10 <= forming["retracementRatio"] <= 0.50

        confirmed_rows, confirmed_pivots = _flag(expected, confirmed=True)
        confirmed = _best(confirmed_rows, confirmed_pivots)
        assert confirmed["kind"] == expected
        assert confirmed["state"] == "confirmed"


def test_detects_bullish_and_bearish_pennants_in_both_states() -> None:
    for expected in ("bullish_pennant", "bearish_pennant"):
        forming_rows, forming_pivots = _continuation(expected)
        forming = _best(forming_rows, forming_pivots)
        assert forming["kind"] == expected
        assert forming["state"] == "forming"
        assert 0.15 <= forming["convergenceRatio"] <= 0.85

        confirmed_rows, confirmed_pivots = _continuation(expected, confirmed=True)
        confirmed = _best(confirmed_rows, confirmed_pivots)
        assert confirmed["kind"] == expected
        assert confirmed["state"] == "confirmed"


def test_detects_bullish_and_bearish_rectangles_in_both_states() -> None:
    for expected in ("bullish_rectangle", "bearish_rectangle"):
        forming_rows, forming_pivots = _continuation(expected)
        forming = _best(forming_rows, forming_pivots)
        assert forming["kind"] == expected
        assert forming["state"] == "forming"
        assert forming["upperTouches"] >= 2
        assert forming["lowerTouches"] >= 2

        confirmed_rows, confirmed_pivots = _continuation(expected, confirmed=True)
        confirmed = _best(confirmed_rows, confirmed_pivots)
        assert confirmed["kind"] == expected
        assert confirmed["state"] == "confirmed"


def test_detects_rising_and_falling_wedges_without_relabeling_them_as_triangles() -> None:
    for expected in ("rising_wedge", "falling_wedge"):
        rows, pivots = _wedge(expected)

        pattern = _best(rows, pivots)

        assert pattern["kind"] == expected
        assert pattern["state"] == "forming"
        assert pattern["convergenceRatio"] < 0.85


def test_detects_only_confirmed_directional_channel_breakouts() -> None:
    for expected, direction in (
        ("descending_channel_breakout", "up"),
        ("ascending_channel_breakdown", "down"),
    ):
        rows, pivots = _channel_break(expected)

        pattern = _best(rows, pivots)

        assert pattern["kind"] == expected
        assert pattern["state"] == "confirmed"
        assert pattern["breakoutDirection"] == direction


def test_non_converging_triangle_is_not_emitted() -> None:
    rows, pivots = _triangle("ascending_triangle")
    for index, row in enumerate(rows):
        row["low"] = 90.0 - 0.03 * index
        row["close"] = (row["high"] + row["low"]) / 2
    for pivot in pivots:
        if pivot["kind"] == "L":
            pivot["price"] = rows[pivot["barIndex"]]["low"]

    patterns = compute_patterns(rows, pivots, atr=1.0, interval="5m")

    assert not any(item["hardPass"] and item["kind"] == "ascending_triangle" for item in patterns)


def test_recorded_public_market_episodes_keep_flag_state_and_direction() -> None:
    fixture_root = ROOT / "systems" / "market-data" / "tests" / "fixtures" / "chart_assets_v2"
    episodes = (
        ("nvda-1d.json", 950, "bullish_flag", "confirmed"),
        ("wmt-1d.json", 490, "bearish_flag", "confirmed"),
        ("aapl-1d.json", 200, "bullish_flag", "forming"),
        ("aapl-1d.json", 1440, "bearish_flag", "forming"),
    )
    for filename, end, expected_kind, expected_state in episodes:
        rows = json.loads((fixture_root / filename).read_text(encoding="utf-8"))[:end]

        patterns = compute_feature_pack(rows, "1D")["patterns"]
        pattern = next(item for item in patterns if item["hardPass"])

        assert (pattern["kind"], pattern["state"]) == (expected_kind, expected_state)
        candle_times = {row["timestamp"] for row in rows[-500:]}
        for geometry in pattern["geometry"].values():
            for point in geometry.values():
                assert point["timestamp"] in candle_times
