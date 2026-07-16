from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[4]
SHARED = ROOT / "systems" / "market-data" / "shared"
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

from alfaka.analytics.geometry import (  # noqa: E402
    ALGORITHM_VERSION,
    EVALUATION_BARS,
    MINIMUM_BARS,
    SUPPORTED_INTERVALS,
    TARGET_BARS,
    _level_drawing,
    analyze_geometry,
    compute_sma_snapshot,
)
from alfaka.analytics.atr import latest_atr  # noqa: E402


class GeometryAssetKernelTest(unittest.TestCase):
    def test_interval_contract_and_coverage_windows_are_exact(self):
        self.assertEqual(SUPPORTED_INTERVALS, ("1m", "5m", "10m", "1h", "4h", "1D", "1W"))
        self.assertEqual(ALGORITHM_VERSION, "ohlcv-consensus-pattern-families-v5")
        self.assertEqual(MINIMUM_BARS, 120)
        for interval in SUPPORTED_INTERVALS[:-1]:
            self.assertEqual(TARGET_BARS[interval], 380)
            self.assertEqual(EVALUATION_BARS[interval], 260)
        self.assertEqual(TARGET_BARS["1W"], 312)
        self.assertEqual(EVALUATION_BARS["1W"], 192)

    def test_sma_periods_are_completed_bar_counts_for_every_interval(self):
        rows = _rows(121, interval="5m", closes=[float(index + 1) for index in range(121)])

        snapshot = compute_sma_snapshot(rows)

        self.assertEqual(snapshot["sma60"], 91.5)
        self.assertEqual(snapshot["sma120"], 61.5)
        self.assertEqual(snapshot["cross"]["status"], "none")

    def test_exactly_120_bars_has_sma_but_not_previous_bar_cross_state(self):
        snapshot = compute_sma_snapshot(_rows(120, interval="1h"))

        self.assertIsNotNone(snapshot["sma60"])
        self.assertIsNotNone(snapshot["sma120"])
        self.assertEqual(snapshot["cross"]["status"], "insufficient_previous_bar")

    def test_sma_cross_snapshot_preserves_the_interpolated_cross_price(self):
        scenarios = {
            "golden": ([101.0] * 60 + [100.0] * 60 + [171.0], 100.986111),
            "dead": ([100.0] * 60 + [101.0] * 60 + [30.0], 100.013889),
        }
        for direction, (closes, expected_price) in scenarios.items():
            with self.subTest(direction=direction):
                rows = _rows(121, interval="1D", closes=closes)

                cross = compute_sma_snapshot(rows)["cross"]

                self.assertEqual(cross["status"], "crossed")
                self.assertEqual(cross["direction"], direction)
                self.assertEqual(cross["timestamp"], rows[-1]["timestamp"])
                self.assertEqual(cross["previousTimestamp"], rows[-2]["timestamp"])
                self.assertGreaterEqual(cross["fraction"], 0)
                self.assertLessEqual(cross["fraction"], 1)
                self.assertEqual(cross["price"], expected_price)
                self.assertNotEqual(cross["price"], rows[-1]["close"])

    def test_support_and_resistance_drawings_use_dashed_lines(self):
        anchors = [
            {"timestamp": "2026-07-10T00:00:00.000Z", "price": 100.0},
            {"timestamp": "2026-07-11T00:00:00.000Z", "price": 100.0},
        ]

        support = _level_drawing("NVDA", "1D", {"id": "support", "role": "support", "anchors": anchors}, anchors[-1]["timestamp"])
        resistance = _level_drawing("NVDA", "1D", {"id": "resistance", "role": "resistance", "anchors": anchors}, anchors[-1]["timestamp"])

        self.assertEqual(support["style"]["lineDash"], [6, 4])
        self.assertEqual(resistance["style"]["lineDash"], [6, 4])
        self.assertEqual(support["style"]["lineStyle"], "dashed")
        self.assertEqual(resistance["style"]["lineStyle"], "dashed")

    def test_geometry_anchors_are_real_canonical_timestamps_and_budget_is_six(self):
        rows = _triangle_rows(180, interval="10m")

        result = analyze_geometry("NVDA", "10m", rows)

        timestamps = {row["timestamp"] for row in rows}
        self.assertLessEqual(len(result["drawings"]), 8)
        self.assertLessEqual(len(result["supports"]), 2)
        self.assertLessEqual(len(result["resistances"]), 2)
        self.assertNotIn("bullish_flag", str(result))
        self.assertNotIn("bearish_flag", str(result))
        for drawing in result["drawings"]:
            self.assertIn(drawing["type"], {"horizontalLine", "trendLine"})
            self.assertTrue(drawing["id"].startswith("chart-asset:NVDA:10m:"))
            self.assertEqual((drawing["symbol"], drawing["interval"], drawing["sourceInterval"]), ("NVDA", "10m", "10m"))
            for anchor in drawing["anchors"]:
                self.assertIn(anchor["timestamp"], timestamps)
                self.assertGreater(anchor["price"], 0)
        if result["primaryTriangle"]:
            self.assertIn(
                result["primaryTriangle"]["kind"],
                {"ascending_triangle", "descending_triangle", "symmetrical_triangle"},
            )
            self.assertTrue(all(item["style"]["lineStyle"] == "solid" for item in result["drawings"][-2:]))

    def test_recorded_market_levels_are_active_relevant_and_role_consistent(self):
        fixture_root = ROOT / "systems" / "market-data" / "tests" / "fixtures" / "chart_assets_v2"
        manifest = json.loads((fixture_root / "manifest.json").read_text(encoding="utf-8"))
        emitted = 0

        for series in manifest["series"]:
            with self.subTest(symbol=series["symbol"]):
                rows = json.loads((fixture_root / series["file"]).read_text(encoding="utf-8"))[-TARGET_BARS["1D"]:]
                result = analyze_geometry(series["symbol"], "1D", rows)
                current = float(rows[-1]["close"])
                atr = latest_atr(rows)
                levels = [*result["supports"], *result["resistances"]]
                emitted += len(levels)

                for level in levels:
                    self.assertGreaterEqual(level["touches"], 3)
                    if level["selectionTier"] == "confirmed":
                        self.assertLessEqual(level["currentDistanceAtr"], 2.0)
                        self.assertGreaterEqual(level["reactionCount"], 2)
                        self.assertTrue(level["activePass"])
                        self.assertTrue(level["hardPass"])
                    else:
                        self.assertEqual(level["selectionTier"], "contextual")
                        self.assertLessEqual(level["currentDistanceAtr"], 3.0)
                        self.assertGreaterEqual(level["reactionCount"], 1)
                        self.assertFalse(level["hardPass"])
                    self.assertIn(level["state"], {
                        "support_active", "resistance_active",
                        "role_flip_support", "role_flip_resistance",
                    })
                    if level["role"] == "support":
                        self.assertGreaterEqual(current, float(level["zoneLow"]) - 0.25 * atr)
                    else:
                        self.assertLessEqual(current, float(level["zoneHigh"]) + 0.25 * atr)

        self.assertGreater(emitted, 0)

    def test_amd_adds_only_the_nearest_contextual_support_without_replacing_confirmed_resistance(self):
        fixture = ROOT / "systems" / "market-data" / "tests" / "fixtures" / "chart_assets_v2" / "amd-1d.json"
        rows = json.loads(fixture.read_text(encoding="utf-8"))[-TARGET_BARS["1D"]:]

        result = analyze_geometry("AMD", "1D", rows)

        self.assertEqual([(item["price"], item["selectionTier"]) for item in result["supports"]], [(469.21, "contextual")])
        self.assertEqual([(item["price"], item["selectionTier"]) for item in result["resistances"]], [(546.44, "confirmed")])
        self.assertEqual(len(result["supports"]), 1)

    def test_contextual_level_publication_does_not_change_pattern_or_trade_plan(self):
        fixture = ROOT / "systems" / "market-data" / "tests" / "fixtures" / "chart_assets_v2" / "amd-1d.json"
        rows = json.loads(fixture.read_text(encoding="utf-8"))[-TARGET_BARS["1D"]:]

        with patch("alfaka.analytics.geometry._contextual_level_pass", return_value=False):
            strict_only = analyze_geometry("AMD", "1D", rows)
        contextual = analyze_geometry("AMD", "1D", rows)

        self.assertEqual(strict_only["primaryPattern"], contextual["primaryPattern"])
        self.assertEqual(strict_only["patterns"], contextual["patterns"])
        self.assertEqual(strict_only["tradePlan"], contextual["tradePlan"])
        self.assertEqual(
            strict_only["primaryPattern"]["geometryHash"] if strict_only["primaryPattern"] else None,
            contextual["primaryPattern"]["geometryHash"] if contextual["primaryPattern"] else None,
        )

    def test_known_aapl_noise_window_does_not_emit_horizontal_levels(self):
        fixture_root = ROOT / "systems" / "market-data" / "tests" / "fixtures" / "chart_assets_v2"
        manifest = json.loads((fixture_root / "manifest.json").read_text(encoding="utf-8"))
        episode = next(
            item for item in manifest["episodes"]
            if item["episodeId"] == "aapl-2026-07-09-known_regression"
        )
        rows = [
            row for row in json.loads((fixture_root / episode["series"]).read_text(encoding="utf-8"))
            if row["timestamp"] <= episode["asOf"]
        ]

        result = analyze_geometry(episode["symbol"], "1D", rows)

        self.assertEqual(result["supports"], [])
        self.assertEqual(result["resistances"], [])

    def test_geometry_promotes_recorded_flag_to_primary_pattern_and_drawings(self):
        fixture = ROOT / "systems" / "market-data" / "tests" / "fixtures" / "chart_assets_v2" / "nvda-1d.json"
        rows = json.loads(fixture.read_text(encoding="utf-8"))[:950]

        result = analyze_geometry("NVDA", "1D", rows)

        self.assertEqual(result["primaryPattern"]["kind"], "bullish_flag")
        self.assertEqual(result["primaryPattern"]["state"], "confirmed")
        self.assertEqual(result["tradePlan"]["action"], "buy_candidate")
        self.assertEqual(result["tradePlan"]["direction"], "long")
        self.assertIn(result["tradePlan"]["signalAt"], {row["timestamp"] for row in rows})
        self.assertEqual(result["patterns"][0]["geometryHash"], result["primaryPattern"]["geometryHash"])
        pattern_drawings = [
            drawing for drawing in result["drawings"]
            if result["primaryPattern"]["geometryHash"] in drawing["id"]
        ]
        self.assertEqual(len(pattern_drawings), 3)
        self.assertTrue(any("상승 깃발형" in drawing["label"] for drawing in pattern_drawings))

    def test_geometry_uses_previous_regression_triangle_detector(self):
        for interval in SUPPORTED_INTERVALS:
            with self.subTest(interval=interval):
                rows = _triangle_rows(180, interval=interval)

                result = analyze_geometry("NVDA", interval, rows)

                self.assertIsNotNone(result["primaryTriangle"])
                self.assertEqual(result["primaryTriangle"]["kind"], "ascending_triangle")
                triangle_drawings = [drawing for drawing in result["drawings"] if drawing["type"] == "trendLine"]
                self.assertEqual(len(triangle_drawings), 2)
                self.assertTrue(all("상승 삼각형" in drawing["label"] for drawing in triangle_drawings))

    def test_rejects_less_than_120_completed_bars(self):
        with self.assertRaisesRegex(ValueError, "120 completed candles"):
            analyze_geometry("NVDA", "1D", _rows(119, interval="1D"))


