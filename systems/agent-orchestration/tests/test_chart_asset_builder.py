from __future__ import annotations

import copy
import json
import math
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "systems" / "agent-orchestration" / "shared", ROOT / "systems" / "market-data" / "shared"):
    if str(path) not in sys.path: sys.path.insert(0, str(path))

from gops_agents.chart_assets.builder import ChartAssetBuilder  # noqa: E402
from gops_agents.chart_assets.envelope import ChartAssetBuildEnvelope  # noqa: E402
from gops_agents.chart_assets.progress import InMemoryChartAssetProgressStore  # noqa: E402
from gops_agents.chart_assets.curation import deterministic_curation  # noqa: E402
from alfaka.analytics.analysis_candles import analysis_input_digest  # noqa: E402


class FakeCandleLoader:
    def __init__(self): self.calls = []; self.bundle_calls = 0
    def load(self, symbol, interval):
        self.calls.append((symbol, interval))
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        return [{
            "timestamp": (start + timedelta(days=index)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "open": 100 + index * 0.2, "high": 102 + index * 0.2 + math.sin(index),
            "low": 98 + index * 0.2 + math.sin(index), "close": 100 + index * 0.2 + math.sin(index),
            "volume": 1000 + index,
        } for index in range(72)]
    def load_symbol(self, symbol, intervals):
        self.bundle_calls += 1
        rows = {interval: self.load(symbol, interval) for interval in intervals}
        coverage = {interval: {
            "expectedBars": len(values), "actualBars": len(values), "missingBars": 0,
            "coverageRatio": 1, "recentContiguousBars": len(values), "largestGapBars": 0,
            "lastExpectedClosedAt": values[-1]["timestamp"], "lastActualClosedAt": values[-1]["timestamp"],
            "renderable": True, "qualityFlags": [],
        } for interval, values in rows.items()}
        return SimpleNamespace(rows=rows, coverage=coverage, digests={interval: analysis_input_digest(symbol, interval, values) for interval, values in rows.items()})


class FakeStorage:
    def __init__(self, initial=None, cancel_after=None, progress=None, job_id=None):
        self.assets = copy.deepcopy(initial or {})
        self.saved = []
        self.snapshot_calls = []
        self.cancel_after = cancel_after
        self.progress = progress
        self.job_id = job_id
    def get(self, symbol, interval): return copy.deepcopy(self.assets.get((symbol, interval)))
    def get_symbol_assets(self, symbol):
        self.snapshot_calls.append(symbol)
        return {interval: copy.deepcopy(self.assets.get((symbol, interval))) for interval in ("1D", "1W", "1M")}
    def save(self, asset):
        self.assets[(asset["symbol"], asset["interval"])] = copy.deepcopy(asset)
        self.saved.append(copy.deepcopy(asset))
        if self.cancel_after and len(self.saved) == self.cancel_after: self.progress.request_cancel(self.job_id)


class FakeCurator:
    model = "mock-model"
    def __init__(self): self.calls = 0
    def curate_symbol(self, bundle):
        self.calls += 1
        return {"output": deterministic_curation(bundle), "degraded": False, "reason": None, "model": self.model, "usage": {}}


def envelope(symbols=("NVDA", "AAPL"), intervals=("1D", "1W", "1M"), job_id="cab-12345678-test", llm_enabled=False, force=False):
    return ChartAssetBuildEnvelope.create(requested_by="test", symbols=symbols, intervals=intervals, llm_enabled=llm_enabled, force=force, job_id=job_id, submitted_at="2026-07-11T00:00:00.000Z")


class ChartAssetBuilderTest(unittest.TestCase):
    def test_rule_only_job_builds_six_assets_and_completes(self):
        progress = InMemoryChartAssetProgressStore(); request = envelope(); progress.initialize(request)
        loader = FakeCandleLoader(); storage = FakeStorage()
        state = ChartAssetBuilder(candle_loader=loader, storage=storage, progress=progress, concurrency=2).run(request)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["progress"], {"total": 6, "done": 6, "failed": 0, "skipped": 0, "warnings": 0, "current": state["progress"]["current"]})
        self.assertEqual(len(storage.saved), 6)
        self.assertEqual(loader.bundle_calls, 2)
        self.assertCountEqual(storage.snapshot_calls, request.symbols)
        created_entities = sum(
            len(layer["drawings"])
            for asset in storage.saved
            for layer in asset["layers"].values()
        )
        self.assertTrue(any("entities=" in line and "(S=" in line for line in state["logs"]))
        self.assertIn(f"created_entities={created_entities}", state["logs"][-1])
        self.assertIn("done=6/6", state["logs"][-1])
        for symbol in request.symbols:
            self.assertEqual([interval for current_symbol, interval in loader.calls if current_symbol == symbol], ["1M", "1W", "1D"])
        one_day = storage.assets[("NVDA", "1D")]
        self.assertEqual(one_day["assetVersion"], "v2")
        self.assertEqual(one_day["build"]["agentOutcome"], "not_requested_empty")
        self.assertLessEqual(len(json.dumps(one_day, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()), 12 * 1024)
        selected_ids = {item["candidateId"] for layer in one_day["layers"].values() for item in layer.get("selected", [])}
        self.assertTrue({item["id"] for item in one_day["features"]["levels"]}.issubset(selected_ids))
        self.assertTrue({item["id"] for item in one_day["features"]["trends"]}.issubset(selected_ids))

    def test_cancel_marks_remaining_items_skipped(self):
        request = envelope(symbols=("NVDA",), job_id="cab-12345678-cancel")
        progress = InMemoryChartAssetProgressStore(); progress.initialize(request)
        storage = FakeStorage(cancel_after=1, progress=progress, job_id=request.job_id)
        state = ChartAssetBuilder(candle_loader=FakeCandleLoader(), storage=storage, progress=progress, concurrency=1).run(request)
        self.assertEqual(state["status"], "canceled")
        self.assertEqual(state["progress"]["done"], 3)
        self.assertEqual(state["progress"]["skipped"], 2)

    def test_rule_only_rebuild_does_not_copy_v1_agent_layer(self):
        existing_agent = {"drawings": [{"id": "kept"}], "intents": [], "rationale": "kept", "degraded": False, "model": "mock", "droppedIntents": []}
        existing = {("NVDA", "1D"): {
            "promptVersion": "prompt-v1", "status": "ready", "layers": {"agent": existing_agent},
            "chartSetup": {"recommended": [{"layer": "ema:20", "reason": "기존 LLM 제안", "source": "llm"}]},
            "commentary": {"text": "기존", "keyLevels": [], "invalidation": "기존", "confidence": 0.5, "enrichment": None},
        }}
        request = envelope(symbols=("NVDA",), intervals=("1D",), job_id="cab-12345678-preserve")
        progress = InMemoryChartAssetProgressStore(); progress.initialize(request); storage = FakeStorage(existing)
        ChartAssetBuilder(candle_loader=FakeCandleLoader(), storage=storage, progress=progress, concurrency=1).run(request)
        rebuilt = storage.assets[("NVDA", "1D")]
        self.assertEqual(rebuilt["layers"]["agent"]["drawings"], [])
        self.assertEqual(rebuilt["layers"]["agent"]["emptyReason"], "llm_not_requested")
        self.assertEqual(rebuilt["promptVersion"], "prompt-v2")
        self.assertEqual(rebuilt["build"]["agentOutcome"], "not_requested_empty")

    def test_standalone_1d_uses_stored_higher_timeframes(self):
        higher = {
            ("NVDA", "1M"): {"assetVersion":"v2","asOf": "2026-06-01T00:00:00.000Z", "quality":{"state":"eligible"},"build":{"ruleDigest":"m"},"features": {"regime": {}}},
            ("NVDA", "1W"): {"assetVersion":"v2","asOf": "2026-07-06T00:00:00.000Z", "quality":{"state":"eligible"},"build":{"ruleDigest":"w"},"features": {"regime": {}}},
        }
        request = envelope(symbols=("NVDA",), intervals=("1D",), job_id="cab-12345678-higher")
        progress = InMemoryChartAssetProgressStore(); progress.initialize(request); storage = FakeStorage(higher)
        ChartAssetBuilder(candle_loader=FakeCandleLoader(), storage=storage, progress=progress, concurrency=1).run(request)
        context = storage.assets[("NVDA", "1D")]["buildContext"]
        self.assertEqual(set(context["higherTf"]), {"1M", "1W"})
        self.assertEqual(context["flags"], [])

    def test_failure_list_is_not_truncated_with_recent_items(self):
        request = envelope(symbols=tuple(f"S{index}" for index in range(60)), intervals=("1D",), job_id="cab-12345678-failures")
        progress = InMemoryChartAssetProgressStore(); progress.initialize(request)
        for index, symbol in enumerate(request.symbols):
            progress.record_item(request.job_id, {
                "symbol": symbol, "interval": "1D", "status": "failed",
                "stage": "kernel", "error": f"failure-{index}", "elapsedMs": 1,
            })
        state = progress.get(request.job_id)
        self.assertEqual(len(state["recentItems"]), 50)
        self.assertEqual(len(state["failedItems"]), 60)

    def test_second_identical_symbol_build_skips_kernel_llm_and_write(self):
        storage = FakeStorage(); service = FakeCurator(); loader = FakeCandleLoader()
        first = envelope(symbols=("NVDA",), job_id="cab-12345678-noop-a", llm_enabled=True)
        first_progress = InMemoryChartAssetProgressStore(); first_progress.initialize(first)
        ChartAssetBuilder(candle_loader=loader, storage=storage, progress=first_progress, llm_service=service, concurrency=1).run(first)
        saved_count = len(storage.saved)
        second = envelope(symbols=("NVDA",), job_id="cab-12345678-noop-b", llm_enabled=True)
        second_progress = InMemoryChartAssetProgressStore(); second_progress.initialize(second)
        state = ChartAssetBuilder(candle_loader=loader, storage=storage, progress=second_progress, llm_service=service, concurrency=1).run(second)
        self.assertEqual(service.calls, 1)
        self.assertEqual(len(storage.saved), saved_count)
        self.assertEqual({item["status"] for item in state["recentItems"]}, {"unchanged"})

        forced = envelope(symbols=("NVDA",), job_id="cab-12345678-noop-force", llm_enabled=True, force=True)
        forced_progress = InMemoryChartAssetProgressStore(); forced_progress.initialize(forced)
        forced_state = ChartAssetBuilder(candle_loader=loader, storage=storage, progress=forced_progress, llm_service=service, concurrency=1).run(forced)
        self.assertEqual(service.calls, 2, "force evaluates curator once")
        self.assertEqual(len(storage.saved), saved_count, "audit timestamps do not change content digest")
        self.assertEqual({item.get("reason") for item in forced_state["recentItems"]}, {"unchanged_after_force"})

    def test_late_identical_intent_skips_llm_and_write_after_kernel(self):
        storage = FakeStorage(); service = FakeCurator(); loader = FakeCandleLoader()
        first = envelope(symbols=("NVDA",), job_id="cab-12345678-late-a", llm_enabled=True)
        first_progress = InMemoryChartAssetProgressStore(); first_progress.initialize(first)
        ChartAssetBuilder(candle_loader=loader, storage=storage, progress=first_progress, llm_service=service, concurrency=1).run(first)
        saved_count = len(storage.saved)
        for asset in storage.assets.values():
            asset["build"]["preKernelDigest"] = "sha256:" + "f" * 64

        second = envelope(symbols=("NVDA",), job_id="cab-12345678-late-b", llm_enabled=True)
        second_progress = InMemoryChartAssetProgressStore(); second_progress.initialize(second)
        state = ChartAssetBuilder(candle_loader=loader, storage=storage, progress=second_progress, llm_service=service, concurrency=1).run(second)

        self.assertEqual(loader.bundle_calls, 2)
        self.assertEqual(service.calls, 1, "late intent no-op skips the second curator call")
        self.assertEqual(len(storage.saved), saved_count)
        self.assertEqual({item.get("reason") for item in state["recentItems"]}, {"late_intent_unchanged"})
        self.assertTrue(any("unchanged after kernel" in line and "entities=" in line for line in state["logs"]))


if __name__ == "__main__": unittest.main()
