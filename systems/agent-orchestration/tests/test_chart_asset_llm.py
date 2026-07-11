from __future__ import annotations

import copy
import json
import math
import sys
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "systems" / "agent-orchestration" / "shared", ROOT / "systems" / "market-data" / "shared"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from gops_agents.chart_assets.builder import ChartAssetBuilder  # noqa: E402
from gops_agents.chart_assets.envelope import ChartAssetBuildEnvelope  # noqa: E402
from gops_agents.chart_assets.intent_compiler import compile_agent_layer, merge_indicator_suggestions  # noqa: E402
from gops_agents.chart_assets.llm import ChartAssetLLMService  # noqa: E402
from gops_agents.chart_assets.progress import InMemoryChartAssetProgressStore  # noqa: E402
from gops_agents.chart_assets.prompts import PROMPT_VERSION, build_llm_input  # noqa: E402
from gops_agents.chart_assets.curation import deterministic_curation  # noqa: E402


NOW = "2026-07-11T00:00:00.000Z"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        return False
    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakeCandleLoader:
    def load(self, _symbol, _interval):
        return candles()


class FakeStorage:
    def __init__(self):
        self.assets = {}
        self.saved = []
    def get(self, symbol, interval):
        return copy.deepcopy(self.assets.get((symbol, interval)))
    def save(self, asset):
        self.assets[(asset["symbol"], asset["interval"])] = copy.deepcopy(asset)
        self.saved.append(copy.deepcopy(asset))
    def is_fresh(self, _symbol, _interval, _hours):
        return False


class RecordingLLMService:
    def __init__(self):
        self.calls = []
        self.model = "mock"
    def curate_symbol(self, bundle):
        self.calls.append((bundle["symbol"], tuple(item["interval"] for item in bundle["intervals"])))
        return {"output": deterministic_curation(bundle), "degraded": False, "reason": None, "model": "mock", "usage": {}}


class RaisingLLMService:
    model = "mock"

    def curate_symbol(self, _bundle):
        raise RuntimeError("injected failure")


