import json
import sys
import unittest
import urllib.error
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
    from app.services import agent_gateway
    from app.routes.agents import parse_report_update_payload

    AGENT_ROUTE_HELPERS_AVAILABLE = True
except Exception:
    parse_pubsub_payload = None
    agent_gateway = None
    parse_report_update_payload = None
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
            response = self.client.post(
                "/api/agents/analyze",
                json={"symbol": "NVDA", "intent": "analysis"},
                headers={"Idempotency-Key": "idem-1", "X-GOPS-User-Id": "user-1"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        self.assertEqual(gateway.call_args.args[0]["symbol"], "NVDA")
        self.assertEqual(gateway.call_args.args[0]["routerMode"], "hybrid")
        self.assertEqual(gateway.call_args.kwargs["idempotency_key"], "idem-1")
        self.assertEqual(gateway.call_args.kwargs["user_id"], "user-1")

    def test_analyze_agents_uses_gateway_status_code_marker(self):
        expected = {"_status_code": 202, "request_id": "agent-request-1", "status": "queued"}
        with patch("app.routes.agents.request_agent_analysis", return_value=expected):
            response = self.client.post("/api/agents/analyze", json={"symbol": "NVDA", "intent": "analysis"})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"request_id": "agent-request-1", "status": "queued"})

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

    @unittest.skipUnless(AGENT_ROUTE_HELPERS_AVAILABLE, "agent routes module is not importable")
    def test_parse_report_update_payload_accepts_delivery_gateway_json(self):
        payload = parse_report_update_payload('{"analysisId":"agent-request-1","status":"deep_completed"}')
        self.assertEqual(payload["analysisId"], "agent-request-1")
        self.assertEqual(payload["status"], "deep_completed")

    @unittest.skipUnless(AGENT_ROUTE_HELPERS_AVAILABLE, "agent gateway module is not importable")
    def test_agent_gateway_uses_configured_orchestrator_timeout(self):
        with patch(
            "app.services.agent_gateway.read_dotenv_value",
            side_effect=lambda name: {
                "AGENT_ORCHESTRATOR_URL": "http://agent-orchestrator:8100",
                "AGENT_ORCHESTRATOR_TIMEOUT_SECONDS": "45",
            }.get(name),
        ):
            with patch("urllib.request.urlopen", return_value=FakeJsonResponse({"status": "ok"})) as urlopen:
                response = agent_gateway.request_orchestrator_json("POST", "/analyze", {"symbol": "DDOG"})

        self.assertEqual(response, {"status": "ok"})
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 45.0)

    @unittest.skipUnless(AGENT_ROUTE_HELPERS_AVAILABLE, "agent gateway module is not importable")
    def test_agent_gateway_maps_upstream_timeout_to_504(self):
        with patch("app.services.agent_gateway.read_dotenv_value", return_value=None):
            with patch("urllib.request.urlopen", side_effect=urllib.error.URLError(TimeoutError("timed out"))):
                with self.assertRaises(Exception) as raised:
                    agent_gateway.request_orchestrator_json("POST", "/analyze", {"symbol": "DDOG"})

        self.assertEqual(getattr(raised.exception, "status_code", None), 504)
        self.assertEqual(getattr(raised.exception, "detail", None), "Agent orchestrator request timed out.")

    @unittest.skipUnless(AGENT_ROUTE_HELPERS_AVAILABLE, "agent gateway module is not importable")
    def test_agent_gateway_async_submit_enqueues_and_stores_queued_report(self):
        from gops_agents.report_store import InMemoryReportStore

        store = InMemoryReportStore()
        queue = FakeAnalysisQueue()
        with patch(
            "app.services.agent_gateway.read_dotenv_value",
            side_effect=lambda name: {"AGENT_ASYNC_ANALYSIS_ENABLED": "true"}.get(name),
        ):
            with patch("app.services.agent_gateway.build_report_store_from_env", return_value=store):
                with patch("app.services.agent_gateway.build_analysis_request_queue_from_env", return_value=queue):
                    response = agent_gateway.request_agent_analysis(
                        {"symbol": "NVDA", "intent": "analysis"},
                        idempotency_key="idem-1",
                        user_id="user-1",
                    )

        self.assertEqual(response["_status_code"], 202)
        self.assertEqual(response["status"], "queued")
        self.assertEqual(len(queue.envelopes), 1)
        request_id = response["request_id"]
        self.assertEqual(store.get(request_id).status, "queued")
        self.assertEqual(store.get_idempotency_request_id("user-1", "idem-1"), request_id)

    @unittest.skipUnless(AGENT_ROUTE_HELPERS_AVAILABLE, "agent gateway module is not importable")
    def test_agent_gateway_async_submit_reuses_idempotent_report(self):
        from gops_agents.report_store import InMemoryReportStore

        store = InMemoryReportStore()
        queue = FakeAnalysisQueue()
        env_patch = patch(
            "app.services.agent_gateway.read_dotenv_value",
            side_effect=lambda name: {"AGENT_ASYNC_ANALYSIS_ENABLED": "true"}.get(name),
        )
        store_patch = patch("app.services.agent_gateway.build_report_store_from_env", return_value=store)
        queue_patch = patch("app.services.agent_gateway.build_analysis_request_queue_from_env", return_value=queue)
        with env_patch, store_patch, queue_patch:
            first = agent_gateway.request_agent_analysis({"symbol": "NVDA", "intent": "analysis"}, idempotency_key="idem-1", user_id="user-1")
            second = agent_gateway.request_agent_analysis({"symbol": "NVDA", "intent": "analysis"}, idempotency_key="idem-1", user_id="user-1")

        self.assertEqual(first["request_id"], second["request_id"])
        self.assertEqual(len(queue.envelopes), 1)

    @unittest.skipUnless(AGENT_ROUTE_HELPERS_AVAILABLE, "agent gateway module is not importable")
    def test_agent_gateway_admission_rejects_when_queue_backpressure_hits(self):
        from gops_agents.report_store import InMemoryReportStore

        store = InMemoryReportStore()
        queue = FakeAnalysisQueue(queue_depth=1)
        with patch(
            "app.services.agent_gateway.read_dotenv_value",
            side_effect=lambda name: {
                "AGENT_ASYNC_ANALYSIS_ENABLED": "true",
                "AGENT_ADMISSION_MAX_QUEUE_DEPTH": "1",
            }.get(name),
        ):
            with patch("app.services.agent_gateway.build_report_store_from_env", return_value=store):
                with patch("app.services.agent_gateway.build_analysis_request_queue_from_env", return_value=queue):
                    with self.assertRaises(Exception) as raised:
                        agent_gateway.request_agent_analysis({"symbol": "NVDA", "intent": "analysis"}, user_id="user-1")

        self.assertEqual(getattr(raised.exception, "status_code", None), 429)
        self.assertEqual(getattr(raised.exception, "detail", None), "analysis_queue_backpressure")
        self.assertEqual(len(queue.envelopes), 0)

    @unittest.skipUnless(AGENT_ROUTE_HELPERS_AVAILABLE, "agent gateway module is not importable")
    def test_agent_gateway_sync_compat_wait_returns_completed_store_report(self):
        from gops_agents.contracts import AnalysisReport, utc_now_iso
        from gops_agents.report_store import InMemoryReportStore

        store = InMemoryReportStore()
        queue = CompletingFakeAnalysisQueue(store)
        with patch(
            "app.services.agent_gateway.read_dotenv_value",
            side_effect=lambda name: {
                "AGENT_ASYNC_ANALYSIS_ENABLED": "true",
                "AGENT_SYNC_COMPAT_WAIT_ENABLED": "true",
                "AGENT_SYNC_COMPAT_WAIT_TIMEOUT_SECONDS": "1",
                "AGENT_SYNC_COMPAT_WAIT_POLL_SECONDS": "0.01",
            }.get(name),
        ):
            with patch("app.services.agent_gateway.build_report_store_from_env", return_value=store):
                with patch("app.services.agent_gateway.build_analysis_request_queue_from_env", return_value=queue):
                    with patch("gops_agents.contracts.utc_now_iso", return_value=utc_now_iso()):
                        response = agent_gateway.request_agent_analysis({"symbol": "NVDA", "intent": "analysis"}, user_id="user-1")

        self.assertEqual(response["_status_code"], 200)
        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["symbol"], "NVDA")

    @unittest.skipUnless(AGENT_ROUTE_HELPERS_AVAILABLE, "agent gateway module is not importable")
    def test_agent_gateway_returns_deep_completed_report_as_terminal(self):
        from gops_agents.contracts import AnalysisReport, utc_now_iso

        report = AnalysisReport(
            analysisId="agent-request-deep",
            symbol="NVDA",
            intent="analysis",
            status="deep_completed",
            createdAt=utc_now_iso(),
            summary="deep done",
            rationale="test",
        )

        response = agent_gateway.response_for_report(report)

        self.assertEqual(response["_status_code"], 200)
        self.assertEqual(response["status"], "deep_completed")


class FakeJsonResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakeAnalysisQueue:
    def __init__(self, queue_depth=0):
        self.envelopes = []
        self.queue_depth = queue_depth

    def submit(self, envelope):
        self.envelopes.append(envelope)

    def metrics(self):
        from gops_agents.analysis_queue import AnalysisQueueMetrics

        return AnalysisQueueMetrics(backend="fake", queue_depth=self.queue_depth, consumer_lag=0)


class CompletingFakeAnalysisQueue(FakeAnalysisQueue):
    def __init__(self, store):
        super().__init__()
        self.store = store

    def submit(self, envelope):
        from gops_agents.contracts import AnalysisReport, utc_now_iso

        super().submit(envelope)
        self.store.save(AnalysisReport(
            analysisId=envelope.request_id,
            symbol=str(envelope.payload.get("symbol") or "UNKNOWN"),
            intent=str(envelope.payload.get("intent") or "analysis"),
            status="completed",
            createdAt=utc_now_iso(),
            summary="completed",
            rationale="test",
        ))


if __name__ == "__main__":
    unittest.main()
