from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SHARED = ROOT / "systems" / "market-data" / "shared"
FIXTURES = ROOT / "systems/market-data/tests/fixtures/chart_assets_v2"
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

from alfaka.analytics.geometry import analyze_geometry  # noqa: E402


class AnalysisEvalFixtureTest(unittest.TestCase):
    def test_recorded_public_market_series_are_valid_completed_candles(self):
        manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(manifest["series"]), 1)
        self.assertIn("Nasdaq", manifest["source"]["provider"])

        long_series = []
        for series in manifest["series"]:
            rows = json.loads((FIXTURES / series["file"]).read_text(encoding="utf-8"))
            self.assertEqual(len(rows), series["bars"])
            self.assertGreaterEqual(len(rows), 300)
            self.assertTrue(all(row["canonicalVersion"] == "v2" and row["isClosed"] for row in rows))
            self.assertTrue(all(float(row["low"]) > 0 and float(row["high"]) >= float(row["low"]) for row in rows))
            span = datetime.fromisoformat(rows[-1]["timestamp"].replace("Z", "+00:00")) - datetime.fromisoformat(rows[0]["timestamp"].replace("Z", "+00:00"))
            if len(rows) >= 1700 and span.days >= 365 * 7 - 7:
                long_series.append(series["symbol"])
        self.assertTrue(long_series)

    def test_geometry_kernel_is_deterministic_on_recorded_public_market_data(self):
        rows = json.loads((FIXTURES / "nvda-1d.json").read_text(encoding="utf-8"))[-380:]

        first = analyze_geometry("NVDA", "1D", rows)
        second = analyze_geometry("NVDA", "1D", rows)

        self.assertEqual(first, second)
        self.assertLessEqual(len(first["drawings"]), 8)
        self.assertLessEqual(len(first["supports"]), 2)
        self.assertLessEqual(len(first["resistances"]), 2)
        timestamps = {row["timestamp"] for row in rows}
        for drawing in first["drawings"]:
            self.assertIn(drawing["type"], {"horizontalLine", "trendLine"})
            self.assertEqual((drawing["symbol"], drawing["interval"]), ("NVDA", "1D"))
            self.assertEqual(drawing["sourceInterval"], "1D")
            self.assertEqual(drawing["style"]["lineStyle"], "solid")
            for anchor in drawing["anchors"]:
                self.assertIn(anchor["timestamp"], timestamps)

    def test_recorded_regression_window_does_not_create_two_point_only_asset(self):
        manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
        regression = next(item for item in manifest["episodes"] if item["episodeId"] == "nvda-2026-07-09-known_regression")
        self.assertEqual(regression["forbiddenEvidence"]["reason"], "two_point_only_and_no_current_relevance")
        self.assertEqual(regression["forbiddenEvidence"]["anchorWindow"], ["2025-09-17", "2025-09-25"])


if __name__ == "__main__":
    unittest.main()