def _rows(count: int, *, interval: str, closes: list[float] | None = None) -> list[dict]:
    steps = {
        "1m": timedelta(minutes=1), "5m": timedelta(minutes=5), "10m": timedelta(minutes=10),
        "1h": timedelta(hours=1), "4h": timedelta(hours=4), "1D": timedelta(days=1),
        "1W": timedelta(weeks=1),
    }
    step = steps[interval]
    started = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)
    values = closes or [100.0 + index * 0.05 for index in range(count)]
    return [
        {
            "candleKey": (started + step * index).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "timestamp": (started + step * index).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "barIndex": index,
            "open": close - 0.2,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1_000_000 + index * 1_000,
            "isClosed": True,
            "interval": interval,
        }
        for index, close in enumerate(values)
    ]


def _triangle_rows(count: int, *, interval: str) -> list[dict]:
    rows = _rows(count, interval=interval)
    start = count - 100
    for index in range(start, count):
        progress = index - start
        upper = 120.0
        lower = 100.0 + progress * 0.16
        phase = progress % 10
        if phase == 0:
            close = lower + 0.1
        elif phase == 5:
            close = upper - 0.1
        else:
            close = (upper + lower) / 2
        rows[index].update({
            "open": close - 0.15,
            "high": upper if phase == 5 else close + 0.4,
            "low": lower if phase == 0 else close - 0.4,
            "close": close,
            "volume": 1_500_000 if phase in {0, 5} else 900_000,
        })
    return rows


if __name__ == "__main__":
    unittest.main()
