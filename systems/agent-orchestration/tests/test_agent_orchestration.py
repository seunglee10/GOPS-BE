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


def layout_context(*, pinned_news=False):
    return {
        "version": 1,
        "selectedPanelId": "panel-chart-primary",
        "panels": [
            {
                "id": "panel-chart-primary",
                "type": "chart",
                "placement": {"group": "workspace", "zone": "mainContext", "col": 1, "row": 1, "colSpan": 3, "rowSpan": 3},
                "layoutPinned": False,
                "layoutWeight": 9,
            },
            {
                "id": "panel-news",
                "type": "newsFeed",
                "placement": {"group": "workspace", "zone": "main", "col": 3, "row": 4, "colSpan": 1, "rowSpan": 2},
                "layoutPinned": pinned_news,
                "layoutWeight": 5,
            },
            {
                "id": "panel-ontology",
                "type": "ontologyGraph",
                "placement": {"group": "workspace", "zone": "context", "col": 4, "row": 1, "colSpan": 1, "rowSpan": 2},
                "layoutPinned": False,
                "layoutWeight": 6,
            },
            {
                "id": "panel-order",
                "type": "orderTicket",
                "placement": {"group": "workspace", "zone": "context", "col": 4, "row": 4, "colSpan": 1, "rowSpan": 2},
                "layoutPinned": False,
                "layoutWeight": 7,
            },
        ],
    }


