from __future__ import annotations

import os
import threading
import unittest
from types import SimpleNamespace

from fastapi.testclient import TestClient

os.environ.pop("REDIS_URL", None)
os.environ.pop("CLICKHOUSE_URL", None)
os.environ["SIM_ENV_FILE"] = "/tmp/gops-simulator-test-no-env"

from systems.simulator.gops_simul.server import create_app


class _BlockingReplayController:
    def __init__(self) -> None:
        self.source = SimpleNamespace(
            dataset_id="sp500-top20-20260715-kst-v1",
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


if __name__ == "__main__":
    unittest.main()
