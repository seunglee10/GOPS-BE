from __future__ import annotations

import json
from collections import Counter
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "systems/market-data/tests/fixtures/chart_assets_v2"


class AnalysisEvalFixtureTest(unittest.TestCase):
    def test_manifest_reuses_real_series_across_stratified_episodes(self):
        manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(manifest["episodes"]), 24)
        self.assertEqual({item["category"] for item in manifest["episodes"]}, {
            "strong_trend", "range", "gap_high_volatility", "weak_noise",
            "structure_transition", "known_regression",
        })
        self.assertEqual(len({item["episodeId"] for item in manifest["episodes"]}), len(manifest["episodes"]))
        self.assertIn("Nasdaq", manifest["source"]["provider"])
        holdout = Counter(
            item["expectation"] for item in manifest["episodes"]
            if item["split"] == "holdout"
        )
        self.assertGreaterEqual(holdout["must_draw"], 20)
        self.assertGreaterEqual(holdout["must_not_draw"], 20)
        fixture_bytes = sum(path.stat().st_size for path in FIXTURES.iterdir() if path.is_file())
        self.assertLessEqual(fixture_bytes, 2 * 1024 * 1024)
        for series in manifest["series"]:
            rows = json.loads((FIXTURES / series["file"]).read_text(encoding="utf-8"))
            self.assertEqual(len(rows), series["bars"])
            self.assertGreaterEqual(len(rows), 300)
            self.assertTrue(all(row["canonicalVersion"] == "v2" and row["isClosed"] for row in rows))
            self.assertTrue(all(float(row["low"]) > 0 and float(row["high"]) >= float(row["low"]) for row in rows))
        for episode in manifest["episodes"]:
            rows = json.loads((FIXTURES / episode["series"]).read_text(encoding="utf-8"))
            self.assertIn(episode["asOf"], {row["timestamp"] for row in rows})

    def test_nvda_recorded_regression_is_explicit(self):
        manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
        regression = next(item for item in manifest["episodes"] if item["episodeId"] == "nvda-2026-07-09-known_regression")
        self.assertEqual(regression["forbiddenEvidence"]["anchorWindow"], ["2025-09-17", "2025-09-25"])
        self.assertEqual(regression["forbiddenEvidence"]["reason"], "two_point_only_and_no_current_relevance")


if __name__ == "__main__":
    unittest.main()
