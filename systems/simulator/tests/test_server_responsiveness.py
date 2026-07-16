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
        self.pump_blocked = threading.Event()
        self.release_pump = threading.Event()

    def status(self) -> dict[str, object]:
        self._status_calls += 1
        if self._status_calls == 2:
            self.pump_blocked.set()
            self.release_pump.wait(timeout=1)
        return {"mode": "simulation", "state": self.state}


class SimulatorServerResponsivenessTests(unittest.TestCase):
    def test_health_stays_responsive_while_replay_pump_is_blocked(self) -> None:
        controller = _BlockingReplayController()
        app = create_app(replay_controller=controller)

        with TestClient(app) as client:
            self.assertTrue(controller.pump_blocked.wait(timeout=1), "replay pump did not enter the blocking call")
            response_box: list[object] = []
            request_thread = threading.Thread(
                target=lambda: response_box.append(client.get("/health")),
                daemon=True,
            )
            request_thread.start()
            request_thread.join(timeout=0.2)
            responded_while_pump_blocked = not request_thread.is_alive()

            controller.release_pump.set()
            request_thread.join(timeout=1)

            self.assertTrue(responded_while_pump_blocked)
            self.assertEqual(getattr(response_box[0], "status_code", None), 200)


if __name__ == "__main__":
    unittest.main()
