from __future__ import annotations

import copy
import json
import math
import sys
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "systems" / "agent-orchestration" / "shared", ROOT / "systems" / "market-data" / "shared"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from gops_agents.chart_assets.builder import ChartAssetBuilder  # noqa: E402
from gops_agents.chart_assets.curation import deterministic_curation  # noqa: E402
from gops_agents.chart_assets.envelope import ChartAssetBuildEnvelope  # noqa: E402
from gops_agents.chart_assets.llm import ChartAssetLLMService  # noqa: E402
from gops_agents.chart_assets.progress import InMemoryChartAssetProgressStore  # noqa: E402


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

    def get_symbol_assets(self, symbol):
        return {interval: self.get(symbol, interval) for interval in ("1D", "1W", "1M")}

    def save(self, asset):
        self.assets[(asset["symbol"], asset["interval"])] = copy.deepcopy(asset)
        self.saved.append(copy.deepcopy(asset))


class RecordingLLMService:
    model = "mock"

    def __init__(self):
        self.calls = []

    def curate_symbol(self, bundle):
        self.calls.append((bundle["symbol"], tuple(item["interval"] for item in bundle["intervals"])))
        return {"output": deterministic_curation(bundle), "degraded": False, "reason": None, "model": self.model, "usage": {}}


class RaisingLLMService:
    model = "mock"

    def curate_symbol(self, _bundle):
        raise RuntimeError("injected failure")


class ChartAssetLLMTest(unittest.TestCase):
    def test_rate_limit_retries_once_after_backoff(self):
        bundle = {"symbol": "NVDA", "intervals": [], "crossTimeframe": {"relationIds": [], "evidenceRefs": []}}
        output = {"intervalSelections": []}
        sleeps = []
        opener = Mock(side_effect=[
            urllib.error.HTTPError("https://api.openai.com/v1/responses", 429, "rate limited", None, None),
            FakeResponse({"status": "completed", "output_text": json.dumps(output)}),
        ])
        result = ChartAssetLLMService(api_key="test", opener=opener, sleeper=sleeps.append).curate_symbol(bundle)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(sleeps, [0.5])
        self.assertFalse(result["degraded"])

    def test_timeout_retries_then_builder_saves_degraded_fallback(self):
        opener = Mock(side_effect=TimeoutError("timeout"))
        service = ChartAssetLLMService(api_key="test", opener=opener)
        request = build_envelope("cab-12345678-llm-timeout", intervals=("1D",))
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
        self.assertIsNone(storage.saved[0]["commentary"]["enrichment"])
        self.assertEqual(storage.saved[0]["build"]["agentOutcome"], "degraded")
        self.assertTrue(any("warning=openai_TimeoutError" in line and "entities=" in line for line in state["logs"]))

    def test_builder_catches_llm_exception_and_saves_degraded_asset(self):
        request = build_envelope("cab-12345678-llm-exception", intervals=("1D",))
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

    def test_builder_calls_llm_once_with_month_week_day_bundle(self):
        request = build_envelope("cab-12345678-llm-order")
        progress = InMemoryChartAssetProgressStore(); progress.initialize(request)
        storage = FakeStorage(); service = RecordingLLMService()
        state = ChartAssetBuilder(
            candle_loader=FakeCandleLoader(), storage=storage, progress=progress,
            llm_service=service, concurrency=1,
        ).run(request)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(service.calls, [("NVDA", ("1M", "1W", "1D"))])

    def test_partial_one_day_build_marks_missing_higher_context(self):
        request = build_envelope("cab-12345678-llm-missing-higher", intervals=("1D",))
        progress = InMemoryChartAssetProgressStore(); progress.initialize(request)
        storage = FakeStorage(); service = RecordingLLMService()
        ChartAssetBuilder(
            candle_loader=FakeCandleLoader(), storage=storage, progress=progress,
            llm_service=service, concurrency=1,
        ).run(request)
        self.assertEqual(service.calls, [("NVDA", ("1D",))])
        self.assertEqual(storage.saved[0]["buildContext"]["flags"], ["no_higher_tf_context"])
        self.assertNotIn("no_higher_tf_context", storage.saved[0]["coverage"]["qualityFlags"])


def build_envelope(job_id, intervals=("1D", "1W", "1M")):
    return ChartAssetBuildEnvelope.create(
        requested_by="test",
        symbols=["NVDA"],
        intervals=intervals,
        llm_enabled=True,
        job_id=job_id,
        submitted_at=NOW,
    )


def candles():
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(72):
        close = 100 + index * 0.15 + math.sin(index / 3)
        rows.append({
            "timestamp": (start + timedelta(days=index)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "open": round(close - 0.5, 2),
            "high": round(close + 1.0, 2),
            "low": round(close - 1.0, 2),
            "close": round(close, 2),
            "volume": 1000 + index,
        })
    return rows


if __name__ == "__main__":
    unittest.main()
