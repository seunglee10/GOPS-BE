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


class _BlockingReplayController:
    def __init__(self) -> None:
        self.source = SimpleNamespace(
            dataset_id="sp500-top20-plus-amd-mu-20260715-kst-v2",
            total_events=1,
        )
        self.state = "running"
        self._status_calls = 0
        self._controller_lock = threading.Lock()
        self.pump_blocked = threading.Event()
        self.release_pump = threading.Event()

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


class SimulatorServerResponsivenessTests(unittest.TestCase):
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

    def test_simulator_does_not_expose_account_order_or_condition_ledgers(self) -> None:
        app = create_app(replay_controller=_BlockingReplayController())
        paths = {route.path for route in app.routes}
        self.assertNotIn("/api/control/account", paths)
        self.assertNotIn("/api/control/orders", paths)
        self.assertNotIn("/api/control/conditions", paths)
        self.assertIn("/api/control/order-flow", paths)

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


if __name__ == "__main__":
    unittest.main()
