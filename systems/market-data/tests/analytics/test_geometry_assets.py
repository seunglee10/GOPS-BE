from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


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
    analyze_geometry,
    compute_sma_snapshot,
)


class GeometryAssetKernelTest(unittest.TestCase):
    def test_interval_contract_and_coverage_windows_are_exact(self):
        self.assertEqual(SUPPORTED_INTERVALS, ("1m", "5m", "10m", "1h", "4h", "1D", "1W"))
        self.assertEqual(ALGORITHM_VERSION, "ohlcv-consensus-pattern-families-v1")
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

    def test_geometry_promotes_recorded_flag_to_primary_pattern_and_drawings(self):
        fixture = ROOT / "systems" / "market-data" / "tests" / "fixtures" / "chart_assets_v2" / "nvda-1d.json"
        rows = json.loads(fixture.read_text(encoding="utf-8"))[:950]

        result = analyze_geometry("NVDA", "1D", rows)

        self.assertEqual(result["primaryPattern"]["kind"], "bullish_flag")
        self.assertEqual(result["primaryPattern"]["state"], "confirmed")
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
