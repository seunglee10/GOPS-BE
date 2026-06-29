import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
ORDER_SHARED = ROOT / "systems" / "order" / "shared"
AGENT_SHARED = ROOT / "systems" / "agent-orchestration" / "shared"
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend"
for path in (str(MARKET_SHARED), str(ORDER_SHARED), str(AGENT_SHARED), str(BACKEND), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

try:
    from app.services.agent_alert_payloads import parse_pubsub_payload

    AGENT_ROUTE_HELPERS_AVAILABLE = True
except Exception:
    parse_pubsub_payload = None
    AGENT_ROUTE_HELPERS_AVAILABLE = False

try:
    from fastapi.testclient import TestClient

    from app.main import create_app

    FASTAPI_TESTCLIENT_AVAILABLE = True
except Exception:
    TestClient = None
    FASTAPI_TESTCLIENT_AVAILABLE = False


@unittest.skipUnless(FASTAPI_TESTCLIENT_AVAILABLE, "FastAPI TestClient is not available")
class AgentRoutesTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app())

    def test_analyze_agents_delegates_to_orchestrator_gateway(self):
        expected = {"analysisId": "analysis-1", "symbol": "NVDA", "status": "completed"}
        with patch("app.routes.agents.request_agent_analysis", return_value=expected) as gateway:
            response = self.client.post("/api/agents/analyze", json={"symbol": "NVDA", "intent": "analysis"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        self.assertEqual(gateway.call_args.args[0]["symbol"], "NVDA")
        self.assertEqual(gateway.call_args.args[0]["routerMode"], "hybrid")

    def test_agent_report_delegates_to_gateway(self):
        expected = {"analysisId": "analysis-1", "status": "completed"}
        with patch("app.routes.agents.get_agent_report", return_value=expected):
            response = self.client.get("/api/agents/reports/analysis-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)

class AgentRouteHelperTest(unittest.TestCase):
    @unittest.skipUnless(AGENT_ROUTE_HELPERS_AVAILABLE, "agent routes module is not importable")
    def test_parse_pubsub_payload_accepts_json_string(self):
        self.assertEqual(parse_pubsub_payload('{"type":"AGENT_ALERT"}')["type"], "AGENT_ALERT")

    @unittest.skipUnless(AGENT_ROUTE_HELPERS_AVAILABLE, "agent routes module is not importable")
    def test_parse_pubsub_payload_wraps_plain_text(self):
        payload = parse_pubsub_payload("not-json")
        self.assertEqual(payload["type"], "AGENT_ALERT")
        self.assertEqual(payload["raw"], "not-json")


if __name__ == "__main__":
    unittest.main()
