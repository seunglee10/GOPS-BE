from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[4]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
if str(MARKET_SHARED) not in sys.path:
    sys.path.insert(0, str(MARKET_SHARED))

from market_data.analytics.patterns import compute_patterns  # noqa: E402
from market_data.analytics import compute_feature_pack  # noqa: E402
import market_data.analytics.geometry as geometry_kernel  # noqa: E402


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


def _all_pattern_fixtures() -> dict[str, tuple[list[dict], list[dict]]]:
    cases = {
        kind: _triangle(kind)
        for kind in ("ascending_triangle", "descending_triangle", "symmetrical_triangle")
    }
    cases.update({kind: _flag(kind) for kind in ("bullish_flag", "bearish_flag")})
    cases.update({
        kind: _continuation(kind)
        for kind in ("bullish_pennant", "bearish_pennant", "bullish_rectangle", "bearish_rectangle")
    })
    cases.update({kind: _wedge(kind) for kind in ("rising_wedge", "falling_wedge")})
    cases.update({
        kind: _channel_break(kind)
        for kind in ("descending_channel_breakout", "ascending_channel_breakdown")
    })
    return cases


def _geometry_pattern_projection(
    expected_kind: str,
    rows: list[dict],
    pivots: list[dict],
) -> dict:
    # Some detector fixtures intentionally contain fewer than the asset
    # pipeline's 120-bar minimum. Keep their detector geometry untouched and
    # isolate only unrelated asset inputs while exercising analyze_geometry's
    # actual public projection, primary selection, timing, and drawing code.
    runtime_pivots = [
        {
            **pivot,
            "confirmedAt": pivot["timestamp"],
            "reversalAtr": 1.0,
            "prominenceAtr": 1.0,
        }
        for pivot in pivots
    ]
    indicator_stub = {
        "sma60": None,
        "sma120": None,
        "cross": {"status": "data_insufficient"},
    }
    with (
        patch.object(geometry_kernel, "MINIMUM_BARS", 20),
        patch.object(geometry_kernel, "compute_pivots", return_value=runtime_pivots),
        patch.object(geometry_kernel, "_wilder_atr", return_value=[1.0] * len(rows)),
        patch.object(geometry_kernel, "regression_atr", return_value=1.0),
        patch.object(geometry_kernel, "compute_sma_snapshot", return_value=indicator_stub),
    ):
        result = geometry_kernel.analyze_geometry("SYNTH", "5m", rows)

    primary = result["primaryPattern"]
    assert primary is not None
    assert primary["kind"] == expected_kind
    drawing_by_id = {drawing["id"]: drawing for drawing in result["drawings"]}
    pattern_drawings = [
        {
            "id": drawing_by_id[drawing_id]["id"],
            "anchors": drawing_by_id[drawing_id]["anchors"],
            "label": drawing_by_id[drawing_id]["label"],
            "style": drawing_by_id[drawing_id]["style"],
            "geometryHash": primary["geometryHash"],
        }
        for drawing_id in result["drawingGroups"]["pattern"]
    ]
    return {
        "patterns": result["patterns"],
        "primaryPattern": primary,
        "primaryTriangle": result["primaryTriangle"],
        "confirmation": primary.get("confirmation"),
        "tradePlan": result["tradePlan"],
        "patternDrawings": pattern_drawings,
    }


