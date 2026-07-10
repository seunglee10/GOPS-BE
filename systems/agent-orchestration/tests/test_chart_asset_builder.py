from __future__ import annotations

import copy
import math
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "systems" / "agent-orchestration" / "shared", ROOT / "systems" / "market-data" / "shared"):
    if str(path) not in sys.path: sys.path.insert(0, str(path))

from gops_agents.chart_assets.builder import ChartAssetBuilder  # noqa: E402
from gops_agents.chart_assets.envelope import ChartAssetBuildEnvelope  # noqa: E402
from gops_agents.chart_assets.progress import InMemoryChartAssetProgressStore  # noqa: E402


class FakeCandleLoader:
    def __init__(self): self.calls = []
    def load(self, symbol, interval):
        self.calls.append((symbol, interval))
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        return [{
            "timestamp": (start + timedelta(days=index)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "open": 100 + index * 0.2, "high": 102 + index * 0.2 + math.sin(index),
            "low": 98 + index * 0.2 + math.sin(index), "close": 100 + index * 0.2 + math.sin(index),
            "volume": 1000 + index,
        } for index in range(72)]


class FakeStorage:
    def __init__(self, initial=None, cancel_after=None, progress=None, job_id=None):
        self.assets = copy.deepcopy(initial or {})
        self.saved = []
        self.cancel_after = cancel_after
        self.progress = progress
        self.job_id = job_id
    def get(self, symbol, interval): return copy.deepcopy(self.assets.get((symbol, interval)))
    def save(self, asset):
        self.assets[(asset["symbol"], asset["interval"])] = copy.deepcopy(asset)
        self.saved.append(copy.deepcopy(asset))
        if self.cancel_after and len(self.saved) == self.cancel_after: self.progress.request_cancel(self.job_id)
    def is_fresh(self, symbol, interval, hours): return False


def envelope(symbols=("NVDA", "AAPL"), intervals=("1D", "1W", "1M"), job_id="cab-12345678-test"):
    return ChartAssetBuildEnvelope.create(requested_by="test", symbols=symbols, intervals=intervals, llm_enabled=False, job_id=job_id, submitted_at="2026-07-11T00:00:00.000Z")


class ChartAssetBuilderTest(unittest.TestCase):
    def test_rule_only_job_builds_six_assets_and_completes(self):
        progress = InMemoryChartAssetProgressStore(); request = envelope(); progress.initialize(request)
        loader = FakeCandleLoader(); storage = FakeStorage()
        state = ChartAssetBuilder(candle_loader=loader, storage=storage, progress=progress, concurrency=2).run(request)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["progress"], {"total": 6, "done": 6, "failed": 0, "skipped": 0, "current": state["progress"]["current"]})
        self.assertEqual(len(storage.saved), 6)
        for symbol in request.symbols:
            self.assertEqual([interval for current_symbol, interval in loader.calls if current_symbol == symbol], ["1M", "1W", "1D"])
        one_day = storage.assets[("NVDA", "1D")]
        self.assertEqual(set(one_day["buildContext"]["higherTf"]), {"1M", "1W"})
        self.assertTrue(one_day["layers"]["structure"]["drawings"])

    def test_cancel_marks_remaining_items_skipped(self):
        request = envelope(symbols=("NVDA",), job_id="cab-12345678-cancel")
        progress = InMemoryChartAssetProgressStore(); progress.initialize(request)
        storage = FakeStorage(cancel_after=1, progress=progress, job_id=request.job_id)
        state = ChartAssetBuilder(candle_loader=FakeCandleLoader(), storage=storage, progress=progress, concurrency=1).run(request)
        self.assertEqual(state["status"], "canceled")
        self.assertEqual(state["progress"]["done"], 3)
        self.assertEqual(state["progress"]["skipped"], 2)

    def test_rule_only_rebuild_preserves_existing_agent_layer(self):
        existing_agent = {"drawings": [{"id": "kept"}], "intents": [], "rationale": "kept", "degraded": False, "model": "mock", "droppedIntents": []}
        existing = {("NVDA", "1D"): {"promptVersion": "prompt-v1", "status": "ready", "layers": {"agent": existing_agent}, "commentary": {"text": "기존", "keyLevels": [], "invalidation": "기존", "confidence": 0.5, "enrichment": None}}}
        request = envelope(symbols=("NVDA",), intervals=("1D",), job_id="cab-12345678-preserve")
        progress = InMemoryChartAssetProgressStore(); progress.initialize(request); storage = FakeStorage(existing)
        ChartAssetBuilder(candle_loader=FakeCandleLoader(), storage=storage, progress=progress, concurrency=1).run(request)
        rebuilt = storage.assets[("NVDA", "1D")]
        self.assertEqual(rebuilt["layers"]["agent"], existing_agent)
        self.assertEqual(rebuilt["commentary"]["text"], "기존")
        self.assertEqual(rebuilt["promptVersion"], "prompt-v1")

    def test_standalone_1d_uses_stored_higher_timeframes(self):
        higher = {
            ("NVDA", "1M"): {"asOf": "2026-06-01T00:00:00.000Z", "features": {"levels": []}},
            ("NVDA", "1W"): {"asOf": "2026-07-06T00:00:00.000Z", "features": {"levels": []}},
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


if __name__ == "__main__": unittest.main()
