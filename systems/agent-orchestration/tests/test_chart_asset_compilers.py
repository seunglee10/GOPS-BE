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
    def test_triangle_compiles_as_grouped_lines_alongside_existing_trend_with_global_budget(self):
        features = feature_pack()
        features["patterns"] = [{
            "id": "1D:pattern:ascending",
            "kind": "ascending_triangle",
            "state": "forming",
            "breakoutDirection": None,
            "hardPass": True,
            "score": .94,
            "touches": 6,
            "containment": .92,
            "evidenceRefs": ["p1", "p2"],
            "geometry": {
                "upper": {
                    "start": {"timestamp": candles()[0]["timestamp"], "price": 122},
                    "end": {"timestamp": candles()[1]["timestamp"], "price": 122},
                },
                "lower": {
                    "start": {"timestamp": candles()[0]["timestamp"], "price": 96},
                    "end": {"timestamp": candles()[1]["timestamp"], "price": 116},
                },
            },
        }]

        layers = compile_rule_layers(
            symbol="NVDA", interval="1D", features=features,
            candles=candles(), generated_at=GENERATED_AT,
        )

        pattern = next(item for item in layers["trend"]["selected"] if item["candidateId"] == "1D:pattern:ascending")
        pattern_drawings = [item for item in layers["trend"]["drawings"] if item["id"] in pattern["drawingIds"]]
        self.assertEqual([item["type"] for item in pattern_drawings], ["trendLine", "trendLine"])
        self.assertTrue(all(item["style"]["lineDash"] == [6, 4] for item in pattern_drawings))
        self.assertIn("형성 중", pattern_drawings[0]["label"])
        self.assertEqual(len(layers["trend"]["drawings"]), 3)
        self.assertLessEqual(len(layers["structure"]["drawings"]) + len(layers["trend"]["drawings"]), 5)

    def test_confirmed_flag_compiles_to_pole_and_parallel_channel(self):
        features = feature_pack()
        features["patterns"] = [{
            "id": "1D:pattern:bear-flag",
            "kind": "bearish_flag",
            "state": "confirmed",
            "breakoutDirection": "down",
            "hardPass": True,
            "score": .91,
            "touches": 5,
            "containment": .88,
            "evidenceRefs": ["p1", "p2"],
            "geometry": {
                "pole": {
                    "start": {"timestamp": candles()[0]["timestamp"], "price": 122},
                    "end": {"timestamp": candles()[1]["timestamp"], "price": 116},
                },
                "upper": {
                    "start": {"timestamp": candles()[0]["timestamp"], "price": 116},
                    "end": {"timestamp": candles()[1]["timestamp"], "price": 120},
                },
                "lower": {
                    "start": {"timestamp": candles()[0]["timestamp"], "price": 112},
                    "end": {"timestamp": candles()[1]["timestamp"], "price": 116},
                },
            },
        }]

        trend = compile_rule_layers(
            symbol="NVDA", interval="1D", features=features,
            candles=candles(), generated_at=GENERATED_AT,
        )["trend"]

        selected = next(item for item in trend["selected"] if item["candidateId"] == "1D:pattern:bear-flag")
        drawings = [item for item in trend["drawings"] if item["id"] in selected["drawingIds"]]
        self.assertEqual([item["type"] for item in drawings], ["trendLine", "trendParallelLines"])
        self.assertNotIn("lineDash", drawings[0]["style"])
        self.assertEqual(drawings[1]["parallelLineCount"], 2)
        self.assertIn("확인", drawings[0]["label"])

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

    def test_ma60_ma120_cross_compiles_as_colored_event_marker(self):
        for direction, label, color in (
            ("golden", "MA60/120 골든크로스", "#22c55e"),
            ("dead", "MA60/120 데드크로스", "#ef4444"),
        ):
            with self.subTest(direction=direction):
                features = feature_pack()
                features.update({
                    "levels": [],
                    "trends": [],
                    "events": [{
                        "id": f"1D:event:{direction}",
                        "timestamp": candles()[-1]["timestamp"],
                        "candleKey": "2026-03-10",
                        "kind": "movingAverageCross",
                        "price": 119.25,
                        "refIds": [],
                        "detail": {
                            "direction": direction,
                            "shortPeriod": 60,
                            "longPeriod": 120,
                            "state": "crossed",
                        },
                        "hardPass": True,
                        "evidencePass": True,
                        "activePass": True,
                        "currentImpact": "high",
                        "ageBars": 0,
                    }],
                })

                structure = compile_rule_layers(
                    symbol="NVDA", interval="1D", features=features,
                    candles=candles(), generated_at=GENERATED_AT,
                )["structure"]

                marker = structure["drawings"][0]
                self.assertEqual(marker["type"], "flagMarker")
                self.assertEqual(marker["label"], label)
                self.assertEqual(marker["style"]["color"], color)
                self.assertEqual(marker["anchors"], [{
                    "timestamp": candles()[-1]["timestamp"],
                    "price": 119.25,
                }])

    def test_legacy_gap_event_is_not_compiled(self):
        features = feature_pack()
        features.update({
            "levels": [],
            "trends": [],
            "events": [{
                "id": "legacy-gap", "timestamp": candles()[-1]["timestamp"], "kind": "gap",
                "price": 120, "refIds": [], "detail": {"direction": "up", "state": "unfilled"},
                "hardPass": True, "currentImpact": "high", "ageBars": 0,
            }],
        })

        structure = compile_rule_layers(
            symbol="NVDA", interval="1D", features=features,
            candles=candles(), generated_at=GENERATED_AT,
        )["structure"]

        self.assertEqual(structure["drawings"], [])
        self.assertEqual(structure["meta"]["passedCount"], 0)

    def test_indicator_rules_are_bounded(self):
        recommendations = recommended_indicators(feature_pack())
        self.assertEqual([item["layer"] for item in recommendations], ["bollinger:20:2", "macd:12:26:9"])
        self.assertLessEqual(len(recommendations), 2)

    def test_exactly_current_level_is_not_ranked_as_missing_distance(self):
        features = feature_pack()
        features["levels"] = [
            {"id": "near", "price": 120, "score": .88, "hardPass": True, "role": "support", "currentDistanceAtr": 0, "lastTouchAgeBars": 0},
            {"id": "far", "price": 110, "score": .62, "hardPass": True, "role": "support", "currentDistanceAtr": 1.2, "lastTouchAgeBars": 1},
        ]

        structure = compile_rule_layers(
            symbol="NVDA", interval="1D", features=features,
            candles=candles(), generated_at=GENERATED_AT,
        )["structure"]

        self.assertEqual(structure["selected"][0]["candidateId"], "near")

    def test_break_pending_level_is_not_hline_but_confirmed_break_is_flag(self):
        features = feature_pack()
        features["levels"] = [{
            "id": "pending", "price": 115, "score": .9, "hardPass": False,
            "evidencePass": True, "activePass": False, "role": "unresolved",
            "state": "break_up_pending", "rejectReasons": ["break_pending"],
        }]
        features["events"] = [{
            "id": "confirmed-break", "timestamp": candles()[-1]["timestamp"], "kind": "breakout",
            "price": 120, "refIds": ["pending"], "detail": {"direction": "up", "state": "hold_confirmed"},
            "hardPass": True, "evidencePass": True, "activePass": True, "currentImpact": "high", "ageBars": 0,
        }]

        structure = compile_rule_layers(
            symbol="NVDA", interval="1D", features=features,
            candles=candles(), generated_at=GENERATED_AT,
        )["structure"]

        self.assertEqual([item["type"] for item in structure["drawings"]], ["flagMarker"])
        self.assertEqual(structure["drawings"][0]["anchors"][0]["timestamp"], candles()[-1]["timestamp"])
        self.assertEqual(structure["meta"]["rejectedByReason"], {"break_pending": 1})

    def test_empty_reason_distinguishes_evidence_from_no_structure(self):
        no_structure = feature_pack()
        no_structure.update({"levels": [], "events": [], "trends": []})
        empty = compile_rule_layers(symbol="NVDA", interval="1D", features=no_structure, candles=candles(), generated_at=GENERATED_AT)
        self.assertEqual(empty["structure"]["emptyReason"], "no_structural_evidence")

        inactive = feature_pack()
        inactive.update({"events": [], "trends": [], "levels": [{
            "id": "inactive", "hardPass": False, "evidencePass": True,
            "rejectReasons": ["current_distance"],
        }]})
        empty = compile_rule_layers(symbol="NVDA", interval="1D", features=inactive, candles=candles(), generated_at=GENERATED_AT)
        self.assertEqual(empty["structure"]["emptyReason"], "not_currently_actionable")

        invalidated = feature_pack()
        invalidated.update({"levels": [], "events": [], "trends": [{
            "id": "invalidated", "hardPass": False, "evidencePass": True,
            "rejectReasons": ["active_invalidation"],
        }]})
        empty = compile_rule_layers(symbol="NVDA", interval="1D", features=invalidated, candles=candles(), generated_at=GENERATED_AT)
        self.assertEqual(empty["trend"]["emptyReason"], "active_invalidation")

    def _assert_shared_schema_shape(self, drawing: dict) -> None:
        schema = json.loads((ROOT / "shared" / "chart-contract" / "chart-command.schema.json").read_text(encoding="utf-8"))["$defs"]["drawingEntity"]
        self.assertTrue(set(schema["required"]).issubset(drawing))
        self.assertFalse(set(drawing).difference(schema["properties"]))
        self.assertIn(drawing["type"], schema["properties"]["type"]["enum"])
        self.assertIn(drawing["createdBy"], schema["properties"]["createdBy"]["enum"])


if __name__ == "__main__":
    unittest.main()
