from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "systems/market-data/tests/fixtures/chart_assets_v2"
EVALUATOR = ROOT / "scripts/local/eval-chart-assets-v2.py"


def load_evaluator():
    spec = importlib.util.spec_from_file_location("chart_asset_evaluator", EVALUATOR)
    if spec is None or spec.loader is None:
        raise RuntimeError("chart asset evaluator could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        # Five seven-year, split-adjusted canary histories plus the compact
        # stratified corpus remain bounded enough for fast local evaluation.
        self.assertLessEqual(fixture_bytes, 5 * 1024 * 1024)
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
        self.assertTrue(long_series, "the evaluator needs at least one real 7-year series for 1M coverage")
        for episode in manifest["episodes"]:
            rows = json.loads((FIXTURES / episode["series"]).read_text(encoding="utf-8"))
            self.assertIn(episode["asOf"], {row["timestamp"] for row in rows})

    def test_nvda_recorded_regression_is_explicit(self):
        manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
        regression = next(item for item in manifest["episodes"] if item["episodeId"] == "nvda-2026-07-09-known_regression")
        self.assertEqual(regression["forbiddenEvidence"]["anchorWindow"], ["2025-09-17", "2025-09-25"])
        self.assertEqual(regression["forbiddenEvidence"]["reason"], "two_point_only_and_no_current_relevance")

    def test_interval_labels_are_optional_and_strict_when_present(self):
        evaluator = load_evaluator()
        legacy = {
            "episodeId": "legacy",
            "expectation": "must_draw",
            "expectedSemanticTypes": ["trend"],
        }
        self.assertEqual(evaluator.normalize_expected_by_interval(legacy), {})
        labeled = {
            "episodeId": "labeled",
            "expectedByInterval": {
                "1D": {"expectation": "must_draw", "expectedSemanticTypes": ["level", "trend"]},
                "1W": {"expectation": "must_not_draw", "semanticTypes": []},
            },
        }
        self.assertEqual(evaluator.normalize_expected_by_interval(labeled), {
            "1D": {"expectation": "must_draw", "semanticTypes": ["level", "trend"]},
            "1W": {"expectation": "must_not_draw", "semanticTypes": []},
        })
        with self.assertRaises(ValueError):
            evaluator.normalize_expected_by_interval({
                "episodeId": "invalid",
                "expectedByInterval": {"1D": {"expectation": "must_not_draw", "semanticTypes": ["trend"]}},
            })

    def test_explicit_interval_metrics_never_infer_legacy_labels(self):
        evaluator = load_evaluator()
        results = [
            {
                "episodeId": "daily-hit", "split": "holdout", "evaluationRound": "round-1",
                "expectation": "must_draw", "expectedSemanticTypes": [],
                "expectedByInterval": {"1D": {"expectation": "must_draw", "semanticTypes": ["trend"]}},
                "drawingCounts": {"1D": 1},
                "intervalStates": {"1D": {"evaluated": True, "qualityState": "ready"}},
            },
            {
                "episodeId": "weekly-zero", "split": "holdout", "evaluationRound": "round-1",
                "expectation": "must_draw", "expectedSemanticTypes": [],
                "expectedByInterval": {"1W": {"expectation": "must_draw", "semanticTypes": ["level"]}},
                "drawingCounts": {"1W": 0},
                "intervalStates": {"1W": {"evaluated": True, "qualityState": "ready"}},
            },
            {
                "episodeId": "legacy-only", "split": "holdout", "evaluationRound": "round-1",
                "expectation": "must_draw", "expectedSemanticTypes": ["level"],
                "expectedByInterval": {}, "drawingCounts": {"1M": 1},
                "intervalStates": {"1M": {"evaluated": True, "qualityState": "ready"}},
            },
            {
                "episodeId": "daily-unnecessary", "split": "holdout", "evaluationRound": "round-1",
                "expectation": "may_draw", "expectedSemanticTypes": [],
                "expectedByInterval": {"1D": {"expectation": "must_not_draw", "semanticTypes": []}},
                "drawingCounts": {"1D": 1},
                "intervalStates": {"1D": {"evaluated": True, "qualityState": "ready"}},
            },
        ]
        reviews = [
            {
                "episodeId": "daily-hit", "split": "holdout", "evaluationRound": "round-1",
                "interval": "1D", "semanticType": "trend", "meaningful": True,
                "clearlyMeaningless": False, "offscreen": False, "scores": {},
            },
            {
                "episodeId": "legacy-only", "split": "holdout", "evaluationRound": "round-1",
                "interval": "1M", "semanticType": "level", "meaningful": True,
                "clearlyMeaningless": False, "offscreen": False, "scores": {},
            },
        ]
        quality = evaluator.quality_summary(reviews, results, ("1D", "1W"))
        self.assertEqual(quality["denominators"]["explicitMustDrawIntervalLabels"], 2)
        self.assertEqual(quality["explicitIntervalEvaluation"]["overallRecall"]["numerator"], 1)
        self.assertEqual(quality["explicitIntervalEvaluation"]["readyFalseZeroRate"]["numerator"], 1)
        self.assertEqual(quality["explicitSemanticEvaluation"]["overallRecall"]["denominator"], 2)
        self.assertEqual(quality["explicitSemanticEvaluation"]["recallByIntervalSemantic"]["1W"]["level"]["point"], 0.0)
        self.assertEqual(quality["legacyExpectedSemanticEvaluation"]["overall"]["denominator"], 1)
        self.assertEqual(
            quality["explicitIntervalEvaluation"]["mustNotDrawUnnecessaryRate"],
            {"numerator": 1, "denominator": 1, "point": 1.0, "wilson95": [0.206549, 1]},
        )


if __name__ == "__main__":
    unittest.main()