class ChartAssetLLMTest(unittest.TestCase):
    def test_valid_response_compiles_drawing_and_strict_request(self):
        opener = Mock(return_value=FakeResponse({"output_text": json.dumps(valid_output(), ensure_ascii=False)}))
        result = ChartAssetLLMService(api_key="test", model="mock-model", opener=opener).build(
            symbol="NVDA", interval="1D", candles=candles(), features=features(),
            rule_layers=rule_layers(), higher_assets={}, generated_at=NOW,
        )
        self.assertEqual(opener.call_count, 1)
        self.assertFalse(result["agentLayer"]["degraded"])
        drawing = result["agentLayer"]["drawings"][0]
        self.assertEqual({
            "id": drawing["id"], "type": drawing["type"], "anchors": drawing["anchors"],
            "createdBy": drawing["createdBy"], "sourceProposalId": drawing["sourceProposalId"],
        }, {
            "id": "ca-NVDA-1D-agent-1", "type": "rangeBox",
            "anchors": [
                {"timestamp": "2026-05-01T00:00:00.000Z", "price": 100.0},
                {"timestamp": "2026-06-01T00:00:00.000Z", "price": 110.0},
            ],
            "createdBy": "llm", "sourceProposalId": "chart-asset:NVDA:1D:agent",
        })
        request_payload = json.loads(opener.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(request_payload["text"]["format"]["type"], "json_schema")
        self.assertTrue(request_payload["text"]["format"]["strict"])
        self.assertEqual(result["indicatorSuggestions"], [{"layer": "rsi:14", "reason": "RSI 위치 확인"}])

    def test_compiler_drops_unknown_anchor_fib_without_candidate_and_duplicate_line(self):
        source = features()
        source["fibCandidates"] = []
        intents = [
            intent("textLabel", ["missing"]),
            intent("fibonacciRetracement", ["p1", "p2"]),
            intent("horizontalLine", ["l1"]),
        ]
        layer = compile_agent_layer(
            symbol="NVDA", interval="1D", intents=intents, features=source,
            rule_layers=rule_layers(), generated_at=NOW, model="mock",
        )
        self.assertEqual([item["reason"] for item in layer["droppedIntents"]], [
            "unknown_anchor", "fib_not_candidate", "rule_duplicate",
        ])
        self.assertEqual(layer["drawings"], [])

    def test_compiler_rejects_wrong_anchor_count_and_untimed_level_anchor(self):
        source = features()
        source["levels"].append({"id": "l2", "price": 115.0, "score": 0.7, "vpConfluence": False})
        layer = compile_agent_layer(
            symbol="NVDA", interval="1D", intents=[
                intent("rangeBox", ["p1"]),
                intent("rangeBox", ["l1", "p2"]),
                intent("horizontalParallelLines", ["l1", "l2"]),
            ], features=source, rule_layers={"structure": {"drawings": []}},
            generated_at=NOW, model="mock",
        )
        self.assertEqual([item["reason"] for item in layer["droppedIntents"]], [
            "invalid_anchor_count", "anchor_shape",
        ])
        self.assertEqual(layer["drawings"][0]["type"], "horizontalParallelLines")

    def test_compiler_enforces_fill_and_foreground_budgets(self):
        fill_intents = [intent("rangeBox", ["p1", "p2"], label=f"구간 {index}") for index in range(3)]
        foreground_intents = [intent("textLabel", ["p1"], label=f"메모 {index}") for index in range(6)]
        layer = compile_agent_layer(
            symbol="NVDA", interval="1D", intents=[*fill_intents, *foreground_intents],
            features=features(), rule_layers={"structure": {"drawings": []}}, generated_at=NOW, model="mock",
        )
        self.assertEqual(len(layer["drawings"]), 7)
        self.assertEqual([item["reason"] for item in layer["droppedIntents"]], ["fill_budget", "foreground_budget"])

    def test_indicator_suggestions_merge_deduplicates_and_caps_two(self):
        self.assertEqual(merge_indicator_suggestions(
            [{"layer": "rsi:14", "reason": "rule rsi", "source": "rule"}],
            [
                {"layer": "rsi:14", "reason": "duplicate"},
                {"layer": "ema:20", "reason": "llm ema"},
                {"layer": "macd:12:26:9", "reason": "overflow"},
            ],
        ), [
            {"layer": "rsi:14", "reason": "rule rsi", "source": "rule"},
            {"layer": "ema:20", "reason": "llm ema", "source": "llm"},
        ])

    def test_invalid_tool_is_retried_once_then_degraded(self):
        invalid = valid_output()
        invalid["intents"][0]["tool"] = "riskRewardBox"
        opener = Mock(return_value=FakeResponse({"output_text": json.dumps(invalid, ensure_ascii=False)}))
        result = ChartAssetLLMService(api_key="test", opener=opener).build(
            symbol="NVDA", interval="1D", candles=candles(), features=features(),
            rule_layers=rule_layers(), higher_assets={}, generated_at=NOW,
        )
        self.assertEqual(opener.call_count, 2)
        self.assertTrue(result["agentLayer"]["degraded"])
        self.assertEqual(result["agentLayer"]["meta"]["failureReason"], "openai_ValueError")

    def test_rate_limit_retries_once_after_backoff(self):
        sleeps = []
        opener = Mock(side_effect=[
            urllib.error.HTTPError("https://api.openai.com/v1/responses", 429, "rate limited", None, None),
            FakeResponse({"output_text": json.dumps(valid_output(), ensure_ascii=False)}),
        ])
        result = ChartAssetLLMService(
            api_key="test", opener=opener, sleeper=sleeps.append,
        ).build(
            symbol="NVDA", interval="1D", candles=candles(), features=features(),
            rule_layers=rule_layers(), higher_assets={}, generated_at=NOW,
        )
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(sleeps, [0.5])
        self.assertFalse(result["agentLayer"]["degraded"])

    def test_compiler_failure_is_not_retried_or_mislabeled_as_openai_failure(self):
        opener = Mock(return_value=FakeResponse({"output_text": json.dumps(valid_output(), ensure_ascii=False)}))
        service = ChartAssetLLMService(api_key="test", opener=opener)

        with patch("gops_agents.chart_assets.llm.compile_agent_layer", side_effect=RuntimeError("compiler bug")):
            with self.assertRaisesRegex(RuntimeError, "compiler bug"):
                service.build(
                    symbol="NVDA", interval="1D", candles=candles(), features=features(),
                    rule_layers=rule_layers(), higher_assets={}, generated_at=NOW,
                )

        self.assertEqual(opener.call_count, 1)

    def test_missing_api_key_degrades_without_network_call(self):
        opener = Mock(side_effect=AssertionError("network should not be called"))
        result = ChartAssetLLMService(api_key="", opener=opener).build(
            symbol="NVDA", interval="1D", candles=candles(), features=features(),
            rule_layers=rule_layers(), higher_assets={}, generated_at=NOW,
        )
        opener.assert_not_called()
        self.assertTrue(result["agentLayer"]["degraded"])
        self.assertEqual(result["agentLayer"]["meta"]["failureReason"], "missing_openai_api_key")

    def test_timeout_retries_then_builder_saves_degraded_fallback(self):
        opener = Mock(side_effect=TimeoutError("timeout"))
        service = ChartAssetLLMService(api_key="test", opener=opener)
        request = ChartAssetBuildEnvelope.create(
            requested_by="test", symbols=["NVDA"], intervals=["1D"], llm_enabled=True,
            job_id="cab-12345678-llm-timeout", submitted_at=NOW,
        )
        progress = InMemoryChartAssetProgressStore(); progress.initialize(request)
        storage = FakeStorage()
        state = ChartAssetBuilder(
            candle_loader=FakeCandleLoader(), storage=storage, progress=progress,
            llm_service=service, concurrency=1,
        ).run(request)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(state["status"], "completed_with_warnings")
        self.assertEqual(state["progress"]["failed"], 0)
        self.assertEqual(state["progress"]["warnings"], 1)
        self.assertEqual(storage.saved[0]["status"], "degraded")
        self.assertIn("주요 관찰", storage.saved[0]["commentary"]["text"])
        self.assertEqual(storage.saved[0]["commentary"]["enrichment"], None)
        self.assertEqual(storage.saved[0]["build"]["agentOutcome"], "degraded")

    def test_builder_catches_llm_exception_and_saves_degraded_asset(self):
        request = ChartAssetBuildEnvelope.create(
            requested_by="test", symbols=["NVDA"], intervals=["1D"], llm_enabled=True,
            job_id="cab-12345678-llm-exception", submitted_at=NOW,
        )
        progress = InMemoryChartAssetProgressStore(); progress.initialize(request)
        storage = FakeStorage()
        state = ChartAssetBuilder(
            candle_loader=FakeCandleLoader(), storage=storage, progress=progress,
            llm_service=RaisingLLMService(), concurrency=1,
        ).run(request)
        self.assertEqual(state["status"], "completed_with_warnings")
        self.assertEqual(state["failedItems"], [])
        self.assertEqual(storage.saved[0]["status"], "degraded")
        self.assertEqual(storage.saved[0]["build"]["agentOutcome"], "degraded")

    def test_grounding_warning_is_recorded_without_rejecting_output(self):
        output = valid_output()
        output["commentary"]["text"] = "입력에 없는 999.99 가격을 언급합니다."
        opener = Mock(return_value=FakeResponse({"output_text": json.dumps(output, ensure_ascii=False)}))
        result = ChartAssetLLMService(api_key="test", opener=opener).build(
            symbol="NVDA", interval="1D", candles=candles(), features=features(),
            rule_layers=rule_layers(), higher_assets={}, generated_at=NOW,
        )
        self.assertEqual(result["agentLayer"]["meta"]["groundingFlags"], ["ungrounded_number"])
        self.assertFalse(result["agentLayer"]["degraded"])

    def test_prompt_multitimeframe_context_is_deterministic_and_scoped(self):
        monthly = higher_asset("1M")
        weekly = higher_asset("1W")
        one_day = build_llm_input(
            symbol="NVDA", interval="1D", candles=candles(), features=features(),
            rule_layers=rule_layers(), higher_assets={"1W": weekly, "1M": monthly},
        )
        self.assertEqual(list(one_day["higherTimeframeContext"]), ["1M", "1W"])
        self.assertEqual(one_day["higherTimeframeContext"]["1M"], {
            "regime": "up", "trend": "상승 추세선",
            "keyLevels": ["저항 110.00 (score 0.90)", "지지 100.00 (score 0.80)"],
            "commentaryGist": "월봉 상승 추세입니다.",
        })
        monthly_input = build_llm_input(
            symbol="NVDA", interval="1M", candles=candles(), features=features(),
            rule_layers=rule_layers(), higher_assets={"1M": monthly, "1W": weekly},
        )
        self.assertIsNone(monthly_input["higherTimeframeContext"])

    def test_builder_calls_llm_once_with_month_week_day_bundle(self):
        request = ChartAssetBuildEnvelope.create(
            requested_by="test", symbols=["NVDA"], intervals=["1D", "1W", "1M"], llm_enabled=True,
            job_id="cab-12345678-llm-order", submitted_at=NOW,
        )
        progress = InMemoryChartAssetProgressStore(); progress.initialize(request)
        storage = FakeStorage(); service = RecordingLLMService()
        state = ChartAssetBuilder(
            candle_loader=FakeCandleLoader(), storage=storage, progress=progress,
            llm_service=service, concurrency=1,
        ).run(request)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(service.calls, [("NVDA", ("1M", "1W", "1D"))])

    def test_partial_one_day_build_marks_missing_higher_context(self):
        request = ChartAssetBuildEnvelope.create(
            requested_by="test", symbols=["NVDA"], intervals=["1D"], llm_enabled=True,
            job_id="cab-12345678-llm-missing-higher", submitted_at=NOW,
        )
        progress = InMemoryChartAssetProgressStore(); progress.initialize(request)
        storage = FakeStorage(); service = RecordingLLMService()
        ChartAssetBuilder(
            candle_loader=FakeCandleLoader(), storage=storage, progress=progress,
            llm_service=service, concurrency=1,
        ).run(request)
        self.assertEqual(service.calls, [("NVDA", ("1D",))])
        self.assertEqual(storage.saved[0]["buildContext"]["flags"], ["no_higher_tf_context"])
        self.assertNotIn("no_higher_tf_context", storage.saved[0]["coverage"]["qualityFlags"])


def candles():
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(72):
        close = 100 + index * 0.15 + math.sin(index / 3)
        rows.append({
            "timestamp": (start + timedelta(days=index)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "open": round(close - 0.5, 2), "high": round(close + 1.0, 2),
            "low": round(close - 1.0, 2), "close": round(close, 2), "volume": 1000 + index,
        })
    return rows


def features():
    return {
        "pivots": [
            {"id": "p1", "timestamp": "2026-05-01T00:00:00.000Z", "price": 100.0, "kind": "L", "strength": 0.8, "inDisplayWindow": True},
            {"id": "p2", "timestamp": "2026-06-01T00:00:00.000Z", "price": 110.0, "kind": "H", "strength": 0.9, "inDisplayWindow": True},
            {"id": "p3", "timestamp": "2026-06-10T00:00:00.000Z", "price": 104.0, "kind": "L", "strength": 0.7, "inDisplayWindow": True},
        ],
        "levels": [{"id": "l1", "price": 105.0, "score": 0.9, "vpConfluence": True}],
        "events": [{"id": "e1", "timestamp": "2026-06-15T00:00:00.000Z", "price": 108.0, "kind": "breakout"}],
        "fibCandidates": [{"fromPivotId": "p1", "toPivotId": "p2", "quality": 0.9}],
        "regime": {"trend": "up", "atr14": 4.0, "rsi14": 55.0},
    }


def rule_layers():
    return {
        "structure": {"drawings": [{"type": "horizontalLine", "anchors": [{"price": 105.0}], "label": "지지 105.00"}]},
        "trend": {"drawings": [{"type": "trendLine", "anchors": [], "label": "상승 추세선"}]},
    }


def intent(tool, anchor_ids, *, label="관찰 구간"):
    return {
        "tool": tool, "anchorIds": anchor_ids, "styleToken": "insight-zone",
        "label": label, "role": "zone", "rationale": "입력 앵커 기반입니다.",
    }


def valid_output():
    return {
        "intents": [intent("rangeBox", ["p1", "p2"])],
        "indicatorSuggestions": [{"layer": "rsi:14", "reason": "RSI 위치 확인"}],
        "commentary": {
            "text": "1D 기준 100.00~110.00 구조를 관찰합니다. 100.00 이탈 시 해석을 다시 확인합니다.",
            "keyLevels": ["지지 100.00", "저항 110.00"],
            "invalidation": "100.00 종가 이탈 시 무효입니다.",
            "confidence": 0.62,
        },
    }


def higher_asset(interval):
    return {
        "interval": interval,
        "features": {
            "regime": {"trend": "up"},
            "levels": [
                {"id": "l2", "price": 100.0, "score": 0.8},
                {"id": "l1", "price": 110.0, "score": 0.9},
                {"id": "l3", "price": 90.0, "score": 0.5},
            ],
        },
        "layers": {
            "structure": {"drawings": [
                {"type": "horizontalLine", "anchors": [{"price": 110.0}], "label": "저항 110.00"},
                {"type": "horizontalLine", "anchors": [{"price": 100.0}], "label": "지지 100.00"},
            ]},
            "trend": {"drawings": [{"label": "상승 추세선"}], "meta": {"kind": "trendline"}},
        },
        "commentary": {"text": "월봉 상승 추세입니다. 두 번째 문장입니다."},
    }


if __name__ == "__main__":
    unittest.main()