def test_all_thirteen_pattern_families_keep_the_canonical_candidate_golden() -> None:
    expected = {
        "ascending_channel_breakdown": "2877eaf18c92cfa81e18129ffa8f3af6a2fedc016245a7f1f511a0ef012b8e6f",
        "ascending_triangle": "7b040f8b38adcebf66fdfb86b5bc5187db6bc9b7d47013c7844e2405d084fbca",
        "bearish_flag": "02a2914bbc452c0d9faa94b837ed38ef5b9d5b5d0d25be31ba547d2e3ce442bd",
        "bearish_pennant": "5a0ec932abda619aec8cc1dfe84c3bf89c9fc6ae08e06ad135d9f8cccb83f48a",
        "bearish_rectangle": "bdfe925db847cd4af921160d7346c70aa74d38b6b15eafb86e50814d3779b748",
        "bullish_flag": "4f5ceaf6c8ffd474b0413a29164396048ee2d73a23ac7775091e100900116428",
        "bullish_pennant": "0037516547f47857f2067e1b5573ae8b7dfc62eea618d99de7ab0faea0d69703",
        "bullish_rectangle": "519b63e8db8d4479c1a8e964d47c17b0f3bd25d4b95f5037f763bda59c057ac1",
        "descending_channel_breakout": "e46b430d57a423abcb7a83fedef464b717c622959c039fedee86b88fa2c4a746",
        "descending_triangle": "b8783661440d81951a5ca6e0dd636d1f8826fa347f04652602c2dd596aafe7c9",
        "falling_wedge": "1c79d4c1cebb48b01486df621158a9fd449bbb947ef60cc7675c2d11fe53da35",
        "rising_wedge": "7bb3cc597ded60cf6db7caa62689ab9899322b926fc507deae8bc6d9aa74fd19",
        "symmetrical_triangle": "97ec3af766bad9d466471498cfc0f78669995b226ab5a47d25a4a87316b2984f",
    }
    cases = {}
    for kind in ("ascending_triangle", "descending_triangle", "symmetrical_triangle"):
        rows, pivots = _triangle(kind)
        cases[kind] = _best(rows, pivots)
    for kind in ("bullish_flag", "bearish_flag"):
        rows, pivots = _flag(kind)
        cases[kind] = _best(rows, pivots)
    for kind in ("bullish_pennant", "bearish_pennant", "bullish_rectangle", "bearish_rectangle"):
        rows, pivots = _continuation(kind)
        cases[kind] = _best(rows, pivots)
    for kind in ("rising_wedge", "falling_wedge"):
        rows, pivots = _wedge(kind)
        cases[kind] = _best(rows, pivots)
    for kind in ("descending_channel_breakout", "ascending_channel_breakdown"):
        rows, pivots = _channel_break(kind)
        cases[kind] = _best(rows, pivots)

    assert set(cases) == set(expected)
    for kind, candidate in cases.items():
        encoded = json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        assert hashlib.sha256(encoded.encode()).hexdigest() == expected[kind]


def test_all_thirteen_pattern_families_keep_the_geometry_public_projection_golden() -> None:
    expected = {
        "ascending_channel_breakdown": "733d71a56d240237050a816ec40ee08d5841b737419ca58ef4b706f203679c73",
        "ascending_triangle": "192fe128dacef1dadc2ed91f4567ba0c632c5fbde48b9d18821a37f65fd4da4a",
        "bearish_flag": "2af5a1f5656e6388cc915fa7a4e10b2c25cb53958dd7b45d06857ffdee624fc8",
        "bearish_pennant": "7264c41ed0d03dcf7b39f1f916c911b7f528e5cc84d70860b8245f993e79d688",
        "bearish_rectangle": "fcaa313910f7b0784cfa3d1fe674cfb167854752174a15c267dc5070e32740ff",
        "bullish_flag": "326a13be0987379b6311f1dfb27b8067aedccf6f26ceae43ea077e2a5815ca73",
        "bullish_pennant": "67866b6bd9c92ebcb9940d91ba1d0c248b2f5dfed3a80d15d7cb8818bea4b543",
        "bullish_rectangle": "26d29e170eee7fc11d4c4dfeb0766e7fb536a2fece1e06bd5814a1ef970c5794",
        "descending_channel_breakout": "6b801003dd9b455279d75d141f90eaec8b06cf81e03d1b42c4e98d62cc145460",
        "descending_triangle": "a7b26d87dcef8b6f9febf0ef1d14cd5fec58f176f4a609adc540a63fc52397a2",
        "falling_wedge": "4f457b55759043528973545da0c67e787b9f7dbf36d989b8c656ea68c9d5ff03",
        "rising_wedge": "9e9d01393ffbfe77a174740937956a83d48b334a470ee0fe483d3da47e85a7e8",
        "symmetrical_triangle": "031c4a197cb68d001729aaa7238bdb31bb66e5e724a83bdc90512a86aa43929f",
    }
    projections = {
        kind: _geometry_pattern_projection(kind, rows, pivots)
        for kind, (rows, pivots) in _all_pattern_fixtures().items()
    }

    assert set(projections) == set(expected)
    for kind, projection in projections.items():
        encoded = json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        assert hashlib.sha256(encoded.encode()).hexdigest() == expected[kind]


def test_competitor_diagnostics_are_opt_in_and_keep_the_public_projection_exact() -> None:
    rows, pivots = _triangle("ascending_triangle")

    public = compute_patterns(rows, pivots, atr=1.0, interval="5m")
    diagnostics = compute_patterns(
        rows,
        pivots,
        atr=1.0,
        interval="5m",
        retain_competitors=True,
    )
    expected_public = [
        *[item for item in diagnostics if item["hardPass"]][:1],
        *[item for item in diagnostics if not item["hardPass"]][:3],
    ]

    assert public == expected_public
    assert sum(item["hardPass"] for item in diagnostics) > 1
    assert len([item for item in diagnostics if not item["hardPass"]]) >= len([
        item for item in public if not item["hardPass"]
    ])


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
