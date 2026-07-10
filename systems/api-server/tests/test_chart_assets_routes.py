from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
for path in (
    ROOT / "systems" / "market-data" / "shared",
    ROOT / "systems" / "order" / "shared",
    ROOT / "systems" / "agent-orchestration" / "shared",
    ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend",
):
    if str(path) not in sys.path: sys.path.insert(0, str(path))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from gops_agents.chart_assets.progress import InMemoryChartAssetProgressStore  # noqa: E402
from gops_agents.chart_assets.queue import InMemoryChartAssetBuildQueue  # noqa: E402


class FakeStorage:
    def __init__(self):
        self.assets = {"NVDA": {"1D": {"symbol": "NVDA", "interval": "1D"}, "1W": None, "1M": None}}
    def get_symbol_assets(self, symbol): return self.assets.get(symbol, {"1D": None, "1W": None, "1M": None})
    def coverage(self, symbols=None):
        items = [{"symbol": "NVDA", "interval": "1D", "generatedAt": "2026-07-11T00:00:00.000Z", "status": "ready"}]
        return [item for item in items if not symbols or item["symbol"] in symbols]


class FailingQueue:
    def submit(self, envelope): raise RuntimeError("kafka unavailable")


class ChartAssetsRoutesTest(unittest.TestCase):
    def setUp(self):
        os.environ["AUTH_ENABLED"] = "false"
        self.storage = FakeStorage()
        self.progress = InMemoryChartAssetProgressStore()
        self.queue = InMemoryChartAssetBuildQueue()
        self.patches = [
            patch("app.routes.chart_assets.chart_asset_storage", return_value=self.storage),
            patch("app.routes.chart_assets.chart_asset_progress_store", return_value=self.progress),
            patch("app.routes.chart_assets.chart_asset_build_queue", return_value=self.queue),
            patch("app.routes.chart_assets.sp500_universe_symbols", return_value=["NVDA", "AAPL"]),
            patch("app.routes.chart_assets.configured_universe_symbols", return_value=["NVDA", "AAPL"]),
        ]
        for current in self.patches: current.start()
        self.client = TestClient(create_app())

    def tearDown(self):
        for current in reversed(self.patches): current.stop()

    def test_serves_all_intervals_and_missing_symbol_as_200(self):
        ready = self.client.get("/api/charts/analysis-assets", params={"symbol": "NVDA"})
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json()["assets"]["1D"]["interval"], "1D")
        missing = self.client.get("/api/charts/analysis-assets", params={"symbol": "AAPL"})
        self.assertEqual(missing.status_code, 200)
        self.assertEqual(missing.json()["assets"], {"1D": None, "1W": None, "1M": None})

    def test_coverage_filters_symbols(self):
        response = self.client.get("/api/charts/analysis-assets/coverage", params={"symbols": "NVDA,AAPL"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 1)

    def test_build_returns_202_and_poll_cancel_stream_work(self):
        submitted = self.client.post("/api/charts/analysis-assets/build", json={"symbols": ["NVDA"], "intervals": ["1D", "1W", "1M"], "llmEnabled": False, "skipFreshHours": 0})
        self.assertEqual(submitted.status_code, 202)
        job_id = submitted.json()["jobId"]
        self.assertEqual(len(self.queue.items), 1)
        status = self.client.get(f"/api/charts/analysis-assets/build/{job_id}")
        self.assertEqual(status.json()["status"], "queued")
        canceled = self.client.post(f"/api/charts/analysis-assets/build/{job_id}/cancel")
        self.assertTrue(canceled.json()["cancelRequested"])
        self.progress.set_status(job_id, "canceled", finishedAt="2026-07-11T00:00:00.000Z")
        stream = self.client.get(f"/api/charts/analysis-assets/build/{job_id}/stream")
        self.assertEqual(stream.status_code, 200)
        self.assertIn("event: status", stream.text)

    def test_enqueue_failure_is_not_reported_as_queued(self):
        with patch("app.routes.chart_assets.chart_asset_build_queue", return_value=FailingQueue()):
            response = self.client.post("/api/charts/analysis-assets/build", json={"symbols": ["NVDA"], "llmEnabled": False})
        self.assertEqual(response.status_code, 503)
        state = next(iter(self.progress._states.values()))
        self.assertEqual(state["status"], "failed")

    def test_rejects_unregistered_symbol(self):
        response = self.client.post("/api/charts/analysis-assets/build", json={"symbols": ["ZZZZ"], "llmEnabled": False})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__": unittest.main()
