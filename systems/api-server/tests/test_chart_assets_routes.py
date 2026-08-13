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
    ROOT / "systems" / "api-server" / "pods" / "api-server",
):
    if str(path) not in sys.path: sys.path.insert(0, str(path))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from app.services.simulator_gateway import SimulatorUnavailable  # noqa: E402
from gops_agents.chart_assets.progress import InMemoryChartAssetProgressStore  # noqa: E402
from gops_agents.chart_assets.queue import InMemoryChartAssetBuildQueue  # noqa: E402


class FakeStorage:
    def __init__(self):
        self.assets = {"NVDA": {interval: {"symbol": "NVDA", "interval": interval} if interval == "1D" else None for interval in ALL_INTERVALS}}
        self.get_calls = []
        self.get_commentary_calls = []
        self.get_symbol_assets_calls = []
        self.snapshot_calls = []
        self.snapshot_commentary_calls = []
        self.snapshot_assets = {}
    def get(self, symbol, interval):
        self.get_calls.append((symbol, interval))
        return self.assets.get(symbol, {}).get(interval)
    def get_symbol_assets(self, symbol):
        self.get_symbol_assets_calls.append(symbol)
        return self.assets.get(symbol, {interval: None for interval in ALL_INTERVALS})
    def get_commentary(self, symbol, interval):
        self.get_commentary_calls.append((symbol, interval))
        if symbol != "NVDA" or interval != "1D":
            return None
        return {
            "assetVersion": "geometry",
            "algorithmVersion": "ohlcv-consensus-pattern-families-v6",
            "asOf": "2026-07-16T04:00:00.000Z",
            "generatedAt": "2026-07-17T00:00:00.000Z",
            "inputDigest": "sha256:input",
            "drawingIds": ["level-1", "pattern-1"],
            "commentary": {"version": "chart-commentary.v2", "status": "ready"},
        }
    def get_snapshot(self, dataset_id, symbol, interval, cutoff=None):
        self.snapshot_calls.append((dataset_id, symbol, interval, cutoff))
        return self.snapshot_assets.get((dataset_id, symbol, interval))
    def get_symbol_snapshots(self, dataset_id, symbol, cutoff=None):
        return {interval: self.get_snapshot(dataset_id, symbol, interval, cutoff) for interval in ALL_INTERVALS}
    def get_snapshot_commentary(self, dataset_id, symbol, interval, cutoff=None):
        self.snapshot_commentary_calls.append((dataset_id, symbol, interval, cutoff))
        asset = self.snapshot_assets.get((dataset_id, symbol, interval))
        if not asset:
            return None
        return {
            "assetVersion": asset["assetVersion"], "algorithmVersion": asset["algorithmVersion"],
            "asOf": asset["asOf"], "generatedAt": asset["generatedAt"],
            "inputDigest": asset["inputDigest"], "drawingIds": ["snapshot-level"],
            "commentary": asset.get("commentary"),
        }
    def coverage(self, symbols=None, dataset_id=None):
        items = [{"symbol": "NVDA", "interval": "1D", "generatedAt": "2026-07-11T00:00:00.000Z", "status": "ready"}]
        return [{**item, **({"datasetId": dataset_id} if dataset_id else {})} for item in items if not symbols or item["symbol"] in symbols]
    def delete(self, symbols, intervals):
        self.deleted = (symbols, intervals)
        return 1


class FailingQueue:
    def __init__(self): self.envelope = None
    def submit(self, envelope):
        self.envelope = envelope
        raise RuntimeError("kafka unavailable")


class FailingStorage:
    def get(self, _symbol, _interval): raise RuntimeError("postgres unavailable")
    def get_symbol_assets(self, _symbol): raise RuntimeError("postgres unavailable")
    def get_commentary(self, _symbol, _interval): raise RuntimeError("postgres unavailable")
    def coverage(self, _symbols=None): raise RuntimeError("postgres unavailable")
    def delete(self, _symbols, _intervals): raise RuntimeError("postgres unavailable")


class FakeSimulatorGateway:
    def __init__(self, mode="live"):
        self.mode = mode
        self.last_status = None

    def status(self):
        self.last_status = {
            "available": True, "mode": self.mode, "state": "paused",
            "datasetId": "dataset-20260715", "runId": "run-1" if self.mode == "simulation" else None,
            "startTime": "2026-07-15T00:00:00.000Z",
            "virtualTime": "2026-07-15T14:00:00.000Z",
        }
        return self.last_status


