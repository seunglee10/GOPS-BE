from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.simulator_gateway import SimulatorGateway, SimulatorTimeout  # noqa: E402


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b'{"candles": []}'


class SimulatorGatewayDiagnosticsTest(unittest.TestCase):
    def test_slow_request_log_contains_only_bounded_request_metadata(self):
        gateway = SimulatorGateway("http://simulator.test")

        with (
            patch("app.services.simulator_gateway.urllib.request.urlopen", return_value=FakeResponse()),
            patch("app.services.simulator_gateway.time.monotonic", side_effect=[10.0, 11.2]),
            self.assertLogs("app.services.simulator_gateway", level="WARNING") as logs,
        ):
            gateway.candles("NVDA", "1D", 500)

        message = logs.output[0]
        self.assertIn("path=/api/control/candles", message)
        self.assertIn("durationMs=1200", message)
        self.assertIn("outcome=success", message)
        self.assertIn("symbol=NVDA", message)
        self.assertIn("interval=1D", message)
        self.assertNotIn("limit=500", message)

    def test_timeout_is_logged_even_before_slow_request_threshold(self):
        gateway = SimulatorGateway("http://simulator.test")

        with (
            patch("app.services.simulator_gateway.urllib.request.urlopen", side_effect=TimeoutError("timed out")),
            patch("app.services.simulator_gateway.time.monotonic", side_effect=[10.0, 10.1]),
            self.assertLogs("app.services.simulator_gateway", level="WARNING") as logs,
        ):
            with self.assertRaises(SimulatorTimeout):
                gateway.candles("NVDA", "1m", 500)

        self.assertIn("durationMs=100", logs.output[0])
        self.assertIn("outcome=timeout", logs.output[0])


if __name__ == "__main__":
    unittest.main()
