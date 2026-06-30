import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))

from gops_agents.agents import AgentContext, NewsAgent, VerificationGuardrailAgent
from gops_agents.contracts import AgentFinding, EvidenceItem, IntentRoute
from gops_agents.event_detector import MarketEventDetector, MarketEventThresholds
from gops_agents.orchestrator import AgentOrchestrator
from gops_agents.publisher import notification_payload
from gops_agents.providers import ClickHouseNewsProvider, GraphDBOntologyProvider, ProviderRequest
from gops_agents.synthesizer import FinalAnswerSynthesizer


class FakeSparqlClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {"results": {"bindings": []}}
        self.error = error
        self.queries = []

    def query(self, sparql):
        self.queries.append(sparql)
        if self.error:
            raise self.error
        return self.payload


class FakeOpenAIResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakeClickHouseProvider:
    def __init__(self, rows):
        self.rows = rows

    def news_articles(self, symbol, limit, days):
        return self.rows


class AgentOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self._openai_api_key = os.environ.pop("OPENAI_API_KEY", None)

    def tearDown(self):
        if self._openai_api_key is not None:
            os.environ["OPENAI_API_KEY"] = self._openai_api_key

    def test_orchestrator_returns_report_with_empty_provider_evidence(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "analyze",
            "chartContext": {
                "chartDocument": {"symbol": "NVDA", "timeframe": "1m"},
                "visibleSummary": {"lastPrice": "120.00", "change": "+2.10%"},
                "dataStatus": {"candleCount": 10, "state": "ready"},
            },
        })

        self.assertEqual(report.symbol, "NVDA")
        self.assertEqual(report.status, "completed")
        self.assertEqual(report.route.intentType, "general-analysis")
        self.assertEqual(report.route.selectedRoles, ["chart", "news", "ontology"])
        self.assertIsNotNone(report.finalAnswer)
        self.assertEqual({finding.role for finding in report.findings}, {"chart-analysis", "news-analysis", "company-relationship-analysis"})
        self.assertTrue(any(item.provider == "news" and item.status == "no-data" for item in report.providerEvidence))
        self.assertIsNone(report.notificationDecision)

    def test_orchestrator_runs_only_requested_mvp_agents(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "selected context",
            "agentIds": ["agent-01", "agent-02"],
            "chartContext": {
                "chartDocument": {"symbol": "NVDA", "timeframe": "1m"},
                "visibleSummary": {"lastPrice": "120.00", "change": "+2.10%"},
                "dataStatus": {"candleCount": 10, "state": "ready"},
            },
        })

        roles = {finding.role for finding in report.findings}
        providers = {item.provider for item in report.providerEvidence}
        self.assertIn("chart-analysis", roles)
        self.assertIn("news-analysis", roles)
        self.assertNotIn("verification-guardrail", roles)
        self.assertNotIn("macro-analysis", roles)
        self.assertNotIn("company-relationship-analysis", roles)
        self.assertEqual(providers, {"news"})
        self.assertEqual(report.route.selectedRoles, ["chart", "news"])
        self.assertTrue(report.finalAnswer.summary)

    def test_conductor_routes_news_intent_even_when_chart_agent_is_selected(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "뉴스 보여줘",
            "agentIds": ["agent-01"],
            "chartContext": {
                "chartDocument": {"symbol": "NVDA", "timeframe": "1m"},
                "dataStatus": {"candleCount": 10, "state": "ready"},
            },
        })

        roles = {finding.role for finding in report.findings}
        self.assertEqual(report.route.intentType, "news")
        self.assertEqual(report.route.selectedRoles, ["news"])
        self.assertIn("news-analysis", roles)
        self.assertNotIn("chart-analysis", roles)

    def test_conductor_routes_market_move_to_all_visible_roles(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "NVDA 왜 올랐어?",
            "chartContext": {
                "chartDocument": {"symbol": "NVDA", "timeframe": "1m"},
                "dataStatus": {"candleCount": 10, "state": "ready"},
            },
        })

        self.assertEqual(report.route.intentType, "market-move")
        self.assertEqual(report.route.selectedRoles, ["chart", "news", "ontology"])
        self.assertNotIn("macro-analysis", {finding.role for finding in report.findings})

    def test_conductor_routes_chart_intent_to_chart_role(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "차트 분석해줘",
            "chartContext": {
                "chartDocument": {"symbol": "NVDA", "timeframe": "1m"},
                "dataStatus": {"candleCount": 10, "state": "ready"},
            },
        })

        self.assertEqual(report.route.intentType, "chart")
        self.assertEqual(report.route.selectedRoles, ["chart"])
        self.assertEqual({finding.role for finding in report.findings}, {"chart-analysis"})

    def test_agent_03_selection_does_not_activate_macro_role(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "selected context",
            "agentIds": ["agent-03"],
        })

        self.assertNotIn("macro", report.route.selectedRoles)
        self.assertNotIn("macro-analysis", {finding.role for finding in report.findings})

    def test_event_detector_detects_price_surge_and_volume_spike(self):
        detector = MarketEventDetector(MarketEventThresholds(price_change_percent=3.0, volume_spike_multiplier=2.0))
        self.assertEqual(detector.detect({"symbol": "NVDA", "price": 100, "volume": 100}, "market.ticks.v1"), [])

        events = detector.detect({"symbol": "NVDA", "price": 105, "volume": 250}, "market.ticks.v1")

        event_types = {event.eventType for event in events}
        self.assertIn("price_surge", event_types)
        self.assertIn("volume_spike", event_types)

    def test_notification_payload_preserves_decision(self):
        payload = notification_payload({"symbol": "NVDA", "level": "alert", "showToast": True})

        self.assertEqual(payload["type"], "AGENT_ALERT")
        self.assertTrue(payload["showToast"])

    def test_news_provider_normalizes_dedupes_and_scores_articles(self):
        rows = [
            {
                "articleId": "a-1",
                "headline": "NVDA shares rise after strong earnings",
                "summary": "NVDA revenue beat expectations.",
                "publishedAt": "2026-06-28T00:00:00Z",
                "url": "https://example.com/old",
            },
            {
                "articleId": "a-1",
                "headline": "NVDA shares rise after strong earnings",
                "summary": "NVDA revenue beat expectations.",
                "publishedAt": "2026-06-29T00:00:00Z",
                "url": "https://example.com/new",
            },
            {
                "headline": "Macro stocks mixed",
                "summary": "Chip stocks are mixed.",
                "publishedAt": "2026-06-27T00:00:00Z",
                "url": "https://example.com/macro",
            },
        ]
        provider = ClickHouseNewsProvider(clickhouse_provider=FakeClickHouseProvider(rows), limit=10)

        evidence = provider.fetch(ProviderRequest("NVDA", "뉴스 보여줘"))

        self.assertEqual(len(evidence), 2)
        self.assertEqual(evidence[0].url, "https://example.com/new")
        self.assertEqual(evidence[0].raw["eventType"], "earnings")
        self.assertEqual(evidence[0].raw["impactDirection"], "positive")
        self.assertGreater(evidence[0].raw["relevanceScore"], evidence[1].raw["relevanceScore"])

    def test_news_agent_openai_success_and_fallback_keep_shape(self):
        provider = ClickHouseNewsProvider(
            clickhouse_provider=FakeClickHouseProvider([
                {
                    "articleId": "a-1",
                    "headline": "NVDA shares rise after strong earnings",
                    "summary": "NVDA revenue beat expectations.",
                    "publishedAt": "2026-06-29T00:00:00Z",
                }
            ]),
            limit=10,
        )
        context = AgentContext(symbol="NVDA", intent="뉴스 보여줘")
        response = {
            "output_text": json.dumps({
                "summary": "OpenAI가 뉴스 1건을 근거로 요약했습니다.",
                "rationale": "제공된 뉴스 근거만 사용했습니다.",
                "confidence": 0.77,
                "tags": ["news", "openai"],
            })
        }

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            with patch("urllib.request.urlopen", return_value=FakeOpenAIResponse(response)):
                openai_finding = NewsAgent(provider).analyze(context)
            with patch("urllib.request.urlopen", return_value=FakeOpenAIResponse({"output_text": "{invalid"})):
                fallback_finding = NewsAgent(provider).analyze(context)

        self.assertEqual(openai_finding.role, "news-analysis")
        self.assertEqual(openai_finding.summary, "OpenAI가 뉴스 1건을 근거로 요약했습니다.")
        self.assertEqual(fallback_finding.role, "news-analysis")
        self.assertIn("NVDA 뉴스 1건", fallback_finding.summary)
        self.assertTrue(fallback_finding.evidence[0].raw["impactDirection"])

    def test_graphdb_provider_maps_sparql_rows_to_ontology_evidence(self):
        payload = {
            "results": {
                "bindings": [
                    {
                        "ticker": {"value": "NVDA"},
                        "companyName": {"value": "NVIDIA Corp"},
                        "themeName": {"value": "AI/반도체/데이터센터"},
                        "themeCategory": {"value": "growth"},
                        "controlledName": {"value": "Mellanox Technologies"},
                        "confidence": {"value": "0.94"},
                        "accession": {"value": "0001045810-24-000001"},
                        "sourceUrl": {"value": "https://www.sec.gov/example"},
                    }
                ]
            }
        }
        provider = GraphDBOntologyProvider(sparql_client=FakeSparqlClient(payload), limit=3)

        evidence = provider.fetch(ProviderRequest("NVDA", "AI/반도체/데이터센터 관계 분석"))

        self.assertTrue(evidence)
        self.assertEqual(evidence[0].provider, "ontology")
        self.assertEqual(evidence[0].status, "available")
        self.assertEqual(evidence[0].raw["relationType"], "theme")
        self.assertEqual(evidence[0].raw["themeName"], "AI/반도체/데이터센터")
        self.assertEqual(evidence[0].raw["controlledName"], "Mellanox Technologies")
        self.assertEqual(evidence[0].raw["accession"], "0001045810-24-000001")
        self.assertEqual(evidence[0].url, "https://www.sec.gov/example")
        self.assertTrue(any(item.raw.get("relationType") == "control" for item in evidence))

    def test_graphdb_provider_returns_no_data_on_empty_or_error(self):
        empty_provider = GraphDBOntologyProvider(sparql_client=FakeSparqlClient(), limit=3)
        empty_evidence = empty_provider.fetch(ProviderRequest("NVDA", "관계 분석"))

        error_provider = GraphDBOntologyProvider(sparql_client=FakeSparqlClient(error=TimeoutError()), limit=3)
        error_evidence = error_provider.fetch(ProviderRequest("NVDA", "관계 분석"))

        self.assertEqual(empty_evidence[0].provider, "ontology")
        self.assertEqual(empty_evidence[0].status, "no-data")
        self.assertEqual(empty_evidence[0].raw["relationType"], "no-ontology-evidence")
        self.assertTrue(any(item.raw.get("relationType") == "no-direct-control" for item in empty_evidence))
        self.assertEqual(error_evidence[0].provider, "ontology")
        self.assertEqual(error_evidence[0].status, "no-data")
        self.assertEqual(error_evidence[0].raw["relationType"], "graphdb-unavailable")

    def test_ontology_agent_selection_routes_to_ontology_role(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "analyze",
            "agentIds": ["agent-04"],
        })

        roles = {finding.role for finding in report.findings}
        self.assertEqual(report.route.selectedRoles, ["ontology"])
        self.assertIn("company-relationship-analysis", roles)
        self.assertEqual({item.provider for item in report.providerEvidence}, {"ontology"})

    def test_ontology_keyword_routes_to_ontology_role(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "NVDA 관계 분석해줘",
        })

        self.assertEqual(report.route.selectedRoles, ["ontology"])
        self.assertEqual(report.route.intentType, "ontology")

    def test_answer_composer_combines_mvp_agent_sections_and_layout(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "NVDA 왜 올랐어?",
            "chartContext": {
                "chartDocument": {"symbol": "NVDA", "timeframe": "1m"},
                "visibleSummary": {"lastPrice": "120.00", "change": "+2.10%"},
                "dataStatus": {"candleCount": 10, "state": "ready"},
            },
        })

        section_titles = [section.title for section in report.finalAnswer.sections]
        self.assertIn("차트 근거", section_titles)
        self.assertIn("뉴스 근거", section_titles)
        self.assertIn("기업 관계 근거", section_titles)
        panel_types = [command.get("payload", {}).get("panelType") for command in report.layoutProposal.commands]
        self.assertEqual(panel_types, ["chart", "newsFeed", "aiSummary"])

    def test_openai_synthesizer_accepts_strict_json_response(self):
        response = {
            "output_text": json.dumps({
                "title": "NVDA 관계 분석",
                "summary": "providerEvidence 1건을 바탕으로 요약했습니다.",
                "sections": [{"title": "근거", "bullets": ["NVIDIA Corp 관계 근거가 있습니다."]}],
                "citations": [
                    {
                        "provider": "ontology",
                        "title": "NVDA control relationship",
                        "url": "https://www.sec.gov/example",
                        "publishedAt": None,
                    }
                ],
                "limitations": [],
            })
        }
        evidence = [
            EvidenceItem(
                provider="ontology",
                status="available",
                title="NVDA control relationship",
                summary="NVIDIA Corp has a derived relationship.",
                url="https://www.sec.gov/example",
            )
        ]
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            with patch("urllib.request.urlopen", return_value=FakeOpenAIResponse(response)):
                answer = FinalAnswerSynthesizer().synthesize(
                    symbol="NVDA",
                    intent="관계 분석",
                    route=IntentRoute("rule", "ontology", ["ontology"], 0.9, "test"),
                    findings=[],
                    provider_evidence=evidence,
                )

        self.assertEqual(answer.title, "NVDA 관계 분석")
        self.assertNotIn("providerEvidence", answer.summary)
        self.assertIn("근거", answer.summary)
        self.assertEqual(answer.citations[0].provider, "ontology")

    def test_openai_synthesizer_falls_back_on_invalid_json(self):
        evidence = [
            EvidenceItem(
                provider="ontology",
                status="available",
                title="NVDA ontology theme",
                summary="NVIDIA Corp is mapped to theme AI/반도체/데이터센터.",
            )
        ]
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            with patch("urllib.request.urlopen", return_value=FakeOpenAIResponse({"output_text": "{invalid"})):
                answer = FinalAnswerSynthesizer().synthesize(
                    symbol="NVDA",
                    intent="관계 분석",
                    route=IntentRoute("rule", "ontology", ["ontology"], 0.9, "test"),
                    findings=[],
                    provider_evidence=evidence,
                )

        self.assertIn("GraphDB 기준", answer.summary)

    def test_legacy_verification_conflict_detector_still_flags_conflict(self):
        context = AgentContext(symbol="NVDA", intent="NVDA 왜 올랐어?")
        chart_finding = AgentFinding(
            agentId="chart-agent",
            role="chart-analysis",
            summary="NVDA chart shows visible change -2.10%.",
            rationale="Chart context.",
            evidence=[
                EvidenceItem(
                    provider="chart",
                    status="available",
                    title="Chart context",
                    summary="Visible chart context.",
                    raw={"visibleSummary": {"change": "-2.10%"}},
                )
            ],
        )
        news_evidence = EvidenceItem(
            provider="news",
            status="available",
            title="NVDA shares rise after strong earnings",
            summary="NVDA revenue beat expectations.",
            raw={"impactDirection": "positive", "eventType": "earnings"},
        )
        news_finding = AgentFinding(
            agentId="news-agent",
            role="news-analysis",
            summary="NVDA 뉴스 1건을 확인했습니다.",
            rationale="핵심 뉴스.",
            evidence=[news_evidence],
        )

        verification = VerificationGuardrailAgent().analyze(context, [chart_finding, news_finding])

        self.assertIn("불일치", verification.summary)


if __name__ == "__main__":
    unittest.main()
