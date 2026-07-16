import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
ORDER_SHARED = ROOT / "systems" / "order" / "shared"
AGENT_SHARED = ROOT / "systems" / "agent-orchestration" / "shared"
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend"
for path in (str(MARKET_SHARED), str(ORDER_SHARED), str(AGENT_SHARED), str(BACKEND), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

try:
    from fastapi.testclient import TestClient
    from app.main import create_app

    FASTAPI_TESTCLIENT_AVAILABLE = True
except Exception:
    TestClient = None
    FASTAPI_TESTCLIENT_AVAILABLE = False


@unittest.skipUnless(FASTAPI_TESTCLIENT_AVAILABLE, "FastAPI TestClient is not available")
class CompanyCompareRoutesTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app())

    def test_compare_route_returns_separate_quantitative_and_narrative_layers(self):
        expected = {
            "version": "company-compare.v1",
            "status": "ready",
            "baseSymbol": "NVDA",
            "compareSymbols": ["AMD"],
            "comparedSymbols": ["NVDA", "AMD"],
            "question": None,
            "quantitative": {"status": "ready", "sections": [], "dataGaps": []},
            "qualitative": {"status": "ready", "sections": [], "dataGaps": []},
            "narrative": {"status": "not-requested", "summary": None, "sections": [], "insights": [], "dataGaps": []},
            "sources": [],
            "dataGaps": [],
            "createdByAgentId": "company-compare-agent",
        }
        with patch("app.routes.llm.company_compare_analysis", return_value=expected) as service:
            response = self.client.post(
                "/api/llm/company-compare",
                json={"baseSymbol": "NVDA", "compareSymbols": ["AMD"]},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        self.assertEqual(service.call_args.args[0].baseSymbol, "NVDA")
        self.assertEqual(service.call_args.args[0].compareSymbols, ["AMD"])

    def test_candidate_route_returns_ontology_candidate_shape_without_extra_nesting(self):
        expected = {
            "symbol": "NVDA",
            "candidates": [{
                "symbol": "AMD",
                "companyName": "Advanced Micro Devices",
                "relationType": "same-theme",
                "themes": ["AI 반도체"],
            }],
            "dataGaps": [],
        }
        with patch("app.routes.llm.company_compare_candidates", return_value=expected):
            response = self.client.get("/api/llm/company-compare/candidates?symbol=NVDA")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)

    def test_quantitative_route_does_not_wait_for_narrative(self):
        expected = {
            "version": "company-compare.v1",
            "status": "ready",
            "baseSymbol": "NVDA",
            "compareSymbols": ["AMD"],
            "comparedSymbols": ["NVDA", "AMD"],
            "question": None,
            "quantitative": {"status": "ready", "sections": [], "dataGaps": []},
            "qualitative": {"status": "ready", "sections": [], "dataGaps": []},
            "narrative": {"status": "not-requested", "summary": None, "sections": [], "insights": [], "dataGaps": []},
            "sources": [],
            "dataGaps": [],
            "createdByAgentId": "company-compare-agent",
        }
        with patch("app.routes.llm.company_compare_quantitative", return_value=expected) as service:
            response = self.client.post(
                "/api/llm/company-compare/quantitative",
                json={"baseSymbol": "NVDA", "compareSymbols": ["AMD"]},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        service.assert_called_once()

    def test_compare_route_uses_existing_authenticated_agent_rate_limit(self):
        expected = {
            "version": "company-compare.v1",
            "status": "ready",
            "baseSymbol": "NVDA",
            "compareSymbols": ["AMD"],
            "comparedSymbols": ["NVDA", "AMD"],
            "question": None,
            "quantitative": {"status": "ready", "sections": [], "dataGaps": []},
            "qualitative": {"status": "ready", "sections": [], "dataGaps": []},
            "narrative": {"status": "ready", "summary": "정보성 비교", "sections": [], "insights": [], "dataGaps": []},
            "sources": [],
            "dataGaps": [],
            "createdByAgentId": "company-compare-agent",
        }
        with (
            patch("app.routes.llm.auth_is_enabled", return_value=True),
            patch("app.routes.llm.enforce_agent_rate_limit") as rate_limit,
            patch("app.routes.llm.company_compare_analysis", return_value=expected),
        ):
            response = self.client.post(
                "/api/llm/company-compare",
                json={"baseSymbol": "NVDA", "compareSymbols": ["AMD"]},
            )

        self.assertEqual(response.status_code, 200)
        rate_limit.assert_called_once()


class CompanyCompareServiceTest(unittest.TestCase):
    def setUp(self):
        from app.services import company_compare as service

        self.service = service
        self.request = SimpleNamespace(baseSymbol="NVDA", compareSymbols=["AMD"], question=None)
        self.quantitative_result = {
            "version": "company-compare.v1",
            "status": "ready",
            "baseSymbol": "NVDA",
            "compareSymbols": ["AMD"],
            "comparedSymbols": ["NVDA", "AMD"],
            "question": None,
            "quantitative": {"status": "ready", "sections": [], "dataGaps": []},
            "qualitative": {"status": "ready", "sections": [], "dataGaps": []},
            "narrative": {"status": "not-requested", "summary": None, "sections": [], "insights": [], "dataGaps": []},
            "sources": [],
            "dataGaps": [],
            "createdByAgentId": "company-compare-agent",
        }

    def test_service_merges_ready_orchestrator_narrative(self):
        agent = Mock()
        agent.compare.return_value = dict(self.quantitative_result)
        ready = {
            "status": "ready",
            "summary": "정보성 비교입니다.",
            "sections": [],
            "insights": [],
            "dataGaps": [],
            "validationWarnings": [],
        }
        with (
            patch.object(self.service, "_agent", return_value=agent),
            patch.object(self.service, "request_agent_company_compare_narrative", return_value=ready) as narrative_request,
        ):
            result = self.service.company_compare_analysis(self.request)

        self.assertEqual(result["narrative"], ready)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(narrative_request.call_args.args[0]["quantitative"], self.quantitative_result["quantitative"])
        self.assertEqual(narrative_request.call_args.args[0]["qualitative"], self.quantitative_result["qualitative"])

    def test_quantitative_service_never_calls_narrative_gateway(self):
        agent = Mock()
        agent.compare.return_value = dict(self.quantitative_result)
        with (
            patch.object(self.service, "_agent", return_value=agent),
            patch.object(self.service, "request_agent_company_compare_narrative") as narrative_request,
        ):
            result = self.service.company_compare_quantitative(self.request)

        self.assertEqual(result, self.quantitative_result)
        narrative_request.assert_not_called()

    def test_service_keeps_quantitative_when_narrative_is_unavailable(self):
        agent = Mock()
        agent.compare.return_value = dict(self.quantitative_result)
        with (
            patch.object(self.service, "_agent", return_value=agent),
            patch.object(self.service, "request_agent_company_compare_narrative", side_effect=RuntimeError("offline")),
        ):
            result = self.service.company_compare_analysis(self.request)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["quantitative"]["status"], "ready")
        self.assertEqual(result["narrative"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