def placements_overlap(left, right):
    left_col_end = left["col"] + left["colSpan"] - 1
    right_col_end = right["col"] + right["colSpan"] - 1
    left_row_end = left["row"] + left["rowSpan"] - 1
    right_row_end = right["row"] + right["rowSpan"] - 1
    return not (
        left_col_end < right["col"] or
        right_col_end < left["col"] or
        left_row_end < right["row"] or
        right_row_end < left["row"]
    )


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
        self.assertIsNotNone(report.finalAnswer)
        self.assertTrue(any(item.provider == "news" and item.status == "no-data" for item in report.providerEvidence))
        self.assertEqual(report.notificationDecision.level, "none")

    def test_orchestrator_runs_only_requested_visible_agents_before_internal_steps(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "analyze news with chart",
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
        self.assertIn("verification-guardrail", roles)
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
            "layoutContext": layout_context(),
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
        self.assertTrue(any(item["panelId"] == "panel-news" and item["layoutWeight"] == 100 for item in report.layoutProposal.panelPriorities))
        self.assertTrue(any(command["type"] == "layout.panel.move" and command["payload"]["panelId"] == "panel-news" for command in report.layoutProposal.commands))

    def test_layout_agent_skips_commands_without_layout_context(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "뉴스 보여줘",
        })

        self.assertEqual(report.layoutProposal.commands, [])
        self.assertEqual(report.layoutProposal.panelPriorities, [])

    def test_layout_agent_does_not_move_pinned_primary_panel(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "뉴스 보여줘",
            "layoutContext": layout_context(pinned_news=True),
        })

        self.assertTrue(any(item["panelId"] == "panel-news" and item["layoutWeight"] == 100 for item in report.layoutProposal.panelPriorities))
        self.assertFalse(any(command["type"] == "layout.panel.move" for command in report.layoutProposal.commands))

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
        self.assertEqual(report.route.selectedRoles, ["chart", "news", "macro", "ontology"])

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

    def test_korean_ontology_ui_prompt_routes_to_ontology_layout(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "UI 바꿔줘 온톨로지 기반으로",
            "layoutContext": layout_context(),
        })

        self.assertEqual(report.route.selectedRoles, [])
        self.assertEqual(report.route.intentType, "ui-layout")
        self.assertEqual(report.findings, [])
        self.assertTrue(any(item["panelId"] == "panel-ontology" and item["layoutWeight"] == 100 for item in report.layoutProposal.panelPriorities))
        self.assertTrue(any(command["type"] == "layout.panels.arrange" for command in report.layoutProposal.commands))

    def test_llm_ui_router_sends_order_panel_resize_only_to_ui_agent(self):
        response = {
            "output_text": json.dumps({
                "isUiIntent": True,
                "intentKind": "layout",
                "targetPanelType": "orderTicket",
                "targetPanelId": "panel-order",
                "action": "resize",
                "sizeIntent": "max",
                "confidence": 0.94,
                "reason": "The user asked to maximize the order entry panel.",
            })
        }

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            with patch("urllib.request.urlopen", return_value=FakeOpenAIResponse(response)) as openai:
                report = AgentOrchestrator().analyze({
                    "symbol": "NVDA",
                    "intent": "주문 입력 패널 제일 크게 만들어줘",
                    "agentIds": ["agent-01"],
                    "layoutContext": layout_context(),
                })

        self.assertEqual(openai.call_count, 1)
        self.assertEqual(report.route.source, "ui-llm")
        self.assertEqual(report.route.intentType, "ui-layout")
        self.assertEqual(report.route.selectedRoles, [])
        self.assertEqual(report.findings, [])
        self.assertEqual(report.providerEvidence, [])
        self.assertEqual(
            [command["type"] for command in report.layoutProposal.commands],
            ["layout.panel.priority.set", "layout.panels.arrange", "layout.panel.move", "layout.reflow"],
        )
        arrange_command = next(command for command in report.layoutProposal.commands if command["type"] == "layout.panels.arrange")
        order_placement = next(
            item["placement"]
            for item in arrange_command["payload"]["placements"]
            if item["panelId"] == "panel-order"
        )
        self.assertEqual(order_placement["col"], 1)
        self.assertEqual(order_placement["row"], 1)
        self.assertEqual(order_placement["colSpan"], 3)
        self.assertEqual(order_placement["rowSpan"], 5)

    def test_ui_fallback_handles_informal_order_panel_resize_when_llm_unavailable(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "주문창좀 젤 크게",
            "agentIds": ["agent-01"],
            "layoutContext": layout_context(),
        })

        self.assertEqual(report.route.source, "ui-fallback")
        self.assertEqual(report.route.intentType, "ui-layout")
        self.assertEqual(report.route.selectedRoles, [])
        self.assertEqual(report.findings, [])
        self.assertTrue(any(command["type"] == "layout.panels.arrange" for command in report.layoutProposal.commands))

    def test_ui_agent_preserves_pinned_panels_and_uses_largest_available_slot(self):
        context = layout_context()
        context["panels"][0]["layoutPinned"] = True

        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "뉴스 패널 제일 크게 만들어줘",
            "layoutContext": context,
        })

        arrange_command = next(command for command in report.layoutProposal.commands if command["type"] == "layout.panels.arrange")
        news_placement = next(
            item["placement"]
            for item in arrange_command["payload"]["placements"]
            if item["panelId"] == "panel-news"
        )
        pinned_chart_placement = context["panels"][0]["placement"]
        self.assertFalse(placements_overlap(news_placement, pinned_chart_placement))
        self.assertGreater(news_placement["colSpan"] * news_placement["rowSpan"], 2)
        self.assertEqual(report.layoutProposal.autoApply, True)

    def test_ui_fallback_moves_chart_to_bottom(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "차트를 아래로 옮겨줘",
            "layoutContext": layout_context(),
        })

        self.assertEqual(report.route.intentType, "ui-layout")
        arrange_command = next(command for command in report.layoutProposal.commands if command["type"] == "layout.panels.arrange")
        chart_placement = next(
            item["placement"]
            for item in arrange_command["payload"]["placements"]
            if item["panelId"] == "panel-chart-primary"
        )
        self.assertEqual(chart_placement["row"], 3)

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

    def test_verification_conflict_is_reflected_in_market_move_answer(self):
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
        answer = FinalAnswerSynthesizer().synthesize(
            symbol="NVDA",
            intent="NVDA 왜 올랐어?",
            route=IntentRoute("rule", "market-move", ["chart", "news", "macro", "ontology"], 0.9, "test"),
            findings=[chart_finding, news_finding, verification],
            provider_evidence=[news_evidence],
        )

        self.assertIn("불일치", verification.summary)
        self.assertTrue(any("불일치" in bullet for section in answer.sections for bullet in section.bullets))
        self.assertTrue(any("불일치" in limitation for limitation in answer.limitations))


if __name__ == "__main__":
    unittest.main()