class FailingSimulatorGateway:
    def __init__(self, last_mode=None):
        self.last_status = {"mode": last_mode} if last_mode else None

    def status(self):
        raise SimulatorUnavailable("simulator timed out")


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
        self.client.app.state.simulator_gateway = FakeSimulatorGateway("live")

    def tearDown(self):
        for current in reversed(self.patches): current.stop()

    def test_serves_all_intervals_and_missing_symbol_as_200(self):
        ready = self.client.get("/api/charts/analysis-assets", params={"symbol": "NVDA"})
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json()["assets"]["1D"]["interval"], "1D")
        missing = self.client.get("/api/charts/analysis-assets", params={"symbol": "AAPL"})
        self.assertEqual(missing.status_code, 200)
        self.assertEqual(missing.json()["assets"], {interval: None for interval in ALL_INTERVALS})

    def test_serves_only_requested_interval(self):
        response = self.client.get("/api/charts/analysis-assets", params={"symbol": "NVDA", "interval": "1D"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["assets"], {"1D": {"symbol": "NVDA", "interval": "1D"}})
        self.assertEqual(self.storage.get_calls, [("NVDA", "1D")])
        self.assertEqual(self.storage.get_symbol_assets_calls, [])

        missing = self.client.get("/api/charts/analysis-assets", params={"symbol": "AAPL", "interval": "1m"})
        self.assertEqual(missing.status_code, 200)
        self.assertEqual(missing.json()["assets"], {"1m": None})

    def test_serves_lightweight_commentary_projection_only(self):
        response = self.client.get(
            "/api/charts/analysis-assets/commentary",
            params={"symbol": "NVDA", "interval": "1D"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["symbol"], "NVDA")
        self.assertEqual(payload["interval"], "1D")
        self.assertEqual(payload["asset"]["drawingIds"], ["level-1", "pattern-1"])
        self.assertNotIn("geometry", payload["asset"])
        self.assertNotIn("analysisTrace", payload["asset"])
        self.assertNotIn("indicators", payload["asset"])
        self.assertNotIn("coverage", payload["asset"])
        self.assertEqual(self.storage.get_commentary_calls, [("NVDA", "1D")])

        missing = self.client.get(
            "/api/charts/analysis-assets/commentary",
            params={"symbol": "AAPL", "interval": "1m"},
        )
        self.assertEqual(missing.status_code, 200)
        self.assertIsNone(missing.json()["asset"])

    def test_coverage_filters_symbols(self):
        response = self.client.get("/api/charts/analysis-assets/coverage", params={"symbols": "NVDA,AAPL"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 1)

    def test_simulation_reads_only_the_dataset_snapshot_and_commentary(self):
        self.client.app.state.simulator_gateway = FakeSimulatorGateway("simulation")
        snapshot = {
            "assetVersion": "geometry", "algorithmVersion": "ohlcv-consensus-pattern-families-v6",
            "symbol": "NVDA", "interval": "1D", "sourceInterval": "1D",
            "asOf": "2026-07-14T04:00:00.000Z", "generatedAt": "2026-07-19T00:00:00.000Z",
            "inputDigest": "sha256:snapshot", "commentary": {"version": "chart-commentary.v2", "status": "ready"},
        }
        self.storage.snapshot_assets[("dataset-20260715", "NVDA", "1D")] = snapshot

        full = self.client.get("/api/charts/analysis-assets", params={"symbol": "NVDA", "interval": "1D"})
        commentary = self.client.get("/api/charts/analysis-assets/commentary", params={"symbol": "NVDA", "interval": "1D"})

        self.assertEqual(full.status_code, 200)
        self.assertEqual(full.json()["assets"]["1D"]["inputDigest"], "sha256:snapshot")
        self.assertEqual(full.json()["meta"]["assetContext"], "simulation")
        self.assertEqual(full.json()["meta"]["snapshotStatus"], "ready")
        self.assertEqual(commentary.json()["asset"]["inputDigest"], "sha256:snapshot")
        self.assertEqual(self.storage.get_calls, [])
        self.assertEqual(self.storage.get_commentary_calls, [])
        self.assertEqual(self.storage.snapshot_calls[-1], (
            "dataset-20260715", "NVDA", "1D", "2026-07-15T14:00:00.000Z"
        ))

    def test_simulation_missing_snapshot_does_not_fallback_to_live(self):
        self.client.app.state.simulator_gateway = FakeSimulatorGateway("simulation")
        response = self.client.get("/api/charts/analysis-assets", params={"symbol": "NVDA", "interval": "1D"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["assets"]["1D"])
        self.assertEqual(response.json()["meta"]["snapshotStatus"], "missing")
        self.assertEqual(self.storage.get_calls, [])

    def test_simulator_status_failure_never_falls_back_to_live_from_sim_or_unknown(self):
        for last_mode in ("simulation", None):
            with self.subTest(last_mode=last_mode):
                self.client.app.state.simulator_gateway = FailingSimulatorGateway(last_mode)
                full = self.client.get(
                    "/api/charts/analysis-assets", params={"symbol": "NVDA", "interval": "1D"},
                )
                commentary = self.client.get(
                    "/api/charts/analysis-assets/commentary",
                    params={"symbol": "NVDA", "interval": "1D"},
                )
                self.assertEqual(full.status_code, 503)
                self.assertEqual(commentary.status_code, 503)
                self.assertEqual(full.json()["detail"], "simulation_service_unavailable")
        self.assertEqual(self.storage.get_calls, [])
        self.assertEqual(self.storage.get_commentary_calls, [])

    def test_simulator_status_failure_can_use_live_only_after_explicit_live_status(self):
        self.client.app.state.simulator_gateway = FailingSimulatorGateway("live")

        response = self.client.get(
            "/api/charts/analysis-assets", params={"symbol": "NVDA", "interval": "1D"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["meta"]["assetContext"], "live")
        self.assertEqual(self.storage.get_calls, [("NVDA", "1D")])

    def test_simulation_build_freezes_server_dataset_start_context(self):
        self.client.app.state.simulator_gateway = FakeSimulatorGateway("simulation")
        response = self.client.post("/api/charts/analysis-assets/build", json={
            "symbols": ["NVDA"], "intervals": ["1D"], "force": True, "target": "simulation",
        })
        self.assertEqual(response.status_code, 202)
        envelope = self.queue.items[-1]
        self.assertEqual(envelope["target"], "simulation")
        self.assertEqual(envelope["datasetId"], "dataset-20260715")
        self.assertEqual(envelope["snapshotCutoff"], "2026-07-15T00:00:00.000Z")

    def test_simulation_build_rejects_absent_context(self):
        self.client.app.state.simulator_gateway = FakeSimulatorGateway("live")
        response = self.client.post("/api/charts/analysis-assets/build", json={
            "symbols": ["NVDA"], "intervals": ["1D"], "force": True, "target": "simulation",
        })
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "simulation_context_unavailable")

    def test_delete_removes_selected_asset_rows(self):
        response = self.client.delete("/api/charts/analysis-assets", params={"symbols": "nvda", "intervals": "1D,1W"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"symbols": ["NVDA"], "intervals": ["1D", "1W"], "deleted": 1})
        self.assertEqual(self.storage.deleted, (["NVDA"], ["1D", "1W"]))

    def test_delete_rejects_invalid_interval(self):
        response = self.client.delete("/api/charts/analysis-assets", params={"symbols": "NVDA", "intervals": "4H"})
        self.assertEqual(response.status_code, 400)

    def test_build_returns_202_and_poll_cancel_work(self):
        submitted = self.client.post("/api/charts/analysis-assets/build", json={"symbols": ["NVDA"], "intervals": ["1m", "1D"]})
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

    def test_sp500_non_force_build_expands_registry_and_preserves_envelope_options(self):
        response = self.client.post("/api/charts/analysis-assets/build", json={
            "symbols": "sp500", "intervals": ["1m", "1D"], "force": False,
        })

        self.assertEqual(response.status_code, 202)
        envelope = self.queue.items[-1]
        self.assertEqual(envelope["symbols"], ["NVDA", "AAPL"])
        self.assertEqual(envelope["intervals"], ["1m", "1D"])
        self.assertNotIn("llmEnabled", envelope)
        self.assertNotIn("skipFreshHours", envelope)
        self.assertFalse(envelope["force"])

    def test_sp500_force_build_is_rejected_before_queueing(self):
        response = self.client.post("/api/charts/analysis-assets/build", json={
            "symbols": "sp500", "intervals": ["1m", "1D"], "force": True,
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn("force refresh", response.json()["detail"])
        self.assertEqual(len(self.queue.items), 0)

    def test_sp500_build_rejects_missing_registry_instead_of_using_fallback(self):
        with patch("app.routes.chart_assets.sp500_universe_symbols", return_value=[]):
            response = self.client.post("/api/charts/analysis-assets/build", json={
                "symbols": "sp500",
            })
        self.assertEqual(response.status_code, 503)

    def test_build_accepts_only_operational_chart_intervals(self):
        response = self.client.post("/api/charts/analysis-assets/build", json={
            "symbols": ["NVDA"], "intervals": ["1m", "1D"],
        })
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.queue.items[-1]["intervals"], ["1m", "1D"])

    def test_build_defaults_to_one_minute_and_one_day(self):
        response = self.client.post("/api/charts/analysis-assets/build", json={"symbols": ["NVDA"]})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.queue.items[-1]["intervals"], ["1m", "1D"])

    def test_build_rejects_supported_but_disabled_interval(self):
        response = self.client.post("/api/charts/analysis-assets/build", json={
            "symbols": ["NVDA"], "intervals": ["5m"],
        })

        self.assertEqual(response.status_code, 422)

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
            commentary = self.client.get("/api/charts/analysis-assets/commentary", params={"symbol": "NVDA", "interval": "1D"})
            coverage = self.client.get("/api/charts/analysis-assets/coverage")
            deleted = self.client.delete("/api/charts/analysis-assets", params={"symbols": "NVDA"})
        self.assertEqual(asset.status_code, 503)
        self.assertEqual(commentary.status_code, 503)
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
