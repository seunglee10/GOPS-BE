from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "systems/agent-orchestration/shared", ROOT / "systems/market-data/shared"):
    if str(path) not in sys.path: sys.path.insert(0, str(path))

from gops_agents.chart_assets.commentary_v2 import assemble_commentary_v2  # noqa: E402
from gops_agents.chart_assets.curation import (  # noqa: E402
    build_interval_palette, build_symbol_bundle, materialize_curation,
    validate_curation_output,
)
from gops_agents.chart_assets.llm import ChartAssetLLMService  # noqa: E402


NOW = "2026-07-11T00:00:00.000Z"


class Response:
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def read(self): return json.dumps(self.payload).encode()


class ChartAssetCurationV2Test(unittest.TestCase):
    def setUp(self):
        self.palette = build_interval_palette(
            symbol="NVDA", interval="1D", input_digest="sha256:" + "a"*64,
            features=features(), rule_layers=rule_layers(), candles=candles(), generated_at=NOW,
        )
        self.bundle = build_symbol_bundle("NVDA", [self.palette])

    def test_compact_bundle_omits_geometry_and_raw_candles(self):
        encoded = json.dumps(self.bundle)
        self.assertNotIn("drawingTemplate", encoded)
        self.assertNotIn("recentBars", encoded)
        self.assertLessEqual(len(self.bundle["intervals"][0]["visualCandidates"]), 6)

    def test_validator_rejects_invented_candidate_fact_and_condition(self):
        output = valid_output(self.bundle)
        output["intervalSelections"][0]["selectedCandidateIds"] = ["1D:vc-invented"]
        with self.assertRaisesRegex(ValueError, "candidate"):
            validate_curation_output(output, self.bundle)
        output = valid_output(self.bundle)
        output["intervalSelections"][0]["focusNarratives"][0]["factIds"] = ["1D:fact:invented"]
        with self.assertRaisesRegex(ValueError, "focus"):
            validate_curation_output(output, self.bundle)
        output = valid_output(self.bundle)
        output["intervalSelections"][0]["counterEvidenceRefs"] = ["1D:pivot:invented"]
        with self.assertRaisesRegex(ValueError, "counter evidence"):
            validate_curation_output(output, self.bundle)
        output = valid_output(self.bundle)
        output["intervalSelections"][0]["focusNarratives"][0]["refType"] = "ruleFinding"
        with self.assertRaisesRegex(ValueError, "focus"):
            validate_curation_output(output, self.bundle)

    def test_materialized_geometry_is_kernel_template_and_focus_covers_it(self):
        output = valid_output(self.bundle)
        validated = validate_curation_output(output, self.bundle)
        layers = materialize_curation(symbol="NVDA", palettes={"1D": self.palette}, output=validated, generated_at=NOW, model="mock")
        candidate = self.palette["visualCandidates"][0]
        drawing = layers["1D"]["drawings"][0]
        self.assertEqual(drawing["anchors"], candidate["drawingTemplate"]["anchors"])
        commentary = assemble_commentary_v2(interval="1D", palette=self.palette, rule_layers=rule_layers(), agent_layer=layers["1D"], curation_selection=output["intervalSelections"][0])
        accepted = {item["id"] for layer in (*rule_layers().values(), layers["1D"]) for item in layer["drawings"]}
        focused = {item for focus in commentary["focusItems"] for item in focus["drawingIds"]}
        self.assertEqual(accepted, focused)
        self.assertTrue(all(item["confirmation"] and item["invalidation"] for item in commentary["focusItems"]))
        self.assertIn("확정 종가", commentary["invalidation"])
        self.assertIsNone(commentary["enrichment"])
        self.assertIsNone(commentary["confidenceV2"]["marketDirection"]["score"])

    def test_responses_request_is_one_strict_bounded_non_stored_call(self):
        output = valid_output(self.bundle)
        opener = Mock(return_value=Response({"status":"completed","model":"mock-resolved","usage":{"input_tokens":100,"output_tokens":50},"output_text":json.dumps(output)}))
        result = ChartAssetLLMService(api_key="secret", model="mock-requested", opener=opener).curate_symbol(self.bundle)
        self.assertFalse(result["degraded"])
        self.assertEqual(opener.call_count, 1)
        payload = json.loads(opener.call_args.args[0].data)
        self.assertFalse(payload["store"])
        self.assertEqual(payload["max_output_tokens"], 1200)
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertEqual(result["model"], "mock-resolved")

    def test_missing_key_is_valid_empty_degraded_without_call(self):
        opener = Mock()
        result = ChartAssetLLMService(api_key="", opener=opener).curate_symbol(self.bundle)
        self.assertTrue(result["degraded"])
        self.assertEqual(result["reason"], "missing_openai_api_key")
        self.assertEqual(opener.call_count, 0)

    def test_empty_asset_explains_rejection_without_repeating_headline(self):
        empty_rules = {
            "structure": {"drawings": [], "selected": [], "meta": {"rejectedByReason": {"current_distance": 4, "stale": 2}}},
            "trend": {"drawings": [], "selected": [], "meta": {"rejectedByReason": {}}},
        }
        commentary = assemble_commentary_v2(
            interval="1D", palette=self.palette, rule_layers=empty_rules,
            agent_layer={"drawings": [], "selected": []}, curation_selection=None,
        )
        self.assertEqual(commentary["confidence"], 0)
        self.assertIn("현재 가격과의 거리", commentary["text"])
        self.assertIn("임의의 선을 만들지 않았습니다", commentary["text"])
        self.assertNotIn(commentary["headline"], commentary["text"])


def valid_output(bundle):
    palette = bundle["intervals"][0]
    candidate = palette["visualCandidates"][0]
    return {"intervalSelections":[{
        "interval":"1D","selectedCandidateIds":[candidate["candidateId"]],
        "headlineFactIds":[candidate["factIds"][0]],
        "focusNarratives":[{"refType":"visualCandidate","refId":candidate["candidateId"],"factIds":[candidate["factIds"][0]],"watchConditionRef":candidate["confirmationConditionRef"],"priority":1}],
        "counterEvidenceRefs":[],"higherTimeframeRelationIds":[],"emphasisCode":"STRUCTURE_FIRST",
    }]}


def features():
    return {"regime":{"trend":"up","atr14":4,"atrPercentile":.5},"pivots":[],"levels":[],"fibCandidates":[],"events":[{"id":"1D:event:e1","timestamp":"2026-07-10T04:00:00.000Z","candleKey":"2026-07-10","price":160,"kind":"gap","refIds":[],"detail":{"state":"unfilled"},"hardPass":True,"currentImpact":"high"}]}


def candles():
    return [{"timestamp":"2026-07-09T04:00:00.000Z","candleKey":"2026-07-09","close":158},{"timestamp":"2026-07-10T04:00:00.000Z","candleKey":"2026-07-10","close":160}]


def rule_layers():
    drawing={"id":"ca-NVDA-1D-trend-t1","type":"trendLine","anchors":[{"timestamp":"2026-07-09T04:00:00.000Z","price":155},{"timestamp":"2026-07-10T04:00:00.000Z","price":158}],"label":"검증된 상승 추세"}
    return {"structure":{"drawings":[],"selected":[],"emptyReason":"no_candidate","meta":{}},"trend":{"drawings":[drawing],"selected":[{"candidateId":"1D:trend:t1","drawingIds":[drawing["id"]],"evidenceRefs":["1D:pivot:p1"],"quality":{"score":.8}}],"emptyReason":None,"meta":{}}}


if __name__ == "__main__": unittest.main()
