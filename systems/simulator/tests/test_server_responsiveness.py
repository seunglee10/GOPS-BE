from __future__ import annotations

import os
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

os.environ.pop("REDIS_URL", None)
os.environ.pop("CLICKHOUSE_URL", None)
os.environ["SIM_ENV_FILE"] = "/tmp/gops-simulator-test-no-env"

REPO_ROOT = Path(__file__).resolve().parents[3]
SIMULATOR_ROOT = REPO_ROOT / "systems" / "simulator"
MARKET_DATA_SHARED_ROOT = REPO_ROOT / "systems" / "market-data" / "shared"
for source_root in (SIMULATOR_ROOT, MARKET_DATA_SHARED_ROOT):
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

from systems.simulator.gops_simul.server import create_app
from systems.simulator.gops_simul.tick_replay import InMemoryReplayEventSource, ReplayController


class _BlockingReplayController:
    def __init__(self) -> None:
        self.source = SimpleNamespace(
            dataset_id="sp500-full-20260715-kst-v3",
            total_events=1,
        )
        self.state = "running"
        self._status_calls = 0
        self._controller_lock = threading.Lock()
        self.pump_blocked = threading.Event()
        self.release_pump = threading.Event()
        self.run_id = "run-1"

    def status(self) -> dict[str, object]:
        with self._controller_lock:
            self._status_calls += 1
            if self._status_calls == 2:
                self.pump_blocked.set()
                self.release_pump.wait(timeout=1)
            return self.status_snapshot()

    def status_snapshot(self) -> dict[str, object]:
        return {
            "datasetId": self.source.dataset_id,
            "mode": "simulation",
            "state": self.state,
            "runId": "run-1",
            "virtualTime": "2026-07-15T00:00:00+09:00",
            "totalEventCount": self.source.total_events,
        }

    def latest_quotes_details(self, symbols: list[str]) -> dict[str, dict[str, object]]:
        return {
            "NVDA": {
                "bid": 99.0,
                "ask": 100.0,
                "sequence": 10,
                "virtualTime": "2026-07-15T00:00:01+09:00",
            }
        }


class SimulatorServerResponsivenessTests(unittest.TestCase):
    def test_speed_endpoint_accepts_only_the_supported_replay_speeds(self) -> None:
        app = create_app(replay_controller=ReplayController(InMemoryReplayEventSource([])))

        with TestClient(app) as client:
            for speed in (1, 2, 5, 10):
                response = client.put("/api/control/speed", json={"speed": speed})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["requestedSpeed"], speed)
            for speed in (20, 60, 300):
                response = client.put("/api/control/speed", json={"speed": speed})
                self.assertEqual(response.status_code, 422)

    def _assert_endpoint_stays_responsive(self, path: str) -> None:
        controller = _BlockingReplayController()
        app = create_app(replay_controller=controller)

        with TestClient(app) as client:
            self.assertTrue(controller.pump_blocked.wait(timeout=1), "replay pump did not enter the blocking call")
            response_box: list[object] = []
            request_thread = threading.Thread(
                target=lambda: response_box.append(client.get(path)),
                daemon=True,
            )
            request_thread.start()
            request_thread.join(timeout=0.2)
            responded_while_pump_blocked = not request_thread.is_alive()

            controller.release_pump.set()
            request_thread.join(timeout=1)

            self.assertTrue(responded_while_pump_blocked)
            self.assertEqual(getattr(response_box[0], "status_code", None), 200)

    def test_health_stays_responsive_while_replay_pump_is_blocked(self) -> None:
        self._assert_endpoint_stays_responsive("/health")

    def test_status_uses_non_blocking_snapshot_while_replay_pump_is_blocked(self) -> None:
        self._assert_endpoint_stays_responsive("/api/control/status")

    def test_batch_quotes_use_the_non_blocking_quote_snapshot(self) -> None:
        controller = _BlockingReplayController()
        app = create_app(replay_controller=controller)

        with TestClient(app) as client:
            response = client.get("/api/control/quotes?symbols=NVDA,MSFT")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["quotes"]["NVDA"]["bid"], 99.0)
        self.assertEqual(response.json()["missingSymbols"], ["MSFT"])

    def test_simulator_does_not_expose_account_order_or_condition_ledgers(self) -> None:
        app = create_app(replay_controller=_BlockingReplayController())
        paths = {route.path for route in app.routes}
        self.assertNotIn("/api/control/account", paths)
        self.assertNotIn("/api/control/orders", paths)
        self.assertNotIn("/api/control/conditions", paths)
        self.assertIn("/api/control/order-flow", paths)
        self.assertIn("/api/control/indices/performance", paths)

    def test_indices_expose_the_fixed_july_15_snapshot_without_future_data(self) -> None:
        controller = _BlockingReplayController()
        app = create_app(replay_controller=controller)

        with TestClient(app) as client:
            response = client.get("/api/control/indices")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["source"], "simulation_replay")
        self.assertEqual(payload["datasetId"], controller.source.dataset_id)
        self.assertEqual(payload["period"], "2026-07-15-kst")
        self.assertEqual(payload["updatedAt"], "2026-07-15T00:00:00+09:00")
        self.assertEqual(payload["virtualTime"], "2026-07-15T00:00:00+09:00")
        self.assertEqual(payload["coverage"]["priced"], payload["coverage"]["total"])
        self.assertIn("^GSPC", {item["symbol"] for item in payload["items"]})

    def test_index_performance_uses_only_timestamped_fixed_observations(self) -> None:
        controller = _BlockingReplayController()
        app = create_app(replay_controller=controller)

        with TestClient(app) as client:
            response = client.get(
                "/api/control/indices/performance?range=1M&startAt=2026-06-14T15:00:00Z"
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["symbol"], "^GSPC")
        self.assertEqual(payload["source"], "fred-sp500+simulation_replay")
        self.assertEqual(payload["range"], "1M")
        self.assertEqual(payload["points"][0]["time"], "2026-06-15T20:00:00Z")
        self.assertEqual(payload["points"][0]["returnPercent"], 0)
        self.assertEqual(payload["points"][-1]["time"], "2026-07-14T14:55:00Z")
        self.assertGreater(len(payload["points"]), 20)
        self.assertTrue(all(point["time"] <= "2026-07-14T15:00:00Z" for point in payload["points"]))


if __name__ == "__main__":
    unittest.main()
