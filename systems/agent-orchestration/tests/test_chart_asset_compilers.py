from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "systems" / "agent-orchestration" / "shared", ROOT / "systems" / "market-data" / "shared"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from gops_agents.chart_assets.compilers import compile_rule_layers, recommended_indicators  # noqa: E402


GENERATED_AT = "2026-07-11T02:00:00.000Z"


def feature_pack() -> dict:
    return {
        "pivots": [
            {"id": "p1", "timestamp": "2026-01-01T00:00:00.000Z", "price": 100, "kind": "L"},
            {"id": "p2", "timestamp": "2026-03-10T00:00:00.000Z", "price": 110, "kind": "L"},
        ],
        "levels": [
            {"id": "l1", "price": 105, "score": 0.91, "hardPass": True, "role": "support", "vpConfluence": True, "touches": 4, "lastTouchAgeBars": 2, "currentDistanceAtr": 1, "memberPivotIds": ["p1"]},
            {"id": "l2", "price": 122, "score": 0.72, "hardPass": True, "role": "resistance", "vpConfluence": False, "touches": 3, "lastTouchAgeBars": 4, "currentDistanceAtr": 0.5, "memberPivotIds": ["p2"]},
            {"id": "rejected", "price": 130, "score": 0.9, "hardPass": False, "role": "resistance", "rejectReasons": ["current_distance"]},
        ],
        "trends": [{
            "id": "t1", "kind": "up", "anchorPivotIds": ["p1", "p2"], "touchPivotIds": ["p1", "p2", "p3"],
            "touches": 3, "slopePerBar": 0.3, "hardPass": True, "score": 0.8,
            "currentDistanceAtr": 0.7, "lastTouchAgeBars": 2, "spanBars": 48,
            "medianResidualAtr": 0.1, "violationCount": 0,
        }],
        "regime": {"trend": "up", "atr14": 4, "bbSqueeze": True, "bbBandwidthPercentile": 0.1, "macdState": "bullish_cross_recent", "rsi14": 61},
        "events": [{
            "id": "e-down", "timestamp": "2026-03-10T00:00:00.000Z", "kind": "breakout",
            "price": 101, "refIds": ["l1"], "detail": {"direction": "down"},
            "hardPass": True, "currentImpact": "high", "ageBars": 0,
        }],
    }


def candles() -> list[dict]:
    return [
        {"timestamp": "2026-01-01T00:00:00.000Z", "open": 100, "high": 115, "low": 90, "close": 100, "volume": 1000},
        {"timestamp": "2026-03-10T00:00:00.000Z", "open": 118, "high": 125, "low": 115, "close": 120, "volume": 1200},
    ]


class ChartAssetCompilerTest(unittest.TestCase):
    def test_only_hard_pass_candidates_compile_with_exact_anchors(self):
        layers = compile_rule_layers(
            symbol="NVDA", interval="1D", features=feature_pack(),
            candles=candles(), generated_at=GENERATED_AT,
        )
        self.assertLessEqual(len(layers["structure"]["drawings"]), 3)
        self.assertLessEqual(len(layers["trend"]["drawings"]), 1)
        self.assertEqual(layers["structure"]["meta"]["candidateCount"], 4)
        self.assertEqual(layers["structure"]["meta"]["rejectedByReason"], {"current_distance": 1})
        candle_times = {item["timestamp"] for item in candles()}
        for drawing in layers["structure"]["drawings"] + layers["trend"]["drawings"]:
            self._assert_shared_schema_shape(drawing)
            if drawing["type"] == "horizontalLine":
                self.assertEqual(set(drawing["anchors"][0]), {"price"})
                self.assertNotIn(str(drawing["anchors"][0]["price"]), drawing["label"])
            else:
                self.assertTrue(all(anchor["timestamp"] in candle_times for anchor in drawing["anchors"]))

    def test_event_label_preserves_breakout_direction_without_price_text(self):
        structure = compile_rule_layers(
            symbol="NVDA", interval="1D", features=feature_pack(),
            candles=candles(), generated_at=GENERATED_AT,
        )["structure"]
        flag = next(item for item in structure["drawings"] if item["type"] == "flagMarker")
        self.assertEqual(flag["label"], "지지 이탈")

    def test_indicator_rules_are_bounded(self):
        recommendations = recommended_indicators(feature_pack())
        self.assertEqual([item["layer"] for item in recommendations], ["bollinger:20:2", "macd:12:26:9"])
        self.assertLessEqual(len(recommendations), 2)

    def _assert_shared_schema_shape(self, drawing: dict) -> None:
        schema = json.loads((ROOT / "shared" / "chart-contract" / "chart-command.schema.json").read_text(encoding="utf-8"))["$defs"]["drawingEntity"]
        self.assertTrue(set(schema["required"]).issubset(drawing))
        self.assertFalse(set(drawing).difference(schema["properties"]))
        self.assertIn(drawing["type"], schema["properties"]["type"]["enum"])
        self.assertIn(drawing["createdBy"], schema["properties"]["createdBy"]["enum"])


if __name__ == "__main__":
    unittest.main()
