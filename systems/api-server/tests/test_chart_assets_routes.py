from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
ALL_INTERVALS = ["1m", "5m", "10m", "1h", "4h", "1D", "1W"]
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
        self.assets = {"NVDA": {interval: {"symbol": "NVDA", "interval": interval} if interval == "1D" else None for interval in ALL_INTERVALS}}
    def get_symbol_assets(self, symbol): return self.assets.get(symbol, {interval: None for interval in ALL_INTERVALS})
    def coverage(self, symbols=None):
        items = [{"symbol": "NVDA", "interval": "1D", "generatedAt": "2026-07-11T00:00:00.000Z", "status": "ready"}]
        return [item for item in items if not symbols or item["symbol"] in symbols]
    def delete(self, symbols, intervals):
        self.deleted = (symbols, intervals)
        return 1


class FailingQueue:
    def __init__(self): self.envelope = None
    def submit(self, envelope):
        self.envelope = envelope
        raise RuntimeError("kafka unavailable")


class FailingStorage:
    def get_symbol_assets(self, _symbol): raise RuntimeError("clickhouse unavailable")
    def coverage(self, _symbols=None): raise RuntimeError("clickhouse unavailable")
    def delete(self, _symbols, _intervals): raise RuntimeError("clickhouse unavailable")


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
        self.assertEqual(missing.json()["assets"], {interval: None for interval in ALL_INTERVALS})

    def test_coverage_filters_symbols(self):
        response = self.client.get("/api/charts/analysis-assets/coverage", params={"symbols": "NVDA,AAPL"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 1)

    def test_delete_removes_selected_asset_rows(self):
        response = self.client.delete("/api/charts/analysis-assets", params={"symbols": "nvda", "intervals": "1D,1W"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"symbols": ["NVDA"], "intervals": ["1D", "1W"], "deleted": 1})
        self.assertEqual(self.storage.deleted, (["NVDA"], ["1D", "1W"]))

    def test_delete_rejects_invalid_interval(self):
        response = self.client.delete("/api/charts/analysis-assets", params={"symbols": "NVDA", "intervals": "4H"})
        self.assertEqual(response.status_code, 400)

    def test_build_returns_202_and_poll_cancel_work(self):
        submitted = self.client.post("/api/charts/analysis-assets/build", json={"symbols": ["NVDA"], "intervals": ["1D", "1W"]})
        self.assertEqual(submitted.status_code, 202)
        job_id = submitted.json()["jobId"]
        self.assertEqual(len(self.queue.items), 1)
        self.assertEqual(self.queue.items[0]["source"], "manual")
        self.assertEqual(self.queue.items[0]["priority"], 100)
        status = self.client.get(f"/api/charts/analysis-assets/build/{job_id}")
        self.assertEqual(status.json()["status"], "queued")
        canceled = self.client.post(f"/api/charts/analysis-assets/build/{job_id}/cancel")
        self.assertTrue(canceled.json()["cancelRequested"])
        self.progress.set_status(job_id, "canceled", finishedAt="2026-07-11T00:00:00.000Z")

    def test_identical_active_manual_builds_are_coalesced(self):
        first = self.client.post(
            "/api/charts/analysis-assets/build",
            json={"symbols": ["NVDA"], "intervals": ["1D"]},
        )
        second = self.client.post(
            "/api/charts/analysis-assets/build",
            json={"symbols": ["NVDA"], "intervals": ["1D"]},
        )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(second.json()["jobId"], first.json()["jobId"])
        self.assertTrue(second.json()["coalesced"])
        self.assertEqual(len(self.queue.items), 1)

    def test_sp500_build_expands_registry_and_preserves_envelope_options(self):
        response = self.client.post("/api/charts/analysis-assets/build", json={
            "symbols": "sp500", "intervals": ["1W", "1D"], "force": True,
        })

        self.assertEqual(response.status_code, 202)
        envelope = self.queue.items[-1]
        self.assertEqual(envelope["symbols"], ["NVDA", "AAPL"])
        self.assertEqual(envelope["intervals"], ["1W", "1D"])
        self.assertNotIn("llmEnabled", envelope)
        self.assertNotIn("skipFreshHours", envelope)
        self.assertTrue(envelope["force"])

    def test_sp500_build_rejects_missing_registry_instead_of_using_fallback(self):
        with patch("app.routes.chart_assets.sp500_universe_symbols", return_value=[]):
            response = self.client.post("/api/charts/analysis-assets/build", json={
                "symbols": "sp500",
            })
        self.assertEqual(response.status_code, 503)

    def test_build_accepts_all_chart_intervals(self):
        response = self.client.post("/api/charts/analysis-assets/build", json={
            "symbols": ["NVDA"], "intervals": ALL_INTERVALS,
        })
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.queue.items[-1]["intervals"], ALL_INTERVALS)

    def test_rejects_invalid_intervals(self):
        response = self.client.post("/api/charts/analysis-assets/build", json={
            "symbols": ["NVDA"], "intervals": ["15m"],
        })
        self.assertEqual(response.status_code, 422)

    def test_build_route_requires_session_when_auth_is_enabled(self):
        from app.auth.config import AuthConfig
        from app.auth.session_store import MemorySessionStore

        with patch.dict(os.environ, {"AUTH_ENABLED": "true", "AUTH_SESSION_SECRET": "test-session-secret"}, clear=False):
            self.client.app.state.auth_session_store = MemorySessionStore(AuthConfig.from_env())
            response = self.client.post("/api/charts/analysis-assets/build", json={
                "symbols": ["NVDA"],
            })

        self.assertEqual(response.status_code, 401)

    def test_storage_failures_return_503(self):
        with patch("app.routes.chart_assets.chart_asset_storage", return_value=FailingStorage()):
            asset = self.client.get("/api/charts/analysis-assets", params={"symbol": "NVDA"})
            coverage = self.client.get("/api/charts/analysis-assets/coverage")
            deleted = self.client.delete("/api/charts/analysis-assets", params={"symbols": "NVDA"})
        self.assertEqual(asset.status_code, 503)
        self.assertEqual(coverage.status_code, 503)
        self.assertEqual(deleted.status_code, 503)

    def test_storage_migration_maintenance_keeps_reads_live_and_blocks_mutations(self):
        with patch.dict(os.environ, {"CHART_ASSET_STORAGE_MAINTENANCE": "true"}):
            asset = self.client.get("/api/charts/analysis-assets", params={"symbol": "NVDA"})
            build = self.client.post("/api/charts/analysis-assets/build", json={"symbols": ["NVDA"]})
            deleted = self.client.delete("/api/charts/analysis-assets", params={"symbols": "NVDA"})
        self.assertEqual(asset.status_code, 200)
        self.assertEqual(build.status_code, 503)
        self.assertEqual(deleted.status_code, 503)

    def test_enqueue_failure_is_not_reported_as_queued(self):
        queue = FailingQueue()
        with patch("app.routes.chart_assets.chart_asset_build_queue", return_value=queue):
            response = self.client.post("/api/charts/analysis-assets/build", json={"symbols": ["NVDA"]})
        self.assertEqual(response.status_code, 503)
        state = self.progress.get(queue.envelope.job_id)
        self.assertEqual(state["status"], "failed")

    def test_rejects_unregistered_symbol(self):
        response = self.client.post("/api/charts/analysis-assets/build", json={"symbols": ["ZZZZ"]})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__": unittest.main()
