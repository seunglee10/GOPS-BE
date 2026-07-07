import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
ORDER_SHARED = ROOT / "systems" / "order" / "shared"
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend"
for path in (str(MARKET_SHARED), str(ORDER_SHARED), str(BACKEND), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

try:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.services.chart_presets_repository import InMemoryChartPresetsRepository

    FASTAPI_TESTCLIENT_AVAILABLE = True
except Exception:
    TestClient = None
    FASTAPI_TESTCLIENT_AVAILABLE = False


@unittest.skipUnless(FASTAPI_TESTCLIENT_AVAILABLE, "FastAPI TestClient is not available")
class ChartPresetsRoutesTest(unittest.TestCase):
    def setUp(self):
        os.environ["AUTH_ENABLED"] = "false"
        self.repository = InMemoryChartPresetsRepository()
        self.app = create_app()
        self.app.state.chart_presets_repository = self.repository
        self.client = TestClient(self.app)

    def test_list_is_empty_by_default(self):
        response = self.client.get("/api/charts/presets")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"presets": []})

    def test_replace_then_list_round_trips(self):
        preset = {"id": "market", "name": "내 시장", "layout": {"version": 1, "slots": []}}
        put = self.client.put("/api/charts/presets", json={"presets": [preset]})
        self.assertEqual(put.status_code, 200)
        self.assertEqual(put.json(), {"presets": [preset]})

        listed = self.client.get("/api/charts/presets")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["presets"], [preset])

    def test_replace_overwrites_previous_list(self):
        first = {"id": "a", "name": "A", "layout": {}}
        second = {"id": "b", "name": "B", "layout": {}}
        self.client.put("/api/charts/presets", json={"presets": [first]})
        self.client.put("/api/charts/presets", json={"presets": [second]})
        listed = self.client.get("/api/charts/presets")
        self.assertEqual(listed.json()["presets"], [second])


if __name__ == "__main__":
    unittest.main()
