import json
import os
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
    from app.routes.agents import is_chart_symbol_supported, parse_report_update_payload, resolve_agent_entity_for_chart_shortcut

    AGENT_ROUTE_HELPERS_AVAILABLE = True
except Exception:
    parse_pubsub_payload = None
    agent_gateway = None
    is_chart_symbol_supported = None
    parse_report_update_payload = None
    resolve_agent_entity_for_chart_shortcut = None
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

    def test_api_health_alias_returns_backend_health(self):
        response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "service": "gops-backend"})

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
        self.assertEqual(gateway.call_args.kwargs["user_id"], "dev-auth-disabled")

    def test_analyze_agents_preserves_interactive_context_fields(self):
        expected = {"analysisId": "analysis-1", "symbol": "NVDA", "status": "completed"}
        reference = {
            "type": "chart.candle",
            "data": {"symbol": "NVDA", "interval": "1D", "timestamp": "2026-07-04T00:00:00Z", "close": 145.5},
        }
        with patch("app.routes.agents.request_agent_analysis", return_value=expected) as gateway:
            response = self.client.post(
                "/api/agents/analyze",
                json={
                    "symbol": "NVDA",
                    "intent": "여기 왜 내려갔어?",
                    "references": [reference],
                    "uiContext": {"activePanelType": "chart"},
                    "futureContext": {"keep": True},
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = gateway.call_args.args[0]
        self.assertEqual(payload["references"], [reference])
        self.assertEqual(payload["uiContext"], {"activePanelType": "chart"})
        self.assertEqual(payload["futureContext"], {"keep": True})

    def test_analyze_agents_uses_gateway_status_code_marker(self):
        expected = {"_status_code": 202, "request_id": "agent-request-1", "status": "queued"}
        with patch("app.routes.agents.request_agent_analysis", return_value=expected):
            response = self.client.post("/api/agents/analyze", json={"symbol": "NVDA", "intent": "analysis"})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"request_id": "agent-request-1", "status": "queued"})

    def test_layout_resolve_delegates_to_orchestrator_gateway(self):
        expected = {
            "status": "ui_layout",
            "summary": "변경했습니다.",
            "layoutProposal": {"commands": []},
        }
        with patch("app.routes.agents.request_agent_layout_resolution", return_value=expected) as gateway:
            response = self.client.post(
                "/api/agents/layout/resolve",
                json={"symbol": "NVDA", "intent": "온톨로지 패널 키워줘", "layoutContext": {"panels": []}},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        self.assertEqual(gateway.call_args.args[0]["symbol"], "NVDA")
        self.assertEqual(gateway.call_args.args[0]["intent"], "온톨로지 패널 키워줘")

    def test_agent_report_delegates_to_gateway(self):
        expected = {"analysisId": "analysis-1", "status": "completed"}
        with patch("app.routes.agents.get_agent_report", return_value=expected):
            response = self.client.get("/api/agents/reports/analysis-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)

    def test_cancel_agent_report_delegates_to_gateway(self):
        expected = {"analysisId": "analysis-1", "status": "canceled", "cancelAccepted": True}
        with patch("app.routes.agents.cancel_agent_analysis", return_value=expected) as gateway:
            response = self.client.post(
                "/api/agents/reports/analysis-1/cancel",
                headers={"X-GOPS-User-Id": "user-1"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        self.assertEqual(gateway.call_args.args[0], "analysis-1")
        self.assertEqual(gateway.call_args.kwargs["user_id"], "dev-auth-disabled")

    def test_resolve_agent_entity_returns_chart_shortcut(self):
        response = self.client.get("/api/agents/entities/resolve", params={"q": "엔비디아", "mode": "chartShortcut"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["chartShortcut"], True)
        self.assertEqual(response.json()["symbol"], "NVDA")


@unittest.skipUnless(FASTAPI_TESTCLIENT_AVAILABLE, "FastAPI TestClient is not available")
class AgentAuthenticatedRoutesTest(unittest.TestCase):
    def setUp(self):
        from app.auth.config import AuthConfig
        from app.auth.session_store import MemorySessionStore

        self.env = patch.dict(os.environ, {
            "AUTH_ENABLED": "true",
            "AUTH_SESSION_SECRET": "test-session-secret",
            "AGENT_RATE_LIMIT_ENABLED": "false",
        }, clear=False)
        self.env.start()
        self.app = create_app()
        self.config = AuthConfig.from_env()
        self.store = MemorySessionStore(self.config)
        self.app.state.auth_session_store = self.store
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.env.stop()

    def authenticate(self, sub: str = "trusted-user") -> None:
        from app.auth.models import AuthenticatedUser

        session_id = self.store.create_session(AuthenticatedUser(sub, f"{sub}@example.com", True))
        self.client.cookies.set(self.config.session_cookie_name, session_id)

    def test_every_agent_http_route_requires_session(self):
        with (
            patch("app.routes.agents.request_agent_analysis", return_value={"status": "queued"}),
            patch("app.routes.agents.request_agent_layout_resolution", return_value={"status": "not_ui"}),
            patch("app.routes.agents.get_agent_report", return_value={"status": "completed"}),
            patch("app.routes.agents.cancel_agent_analysis", return_value={"status": "canceled"}),
        ):
            responses = [
                self.client.post("/api/agents/analyze", json={"symbol": "NVDA", "intent": "analysis"}),
                self.client.post("/api/agents/layout/resolve", json={"symbol": "NVDA", "intent": "패널 추가"}),
                self.client.get("/api/agents/entities/resolve", params={"q": "NVDA"}),
                self.client.get("/api/agents/reports/analysis-1"),
                self.client.post("/api/agents/reports/analysis-1/cancel"),
                self.client.get("/api/agents/reports/analysis-1/stream"),
            ]

        self.assertEqual([response.status_code for response in responses], [401] * len(responses))

    def test_analyze_uses_session_identity_and_removes_client_identity_controls(self):
        self.authenticate("trusted-user")
        expected = {"analysisId": "analysis-1", "status": "queued"}
        with patch("app.routes.agents.request_agent_analysis", return_value=expected) as gateway:
            response = self.client.post(
                "/api/agents/analyze",
                headers={"X-GOPS-User-Id": "header-attacker", "Idempotency-Key": "idem-1"},
                json={
                    "symbol": "NVDA",
                    "intent": "analysis",
                    "userId": "body-attacker",
                    "maxLlmCalls": 999,
                    "llmBudgetOwner": "user",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(gateway.call_args.kwargs["user_id"], "trusted-user")
        payload = gateway.call_args.args[0]
        self.assertNotIn("userId", payload)
        self.assertNotIn("maxLlmCalls", payload)
        self.assertNotIn("llmBudgetOwner", payload)

    def test_report_and_cancel_pass_session_owner_to_gateway(self):
        self.authenticate("trusted-user")
        with patch("app.routes.agents.get_agent_report", return_value={"analysisId": "analysis-1", "status": "completed"}) as get_report:
            response = self.client.get("/api/agents/reports/analysis-1")
        with patch("app.routes.agents.cancel_agent_analysis", return_value={"analysisId": "analysis-1", "status": "canceled"}) as cancel:
            cancel_response = self.client.post(
                "/api/agents/reports/analysis-1/cancel",
                headers={"X-GOPS-User-Id": "header-attacker"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(cancel_response.status_code, 200)
        self.assertEqual(get_report.call_args.kwargs["user_id"], "trusted-user")
        self.assertEqual(cancel.call_args.kwargs["user_id"], "trusted-user")

    def test_agent_request_rejects_oversized_extensible_context(self):
        self.authenticate()
        with patch("app.routes.agents.request_agent_analysis", return_value={"status": "queued"}) as gateway:
            response = self.client.post(
                "/api/agents/analyze",
                json={"symbol": "NVDA", "intent": "analysis", "futureContext": {"blob": "x" * 70000}},
            )

        self.assertEqual(response.status_code, 413)
        gateway.assert_not_called()

    def test_layout_resolution_uses_authenticated_user_rate_limit(self):
        self.authenticate("trusted-user")
        with (
            patch("app.routes.agents.enforce_agent_rate_limit") as rate_limit,
            patch("app.routes.agents.request_agent_layout_resolution", return_value={"status": "ui_layout"}),
        ):
            response = self.client.post(
                "/api/agents/layout/resolve",
                json={"symbol": "NVDA", "intent": "차트를 크게 보여줘"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(rate_limit.call_args.args[1], "trusted-user")

    def test_agent_alert_websocket_requires_session(self):
        with self.client.websocket_connect("/ws/agent-alerts") as websocket:
            message = websocket.receive_json()

        self.assertEqual(message, {"type": "error", "detail": "authentication required"})

class AgentRouteHelperTest(unittest.TestCase):
    @unittest.skipUnless(AGENT_ROUTE_HELPERS_AVAILABLE, "agent routes module is not importable")
    def test_agent_rate_limiter_defaults_to_thirty_requests_per_minute(self):
        from types import SimpleNamespace

        from fastapi import HTTPException
        from app.services import agent_rate_limit

        redis = FakeRateLimitRedis()
        app = SimpleNamespace(state=SimpleNamespace(agent_rate_limit_redis=redis))
        with (
            patch.object(agent_rate_limit, "read_dotenv_value", return_value=None),
            patch.dict(os.environ, {"AGENT_RATE_LIMIT_ENABLED": "true"}, clear=True),
        ):
            for _ in range(30):
                agent_rate_limit.enforce_agent_rate_limit(app, "user-1", now=120.0)
            with self.assertRaises(HTTPException) as raised:
                agent_rate_limit.enforce_agent_rate_limit(app, "user-1", now=120.0)

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.headers["Retry-After"], "60")

    @unittest.skipUnless(AGENT_ROUTE_HELPERS_AVAILABLE, "agent routes module is not importable")
    def test_agent_rate_limiter_rejects_requests_above_user_limit(self):
        from types import SimpleNamespace

        from fastapi import HTTPException
        from app.services.agent_rate_limit import enforce_agent_rate_limit

        redis = FakeRateLimitRedis()
        app = SimpleNamespace(state=SimpleNamespace(agent_rate_limit_redis=redis))
        with patch.dict(os.environ, {
            "AGENT_RATE_LIMIT_ENABLED": "true",
            "AGENT_RATE_LIMIT_REQUESTS": "2",
            "AGENT_RATE_LIMIT_WINDOW_SECONDS": "60",
        }, clear=False):
            enforce_agent_rate_limit(app, "user-1", now=120.0)
            enforce_agent_rate_limit(app, "user-1", now=120.0)
            with self.assertRaises(HTTPException) as raised:
                enforce_agent_rate_limit(app, "user-1", now=120.0)

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.headers["Retry-After"], "60")

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

    @unittest.skipUnless(AGENT_ROUTE_HELPERS_AVAILABLE, "agent routes module is not importable")
    def test_agent_entity_resolve_confirms_bare_company_names(self):
        for query in ("엔비디아", "nvidia", "nvidia corp", "NVIDIA Corporation", "NVDA"):
            with self.subTest(query=query):
                payload = resolve_agent_entity_for_chart_shortcut(query)
                self.assertEqual(payload["status"], "confirmed")
                self.assertEqual(payload["chartShortcut"], True)
                self.assertEqual(payload["symbol"], "NVDA")

        for query in ("마이크론", "마이크론 보여줘", "micron", "Micron Technology", "MU"):
            with self.subTest(query=query):
                payload = resolve_agent_entity_for_chart_shortcut(query)
                self.assertEqual(payload["status"], "confirmed")
                self.assertEqual(payload["chartShortcut"], True)
                self.assertEqual(payload["chartAction"], "replace")
                self.assertEqual(payload["symbol"], "MU")

    @unittest.skipUnless(AGENT_ROUTE_HELPERS_AVAILABLE, "agent routes module is not importable")
    def test_agent_entity_resolve_confirms_chart_open_commands(self):
        for query in ("애플차트", "애플 차트 보여줘", "AAPL chart", "show AAPL chart"):
            with self.subTest(query=query):
                payload = resolve_agent_entity_for_chart_shortcut(query)
                self.assertEqual(payload["status"], "confirmed")
                self.assertEqual(payload["chartShortcut"], True)
                self.assertEqual(payload["chartAction"], "replace")
                self.assertEqual(payload["symbol"], "AAPL")

        for query in ("마이크론 차트 보여줘", "micron chart", "MU chart"):
            with self.subTest(query=query):
                payload = resolve_agent_entity_for_chart_shortcut(query)
                self.assertEqual(payload["status"], "confirmed")
                self.assertEqual(payload["chartShortcut"], True)
                self.assertEqual(payload["chartAction"], "replace")
                self.assertEqual(payload["symbol"], "MU")

    @unittest.skipUnless(AGENT_ROUTE_HELPERS_AVAILABLE, "agent routes module is not importable")
    def test_agent_entity_resolve_confirms_chart_add_commands(self):
        for query in ("애플 차트 추가해줘", "애플도 같이 보여줘", "애플 차트랑 같이 보여줘", "AAPL chart too", "애플 비교 차트"):
            with self.subTest(query=query):
                payload = resolve_agent_entity_for_chart_shortcut(query)
                self.assertEqual(payload["status"], "confirmed")
                self.assertEqual(payload["chartShortcut"], True)
                self.assertEqual(payload["chartAction"], "add")
                self.assertEqual(payload["symbol"], "AAPL")
                self.assertEqual(payload["symbols"], ["AAPL"])

        bottom = resolve_agent_entity_for_chart_shortcut("애플 차트 밑에 추가해줘")
        self.assertEqual(bottom["chartAction"], "add")
        self.assertEqual(bottom["chartPlacementIntent"], "bottom")

    @unittest.skipUnless(AGENT_ROUTE_HELPERS_AVAILABLE, "agent routes module is not importable")
    def test_agent_entity_resolve_confirms_multi_chart_commands(self):
        payload = resolve_agent_entity_for_chart_shortcut("애플 차트 엔비디아 차트 같이 보여줘")

        self.assertEqual(payload["status"], "confirmed")
        self.assertEqual(payload["chartShortcut"], True)
        self.assertEqual(payload["chartAction"], "add")
        self.assertEqual(payload["symbol"], "AAPL")
        self.assertEqual(payload["symbols"], ["AAPL", "NVDA"])

    @unittest.skipUnless(AGENT_ROUTE_HELPERS_AVAILABLE, "agent routes module is not importable")
    def test_agent_entity_resolve_rejects_analysis_chart_shortcuts(self):
        for query in (
            "엔비디아 뉴스",
            "엔비디아 분석해줘",
            "엔비디아 차트 분석해줘",
            "엔비디아 왜 올랐어",
            "엔비디아랑 AMD 관계",
            "NVDA랑 AAPL 재무 비교해줘",
            "NVDA AAPL 매출 비교해줘",
            "NVDA AAPL fundamentals compare",
            "반도체",
        ):
            with self.subTest(query=query):
                payload = resolve_agent_entity_for_chart_shortcut(query)
                self.assertEqual(payload["chartShortcut"], False)
                self.assertEqual(payload["chartAction"], "none")

        unsupported = resolve_agent_entity_for_chart_shortcut("엔비디아", mode="analysis")
        self.assertEqual(unsupported["status"], "unsupported")
        self.assertEqual(unsupported["chartShortcut"], False)

    @unittest.skipUnless(AGENT_ROUTE_HELPERS_AVAILABLE, "agent routes module is not importable")
    def test_agent_entity_resolve_requires_chart_registry_support(self):
        is_chart_symbol_supported.cache_clear()
        with patch("app.routes.agents.sp500_universe_symbols", return_value=["NVDA"]):
            with patch("app.routes.agents.get_market_data_provider") as provider_factory:
                provider_factory.return_value.symbol_detail.side_effect = LookupError("unsupported")

                payload = resolve_agent_entity_for_chart_shortcut("어도비")

        is_chart_symbol_supported.cache_clear()
        self.assertEqual(payload["status"], "confirmed")
        self.assertEqual(payload["symbol"], "ADBE")
        self.assertEqual(payload["chartShortcut"], False)

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
    def test_agent_gateway_layout_resolution_uses_orchestrator_fast_path(self):
        with patch("app.services.agent_gateway.request_orchestrator_json", return_value={"status": "not_ui"}) as request_json:
            response = agent_gateway.request_agent_layout_resolution({"symbol": "NVDA", "intent": "analysis"})

        self.assertEqual(response, {"status": "not_ui"})
        self.assertEqual(request_json.call_args.args, ("POST", "/layout/resolve", {"symbol": "NVDA", "intent": "analysis"}))

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
        from gops_agents.runtime.report_store import InMemoryReportStore

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
        self.assertTrue(store.is_owner(request_id, "user-1"))

    @unittest.skipUnless(AGENT_ROUTE_HELPERS_AVAILABLE, "agent gateway module is not importable")
    def test_agent_gateway_rejects_report_access_for_non_owner(self):
        from fastapi import HTTPException
        from gops_agents.contracts import AnalysisReport, utc_now_iso
        from gops_agents.runtime.report_store import InMemoryReportStore

        store = InMemoryReportStore()
        store.save(AnalysisReport(
            analysisId="agent-request-owned",
            symbol="NVDA",
            intent="analysis",
            status="completed",
            createdAt=utc_now_iso(),
            summary="done",
            rationale="done",
        ))
        store.save_owner_mapping("user-1", "agent-request-owned")

        with patch("app.services.agent_gateway.build_report_store_from_env", return_value=store):
            with patch("app.services.agent_gateway.read_dotenv_value", side_effect=lambda name: {
                "AGENT_SHARED_REPORT_STORE_ENABLED": "true",
            }.get(name)):
                with self.assertRaises(HTTPException) as raised:
                    agent_gateway.get_agent_report("agent-request-owned", user_id="user-2")

        self.assertEqual(raised.exception.status_code, 404)

    @unittest.skipUnless(AGENT_ROUTE_HELPERS_AVAILABLE, "agent gateway module is not importable")
    def test_agent_gateway_rejects_cancel_for_non_owner(self):
        from fastapi import HTTPException
        from gops_agents.runtime.report_store import InMemoryReportStore

        store = InMemoryReportStore()
        store.save_owner_mapping("user-1", "agent-request-owned")
        with patch("app.services.agent_gateway.build_report_store_from_env", return_value=store):
            with patch("app.services.agent_gateway.read_dotenv_value", side_effect=lambda name: {
                "AGENT_ASYNC_ANALYSIS_ENABLED": "true",
            }.get(name)):
                with self.assertRaises(HTTPException) as raised:
                    agent_gateway.cancel_agent_analysis("agent-request-owned", user_id="user-2")

        self.assertEqual(raised.exception.status_code, 404)
        self.assertFalse(store.is_canceled("agent-request-owned"))

    @unittest.skipUnless(AGENT_ROUTE_HELPERS_AVAILABLE, "agent gateway module is not importable")
    def test_agent_gateway_async_submit_reuses_idempotent_report(self):
        from gops_agents.runtime.report_store import InMemoryReportStore

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
    def test_agent_gateway_async_cancel_marks_report_canceled(self):
        from gops_agents.runtime.report_store import InMemoryReportStore

        store = InMemoryReportStore()
        store.save_owner_mapping("user-1", "agent-request-cancel")
        with patch(
            "app.services.agent_gateway.read_dotenv_value",
            side_effect=lambda name: {"AGENT_ASYNC_ANALYSIS_ENABLED": "true"}.get(name),
        ):
            with patch("app.services.agent_gateway.build_report_store_from_env", return_value=store):
                with patch("app.services.agent_gateway.publish_report_update") as publish:
                    response = agent_gateway.cancel_agent_analysis("agent-request-cancel", user_id="user-1")

        self.assertEqual(response["analysisId"], "agent-request-cancel")
        self.assertEqual(response["status"], "canceled")
        self.assertEqual(response["cancelAccepted"], True)
        self.assertTrue(store.is_canceled("agent-request-cancel"))
        self.assertEqual(publish.call_args.args[0]["status"], "canceled")

    @unittest.skipUnless(AGENT_ROUTE_HELPERS_AVAILABLE, "agent gateway module is not importable")
    def test_agent_gateway_submit_skips_queue_when_request_was_already_canceled(self):
        from gops_agents.runtime.report_store import InMemoryReportStore

        store = InMemoryReportStore()
        store.mark_canceled("agent-request-pre-canceled", reason="user stopped", user_id="user-1")
        queue = FakeAnalysisQueue()
        with patch(
            "app.services.agent_gateway.read_dotenv_value",
            side_effect=lambda name: {"AGENT_ASYNC_ANALYSIS_ENABLED": "true"}.get(name),
        ):
            with patch("app.services.agent_gateway.build_report_store_from_env", return_value=store):
                with patch("app.services.agent_gateway.build_analysis_request_queue_from_env", return_value=queue):
                    response = agent_gateway.request_agent_analysis(
                        {"symbol": "NVDA", "intent": "analysis", "requestId": "agent-request-pre-canceled"},
                        user_id="user-1",
                    )

        self.assertEqual(response["_status_code"], 200)
        self.assertEqual(response["status"], "canceled")
        self.assertEqual(len(queue.envelopes), 0)

    @unittest.skipUnless(AGENT_ROUTE_HELPERS_AVAILABLE, "agent gateway module is not importable")
    def test_agent_gateway_cancel_does_not_overwrite_completed_report(self):
        from gops_agents.contracts import AnalysisReport, utc_now_iso
        from gops_agents.runtime.report_store import InMemoryReportStore

        store = InMemoryReportStore()
        store.save(AnalysisReport(
            analysisId="agent-request-completed",
            symbol="NVDA",
            intent="analysis",
            status="completed",
            createdAt=utc_now_iso(),
            summary="done",
            rationale="test",
        ))
        store.save_owner_mapping("user-1", "agent-request-completed")
        with patch(
            "app.services.agent_gateway.read_dotenv_value",
            side_effect=lambda name: {"AGENT_ASYNC_ANALYSIS_ENABLED": "true"}.get(name),
        ):
            with patch("app.services.agent_gateway.build_report_store_from_env", return_value=store):
                response = agent_gateway.cancel_agent_analysis("agent-request-completed", user_id="user-1")

        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["cancelAccepted"], False)
        self.assertFalse(store.is_canceled("agent-request-completed"))

    @unittest.skipUnless(AGENT_ROUTE_HELPERS_AVAILABLE, "agent gateway module is not importable")
    def test_agent_gateway_admission_rejects_when_queue_backpressure_hits(self):
        from gops_agents.runtime.report_store import InMemoryReportStore

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
        from gops_agents.runtime.report_store import InMemoryReportStore

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

    @unittest.skipUnless(AGENT_ROUTE_HELPERS_AVAILABLE, "agent gateway module is not importable")
    def test_agent_gateway_returns_canceled_report_as_terminal(self):
        from gops_agents.contracts import AnalysisReport, utc_now_iso

        report = AnalysisReport(
            analysisId="agent-request-canceled",
            symbol="NVDA",
            intent="analysis",
            status="canceled",
            createdAt=utc_now_iso(),
            summary="canceled",
            rationale="test",
        )

        response = agent_gateway.response_for_report(report)

        self.assertEqual(response["_status_code"], 200)
        self.assertEqual(response["status"], "canceled")


class AgentDockerImageContractTest(unittest.TestCase):
    def test_gops_backend_image_includes_agent_entity_alias_catalog(self):
        dockerfile = (ROOT / "infra" / "docker" / "Dockerfile.gops-backend").read_text(encoding="utf-8")

        self.assertIn(
            "COPY systems/agent-orchestration/config ./systems/agent-orchestration/config",
            dockerfile,
        )


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
        from gops_agents.runtime.queues import AnalysisQueueMetrics

        return AnalysisQueueMetrics(backend="fake", queue_depth=self.queue_depth, consumer_lag=0)


class FakeRateLimitRedis:
    def __init__(self):
        self.values = {}
        self.expirations = {}

    def incr(self, key):
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    def expire(self, key, seconds):
        self.expirations[key] = seconds


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
