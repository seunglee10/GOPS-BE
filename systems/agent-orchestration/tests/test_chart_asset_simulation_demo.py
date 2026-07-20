from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SHARED = ROOT / "systems" / "agent-orchestration" / "shared"
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
for path in (MARKET_SHARED, SHARED):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from gops_agents.chart_assets.simulation_demo import (  # noqa: E402
    NVDA_SIMULATION_DEMO_DATASET_ID,
    is_complete_nvda_simulation_demo_snapshot,
    project_nvda_simulation_demo_snapshot,
)


class ChartAssetSimulationDemoTest(unittest.TestCase):
    def test_projection_keeps_canonical_identity_and_clamps_geometry_before_commentary(self):
        base = _base_asset()

        projected = project_nvda_simulation_demo_snapshot(
            dataset_id=NVDA_SIMULATION_DEMO_DATASET_ID,
            base_asset=base,
            source_asset=_source_asset(),
        )

        self.assertEqual(projected["asOf"], base["asOf"])
        self.assertEqual(projected["inputDigest"], base["inputDigest"])
        self.assertEqual(projected["coverage"], base["coverage"])
        self.assertEqual(projected["indicators"], base["indicators"])
        self.assertNotIn("commentary", projected)
        self.assertEqual(
            projected["geometry"]["drawings"][-1]["anchors"][-1]["timestamp"],
            base["asOf"],
        )
        self.assertEqual(
            projected["geometry"]["patterns"][0]["confirmation"]["confirmedAt"],
            base["asOf"],
        )
        trade_plan = projected["geometry"]["tradePlan"]
        self.assertEqual((trade_plan["action"], trade_plan["direction"]), ("buy_candidate", "long"))
        self.assertIn("simulation_demo_reward_risk_override", trade_plan["reasons"])

    def test_complete_snapshot_requires_matching_v5_identity_and_drawing_references(self):
        projected = project_nvda_simulation_demo_snapshot(
            dataset_id=NVDA_SIMULATION_DEMO_DATASET_ID,
            base_asset=_base_asset(),
            source_asset=_source_asset(),
        )
        projected["commentary"] = _commentary(projected)

        self.assertTrue(is_complete_nvda_simulation_demo_snapshot(projected))

        projected["commentary"]["sourceIdentity"]["geometryInputDigest"] = "sha256:other"
        self.assertFalse(is_complete_nvda_simulation_demo_snapshot(projected))

    def test_projection_is_scoped_to_the_fixed_dataset_symbol_and_interval(self):
        base = _base_asset()

        projected = project_nvda_simulation_demo_snapshot(
            dataset_id="another-dataset",
            base_asset=base,
            source_asset=None,
        )

        self.assertEqual(projected, base)
        self.assertIsNot(projected, base)


def _base_asset():
    as_of = "2026-07-13T04:00:00.000Z"
    return {
        "assetVersion": "geometry",
        "algorithmVersion": "ohlcv-consensus-pattern-families-v6",
        "symbol": "NVDA",
        "interval": "1D",
        "sourceInterval": "1D",
        "asOf": as_of,
        "generatedAt": "2026-07-20T00:00:00.000Z",
        "status": "ready",
        "inputDigest": "sha256:canonical-cutoff",
        "coverage": {"lastActualClosedAt": as_of, "state": "full"},
        "geometry": {"drawings": []},
        "indicators": {"sma60": 1},
    }


def _source_asset():
    future = "2026-07-16T04:00:00.000Z"
    pattern_id = "nvda-falling-wedge"
    drawings = [
        {"id": "level-1", "type": "horizontalLine", "anchors": [{"timestamp": future, "price": 180}]},
        {"id": "trend-1", "type": "trendLine", "anchors": [{"timestamp": future, "price": 185}]},
        {"id": "pattern-upper", "type": "trendLine", "anchors": [{"timestamp": future, "price": 190}]},
    ]
    return {
        "assetVersion": "geometry",
        "algorithmVersion": "ohlcv-consensus-pattern-families-v6",
        "symbol": "NVDA",
        "interval": "1D",
        "commentary": {"status": "ready"},
        "geometry": {
            "drawings": drawings,
            "drawingGroups": {"levels": ["level-1"], "trend": ["trend-1"], "pattern": ["pattern-upper"]},
            "patterns": [{
                "id": pattern_id,
                "kind": "falling_wedge",
                "state": "confirmed",
                "confirmation": {"breakoutAt": future, "confirmedAt": future},
            }],
            "primaryPattern": {"id": pattern_id, "kind": "falling_wedge", "state": "confirmed"},
            "tradePlan": {
                "patternId": pattern_id,
                "patternState": "confirmed",
                "action": "no_trade",
                "direction": None,
                "entryTrigger": 190,
                "entryPrice": 191,
                "stopPrice": 180,
                "targetPrice": 200,
                "rewardRiskRatio": 0.8,
                "minimumRewardRisk": 2,
                "reasons": ["confirmed_upward_breakout", "reward_risk_below_minimum"],
            },
            "analysisTrace": {
                "version": "geometry-analysis-trace-v2",
                "completeness": {
                    "complete": True,
                    "detected": {"levels": 1, "trends": 1, "patterns": 1},
                    "stored": {"levels": 1, "trends": 1, "patterns": 1},
                },
            },
        },
    }


def _commentary(asset):
    return {
        "version": "chart-commentary.v2",
        "status": "ready",
        "promptVersion": "chart-commentary.ko.v5",
        "sourceIdentity": {
            "geometryInputDigest": asset["inputDigest"],
            "candlesAsOf": asset["asOf"],
            "indicatorsAsOf": asset["asOf"],
            "contextDigest": "sha256:commentary-context",
        },
        "references": [{"id": "drawing:pattern", "type": "drawing", "drawingIds": ["pattern-upper"]}],
    }


if __name__ == "__main__":
    unittest.main()
