from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "systems" / "agent-orchestration" / "shared", ROOT / "systems" / "market-data" / "shared"):
    if str(path) not in sys.path: sys.path.insert(0, str(path))

from gops_agents.chart_assets.compilers import compile_rule_layers, fallback_commentary, recommended_indicators  # noqa: E402


GENERATED_AT = "2026-07-11T02:00:00.000Z"


def feature_pack() -> dict:
    return {
        "pivots": [
            {"id": "p1", "timestamp": "2026-01-01T00:00:00.000Z", "price": 100, "kind": "L"},
            {"id": "p2", "timestamp": "2026-02-01T00:00:00.000Z", "price": 110, "kind": "L"},
            {"id": "p3", "timestamp": "2026-01-15T00:00:00.000Z", "price": 125, "kind": "H"},
        ],
        "levels": [
            {"id": "l1", "price": 105, "score": 0.91, "vpConfluence": True},
            {"id": "l2", "price": 118, "score": 0.72, "vpConfluence": False},
            {"id": "l3", "price": 122, "score": 0.60, "vpConfluence": False},
            {"id": "l4", "price": 98, "score": 0.51, "vpConfluence": False},
            {"id": "l5", "price": 95, "score": 0.41, "vpConfluence": False},
            {"id": "l6", "price": 130, "score": 0.36, "vpConfluence": False},
        ],
        "trends": [{"id": "t1", "kind": "up", "anchorPivotIds": ["p1", "p2"], "touches": 3, "slopePerBar": 0.3, "channelWidth": None}],
        "vp": {"poc": 105},
        "regime": {"trend": "up", "atr14": 4, "bbSqueeze": True, "bbBandwidthPercentile": 0.1, "macdState": "bullish_cross_recent", "rsi14": 61},
        "events": [
            {"id": "e1", "timestamp": "2026-03-01T00:00:00.000Z", "kind": "breakout", "price": 119, "refIds": ["l2"], "detail": {"volumeZ": 2.8}},
            {"id": "e2", "timestamp": "2026-03-02T00:00:00.000Z", "kind": "retest", "price": 118, "refIds": ["l2"], "detail": {}},
            {"id": "e3", "timestamp": "2026-03-03T00:00:00.000Z", "kind": "volumeSpike", "price": 121, "refIds": [], "detail": {"volumeZ": 3.1}},
        ],
        "fibCandidates": [],
    }


def candles() -> list[dict]:
    return [
        {"timestamp": "2026-01-01T00:00:00.000Z", "open": 100, "high": 115, "low": 90, "close": 100, "volume": 1000},
        {"timestamp": "2026-03-10T00:00:00.000Z", "open": 118, "high": 125, "low": 115, "close": 120, "volume": 1200},
    ]


class ChartAssetCompilerTest(unittest.TestCase):
    def test_rule_layers_obey_visual_budget_and_anchor_contract(self):
        layers = compile_rule_layers(symbol="NVDA", interval="1D", features=feature_pack(), candles=candles(), generated_at=GENERATED_AT)
        structure = layers["structure"]
        trend = layers["trend"]
        self.assertLessEqual(len(structure["meta"]["levelIds"]), 5)
        self.assertLessEqual(len(structure["meta"]["flagEventIds"]), 3)
        self.assertEqual(len(trend["drawings"]), 1)
        for drawing in structure["drawings"] + trend["drawings"]:
            self._assert_shared_schema_shape(drawing)
            self.assertTrue(drawing["label"])
            if drawing["type"] == "horizontalLine":
                self.assertEqual(set(drawing["anchors"][0]), {"price"})
            else:
                self.assertTrue(all("timestamp" in anchor and "price" in anchor for anchor in drawing["anchors"]))

    def test_macro_level_has_separate_budget_and_precedence(self):
        higher = {"1M": {"features": {"levels": [{"id": "l1", "price": 118.5, "score": 0.9}]}}}
        layer = compile_rule_layers(symbol="NVDA", interval="1W", features=feature_pack(), candles=candles(), generated_at=GENERATED_AT, higher_assets=higher)["structure"]
        self.assertEqual(len(layer["meta"]["macroLevels"]), 1)
        self.assertNotIn("l2", layer["meta"]["levelIds"])
        self.assertEqual(layer["drawings"][0]["style"]["colorToken"], "asset-sr-macro")

    def test_range_is_never_empty(self):
        features = feature_pack()
        features["trends"] = [{"id": "t1", "kind": "range", "anchorPivotIds": [], "touches": 0, "slopePerBar": 0, "channelWidth": 10, "rangeFrom": "2026-01-01T00:00:00.000Z", "rangeTo": "2026-03-10T00:00:00.000Z", "rangeHigh": 125, "rangeLow": 115}]
        layer = compile_rule_layers(symbol="NVDA", interval="1D", features=features, candles=candles(), generated_at=GENERATED_AT)["trend"]
        self.assertEqual(layer["meta"]["kind"], "range")
        self.assertEqual(layer["drawings"][0]["type"], "rangeBox")

    def test_indicator_rules_and_fallback_commentary(self):
        recommendations = recommended_indicators(feature_pack())
        self.assertEqual([item["layer"] for item in recommendations], ["bollinger:20:2", "macd:12:26:9"])
        commentary = fallback_commentary("NVDA", "1D", feature_pack(), 120)
        self.assertEqual(commentary["confidence"], 0.3)
        self.assertIsNone(commentary["enrichment"])
        self.assertIn("무효", commentary["invalidation"])

    def _assert_shared_schema_shape(self, drawing: dict) -> None:
        schema = json.loads((ROOT / "shared" / "chart-contract" / "chart-command.schema.json").read_text(encoding="utf-8"))["$defs"]["drawingEntity"]
        self.assertTrue(set(schema["required"]).issubset(drawing))
        self.assertFalse(set(drawing).difference(schema["properties"]))
        self.assertIn(drawing["type"], schema["properties"]["type"]["enum"])
        self.assertIn(drawing["createdBy"], schema["properties"]["createdBy"]["enum"])


if __name__ == "__main__": unittest.main()
