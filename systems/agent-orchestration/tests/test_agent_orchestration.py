import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(REPO_ROOT / "systems" / "market-data" / "shared"))
sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda *args, **kwargs: None))

from gops_agents.agents import AgentContext, NewsAgent, OntologyAgent, VerificationGuardrailAgent
from gops_agents.contracts import AgentFinding, EvidenceItem, IntentRoute, utc_now_iso
from gops_agents.event_detector import MarketEventDetector, MarketEventThresholds
from gops_agents.news_localization import NewsLocalizationService
from gops_agents.orchestrator import AgentOrchestrator
from gops_agents.publisher import notification_payload
from gops_agents.providers import ClickHouseNewsProvider, GraphDBOntologyProvider, ProviderRequest
from gops_agents.synthesizer import FinalAnswerSynthesizer
import alfaka.alpaca.news  # noqa: F401
import alfaka.common.secrets  # noqa: F401


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
        self.calls = 0
        self.requested_symbols = []

    def news_articles(self, symbol, limit, days):
        self.calls += 1
        self.requested_symbols.append(symbol)
        return self.rows


class FakeOntologyProvider:
    def __init__(self, evidence):
        self.evidence = evidence

    def fetch(self, request):
        return self.evidence


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


def news_panel_props_command(report):
    return next(
        command for command in report.layoutProposal.commands
        if command.get("type") in {"layout.panel.add", "layout.panel.props.update"}
        and (
            command.get("payload", {}).get("panelType") == "newsFeed"
            or command.get("payload", {}).get("panelId") == "panel-news"
        )
    )


class AgentOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self._env_backup = {
            name: os.environ.pop(name, None)
            for name in [
                "OPENAI_API_KEY",
                "AGENT_ONTOLOGY_ROLE_ANALYSIS_PROVIDER",
                "AGENT_ONTOLOGY_FINAL_ANSWER_PROVIDER",
                "AGENT_ROLE_ANALYSIS_PROVIDER",
                "AGENT_FINAL_ANSWER_PROVIDER",
            ]
        }

    def tearDown(self):
        for name, value in self._env_backup.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

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

    def test_conductor_routes_news_based_market_question_to_news_role(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "NVDA 뉴스 기반으로 왜 올랐는지 알려줘",
            "layoutContext": layout_context(),
        })

        self.assertEqual(report.route.intentType, "news")
        self.assertEqual(report.route.selectedRoles, ["news"])

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
        self.assertGreater(evidence[0].raw["importanceScore"], 0)

    def test_news_provider_uses_alpaca_fallback_when_clickhouse_is_empty(self):
        clickhouse = FakeClickHouseProvider([])
        provider = ClickHouseNewsProvider(clickhouse_provider=clickhouse, limit=5, publish_fallback=False)
        article = {
            "id": "fallback-1",
            "headline": "NVDA shares rise after new AI chip launch",
            "summary": "NVIDIA announced a new data center GPU.",
            "url": "https://example.com/fallback",
            "source": "alpaca",
            "created_at": "2026-06-30T01:02:03Z",
            "symbols": ["NVDA"],
        }

        with patch("alfaka.common.secrets.load_alpaca_credentials", return_value=("key", "secret")):
            with patch("alfaka.alpaca.news.fetch_alpaca_news", return_value=[article]) as fetch:
                evidence = provider.fetch(ProviderRequest("NVDA", "뉴스 보여줘"))

        fetch.assert_called_once()
        self.assertEqual(clickhouse.calls, 1)
        self.assertEqual(evidence[0].status, "available")
        self.assertEqual(evidence[0].raw["dataSource"], "alpaca-direct")
        self.assertGreaterEqual(evidence[0].raw["importanceScore"], 0.8)

    def test_news_provider_does_not_fallback_when_clickhouse_news_is_fresh(self):
        clickhouse = FakeClickHouseProvider([
            {
                "articleId": "fresh-1",
                "headline": "NVDA shares rise after strong earnings",
                "summary": "NVDA revenue beat expectations.",
                "publishedAt": utc_now_iso(),
                "url": "https://example.com/fresh",
                "symbols": ["NVDA"],
            }
        ])
        provider = ClickHouseNewsProvider(clickhouse_provider=clickhouse, limit=5, publish_fallback=False)

        with patch("alfaka.alpaca.news.fetch_alpaca_news") as fetch:
            evidence = provider.fetch(ProviderRequest("NVDA", "뉴스 보여줘"))

        fetch.assert_not_called()
        self.assertEqual(evidence[0].raw["dataSource"], "clickhouse")

    def test_orchestrator_adds_news_panel_layout_payload(self):
        provider = ClickHouseNewsProvider(
            clickhouse_provider=FakeClickHouseProvider([
                {
                    "articleId": "panel-1",
                    "headline": "NVDA shares rise after strong earnings",
                    "summary": "NVDA revenue beat expectations.",
                    "publishedAt": utc_now_iso(),
                    "url": "https://example.com/panel",
                    "symbols": ["NVDA"],
                    "source": "alpaca",
                }
            ]),
            publish_fallback=False,
        )
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(provider)

        report = orchestrator.analyze({"symbol": "NVDA", "intent": "뉴스 보여줘", "agentIds": ["agent-02"]})

        news_command = news_panel_props_command(report)
        props = news_command["payload"]["props"]
        self.assertEqual(props["symbol"], "NVDA")
        self.assertEqual(props["latestNews"][0]["title"], "NVDA shares rise after strong earnings")
        self.assertEqual(props["majorNews"][0]["importanceScore"], props["latestNews"][0]["importanceScore"])

    def test_orchestrator_updates_existing_news_panel_layout_payload(self):
        provider = ClickHouseNewsProvider(
            clickhouse_provider=FakeClickHouseProvider([
                {
                    "articleId": "panel-existing-1",
                    "headline": "NVDA shares rise after strong earnings",
                    "summary": "NVDA revenue beat expectations.",
                    "publishedAt": utc_now_iso(),
                    "url": "https://example.com/panel-existing",
                    "symbols": ["NVDA"],
                    "source": "alpaca",
                }
            ]),
            publish_fallback=False,
        )
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(provider)

        report = orchestrator.analyze({
            "symbol": "NVDA",
            "intent": "뉴스 보여줘",
            "agentIds": ["agent-02"],
            "layoutContext": layout_context(),
        })

        news_command = news_panel_props_command(report)
        self.assertEqual(news_command["type"], "layout.panel.props.update")
        self.assertEqual(news_command["payload"]["panelId"], "panel-news")
        self.assertEqual(news_command["target"]["panelId"], "panel-news")
        self.assertEqual(news_command["payload"]["props"]["latestNews"][0]["title"], "NVDA shares rise after strong earnings")
        self.assertFalse(any(command["type"] == "layout.panel.add" for command in report.layoutProposal.commands))

    def test_news_content_request_does_not_route_to_ui_agent_when_ui_router_is_available(self):
        provider = ClickHouseNewsProvider(
            clickhouse_provider=FakeClickHouseProvider([
                {
                    "articleId": "orcl-news-1",
                    "headline": "Oracle shares rise on cloud demand",
                    "summary": "ORCL cloud demand lifted sentiment.",
                    "publishedAt": utc_now_iso(),
                    "url": "https://example.com/orcl-news",
                    "symbols": ["ORCL"],
                    "source": "alpaca",
                }
            ]),
            publish_fallback=False,
        )
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(provider)
        ui_router_response = {
            "output_text": json.dumps({
                "isUiIntent": True,
                "intentKind": "layout",
                "targetPanelType": "newsFeed",
                "targetPanelId": "panel-news",
                "action": "focus",
                "sizeIntent": None,
                "positionIntent": None,
                "confidence": 0.94,
                "reason": "Incorrectly treated latest-news lookup as a news panel focus request.",
            })
        }

        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-key",
            "AGENT_FINAL_ANSWER_PROVIDER": "deterministic",
            "AGENT_NEWS_LOCALIZATION_PROVIDER": "deterministic",
            "AGENT_ROLE_ANALYSIS_PROVIDER": "deterministic",
        }, clear=False):
            with patch("urllib.request.urlopen", return_value=FakeOpenAIResponse(ui_router_response)) as openai:
                report = orchestrator.analyze({
                    "symbol": "ORCL",
                    "intent": "ORCL 최신뉴스 보여줘",
                    "agentIds": ["agent-02"],
                    "layoutContext": layout_context(),
                })

        openai.assert_not_called()
        self.assertEqual(report.route.intentType, "news")
        self.assertEqual(report.route.selectedRoles, ["news"])
        finding_roles = {finding.role for finding in report.findings}
        self.assertIn("news-analysis", finding_roles)
        self.assertIn("verification-guardrail", finding_roles)
        self.assertEqual(report.providerEvidence[0].provider, "news")
        self.assertNotEqual(report.finalAnswer.summary, "UIAgent arranged 시장 뉴스 for the requested UI action.")

    def test_news_content_request_variants_default_to_news_agent(self):
        provider = ClickHouseNewsProvider(
            clickhouse_provider=FakeClickHouseProvider([
                {
                    "articleId": "orcl-article-1",
                    "headline": "Oracle announces database update",
                    "summary": "The article describes a database cloud update.",
                    "publishedAt": utc_now_iso(),
                    "url": "https://example.com/orcl-article",
                    "symbols": ["ORCL"],
                    "source": "alpaca",
                }
            ]),
            publish_fallback=False,
        )
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(provider)

        for intent in (
            "관련 뉴스 보여줘",
            "오늘 기사 요약해줘",
            "NVDA 뉴스 기반으로 왜 올랐는지 알려줘",
            "뉴스 중심으로 분석해줘",
        ):
            with self.subTest(intent=intent):
                report = orchestrator.analyze({
                    "symbol": "NVDA",
                    "intent": intent,
                    "agentIds": ["agent-02"],
                    "layoutContext": layout_context(),
                })

                self.assertEqual(report.route.intentType, "news")
                self.assertEqual(report.route.selectedRoles, ["news"])
                news_command = news_panel_props_command(report)
                self.assertEqual(news_command["type"], "layout.panel.props.update")
                self.assertEqual(news_command["payload"]["panelId"], "panel-news")

    def test_news_localization_success_updates_panel_payload_and_preserves_originals(self):
        provider = ClickHouseNewsProvider(
            clickhouse_provider=FakeClickHouseProvider([
                {
                    "articleId": "panel-ko-1",
                    "headline": "NVDA shares rise after strong earnings",
                    "summary": "NVDA revenue beat expectations.",
                    "publishedAt": utc_now_iso(),
                    "url": "https://example.com/panel-ko",
                    "symbols": ["NVDA"],
                    "source": "alpaca",
                }
            ]),
            publish_fallback=False,
        )
        response = {
            "output_text": json.dumps({
                "items": [
                    {
                        "key": "article:panel-ko-1",
                        "localizedTitle": "엔비디아, 실적 호조에 주가 상승",
                        "localizedSummary": "엔비디아 매출이 기대치를 웃돌며 투자심리가 개선됐습니다.",
                    }
                ]
            })
        }
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(provider, localizer=NewsLocalizationService())

        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-key",
            "AGENT_NEWS_LOCALIZATION_PROVIDER": "openai",
            "AGENT_ROLE_ANALYSIS_PROVIDER": "deterministic",
            "AGENT_FINAL_ANSWER_PROVIDER": "deterministic",
        }, clear=False):
            with patch("urllib.request.urlopen", return_value=FakeOpenAIResponse(response)):
                report = orchestrator.analyze({"symbol": "NVDA", "intent": "뉴스 보여줘", "agentIds": ["agent-02"]})

        evidence = report.providerEvidence[0]
        self.assertEqual(evidence.raw["originalTitle"], "NVDA shares rise after strong earnings")
        self.assertEqual(evidence.raw["originalSummary"], "NVDA revenue beat expectations.")
        self.assertEqual(evidence.raw["localizedTitle"], "엔비디아, 실적 호조에 주가 상승")
        self.assertEqual(evidence.raw["localizedSummary"], "엔비디아 매출이 기대치를 웃돌며 투자심리가 개선됐습니다.")
        self.assertEqual(evidence.url, "https://example.com/panel-ko")
        news_command = news_panel_props_command(report)
        item = news_command["payload"]["props"]["latestNews"][0]
        self.assertEqual(item["title"], "엔비디아, 실적 호조에 주가 상승")
        self.assertEqual(item["summary"], "엔비디아 매출이 기대치를 웃돌며 투자심리가 개선됐습니다.")
        self.assertEqual(item["originalTitle"], "NVDA shares rise after strong earnings")
        self.assertEqual(item["originalSummary"], "NVDA revenue beat expectations.")

    def test_news_localization_failure_falls_back_to_original_text(self):
        provider = ClickHouseNewsProvider(
            clickhouse_provider=FakeClickHouseProvider([
                {
                    "articleId": "panel-fallback-1",
                    "headline": "NVDA shares rise after strong earnings",
                    "summary": "NVDA revenue beat expectations.",
                    "publishedAt": utc_now_iso(),
                    "url": "https://example.com/panel-fallback",
                    "symbols": ["NVDA"],
                }
            ]),
            publish_fallback=False,
        )
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(provider, localizer=NewsLocalizationService())

        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-key",
            "AGENT_NEWS_LOCALIZATION_PROVIDER": "openai",
            "AGENT_ROLE_ANALYSIS_PROVIDER": "deterministic",
            "AGENT_FINAL_ANSWER_PROVIDER": "deterministic",
        }, clear=False):
            with patch("urllib.request.urlopen", return_value=FakeOpenAIResponse({"output_text": "{invalid"})):
                report = orchestrator.analyze({"symbol": "NVDA", "intent": "뉴스 보여줘", "agentIds": ["agent-02"]})

        evidence = report.providerEvidence[0]
        self.assertNotIn("localizedTitle", evidence.raw)
        news_command = news_panel_props_command(report)
        item = news_command["payload"]["props"]["latestNews"][0]
        self.assertEqual(item["title"], "NVDA shares rise after strong earnings")
        self.assertEqual(item["summary"], "NVDA revenue beat expectations.")

    def test_news_localization_cache_avoids_repeated_openai_calls(self):
        provider = ClickHouseNewsProvider(
            clickhouse_provider=FakeClickHouseProvider([
                {
                    "articleId": "cache-1",
                    "headline": "NVDA shares rise after strong earnings",
                    "summary": "NVDA revenue beat expectations.",
                    "publishedAt": utc_now_iso(),
                    "url": "https://example.com/cache",
                    "symbols": ["NVDA"],
                }
            ]),
            publish_fallback=False,
        )
        localizer = NewsLocalizationService(cache_ttl_seconds=3600)
        agent = NewsAgent(provider, localizer=localizer)
        context = AgentContext(symbol="NVDA", intent="뉴스 보여줘")
        response = {
            "output_text": json.dumps({
                "items": [
                    {
                        "key": "article:cache-1",
                        "localizedTitle": "엔비디아 실적 호조",
                        "localizedSummary": "엔비디아 매출이 기대치를 웃돌았습니다.",
                    }
                ]
            })
        }

        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-key",
            "AGENT_NEWS_LOCALIZATION_PROVIDER": "openai",
            "AGENT_ROLE_ANALYSIS_PROVIDER": "deterministic",
        }, clear=False):
            with patch("urllib.request.urlopen", return_value=FakeOpenAIResponse(response)) as urlopen:
                first = agent.analyze(context)
                second = agent.analyze(context)

        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(first.evidence[0].raw["localizedTitle"], "엔비디아 실적 호조")
        self.assertEqual(second.evidence[0].raw["localizedTitle"], "엔비디아 실적 호조")

    def test_news_final_answer_prefers_localized_text(self):
        evidence = [
            EvidenceItem(
                provider="news",
                status="available",
                title="NVDA shares rise after strong earnings",
                summary="NVDA revenue beat expectations.",
                url="https://example.com/localized-answer",
                raw={
                    "articleId": "answer-1",
                    "publishedAt": utc_now_iso(),
                    "impactDirection": "positive",
                    "eventType": "earnings",
                    "importanceScore": 0.9,
                    "relevanceScore": 0.9,
                    "originalTitle": "NVDA shares rise after strong earnings",
                    "originalSummary": "NVDA revenue beat expectations.",
                    "localizedTitle": "엔비디아, 실적 호조에 상승",
                    "localizedSummary": "엔비디아 매출이 기대치를 웃돌며 긍정적으로 분류됐습니다.",
                },
            )
        ]
        answer = FinalAnswerSynthesizer().synthesize(
            symbol="NVDA",
            intent="뉴스 보여줘",
            route=IntentRoute("rule", "news", ["news"], 0.9, "test"),
            findings=[],
            provider_evidence=evidence,
        )

        self.assertIn("엔비디아, 실적 호조에 상승", answer.sections[0].bullets[0])
        self.assertIn("엔비디아 매출이 기대치를 웃돌며", answer.sections[0].bullets[0])
        self.assertEqual(answer.citations[0].title, "엔비디아, 실적 호조에 상승")

    def test_explicit_ticker_in_intent_overrides_current_chart_symbol_for_news(self):
        clickhouse = FakeClickHouseProvider([
            {
                "articleId": "xlv-1",
                "headline": "XLV rises as healthcare stocks gain",
                "summary": "Healthcare sector ETF moved higher.",
                "publishedAt": utc_now_iso(),
                "url": "https://example.com/xlv",
                "symbols": ["XLV"],
                "source": "alpaca",
            }
        ])
        provider = ClickHouseNewsProvider(clickhouse_provider=clickhouse, publish_fallback=False)
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(provider)

        report = orchestrator.analyze({
            "symbol": "NVDA",
            "intent": "XLV 헬스케어 뉴스 보여줘",
            "agentIds": ["agent-02"],
            "chartContext": {
                "chartDocument": {"symbol": "NVDA", "timeframe": "1m"},
                "dataStatus": {"candleCount": 10, "state": "ready"},
            },
        })

        self.assertEqual(report.symbol, "XLV")
        self.assertEqual(clickhouse.requested_symbols, ["XLV"])
        self.assertIn("XLV", report.finalAnswer.title)
        news_command = news_panel_props_command(report)
        self.assertEqual(news_command["payload"]["props"]["symbol"], "XLV")
        self.assertEqual(news_command["payload"]["props"]["latestNews"][0]["symbol"], "XLV")

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

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "AGENT_NEWS_LOCALIZATION_PROVIDER": "deterministic"}, clear=False):
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

    def test_ontology_agent_defaults_to_graphdb_evidence_analysis_even_with_openai_key(self):
        evidence = [
            EvidenceItem(
                provider="ontology",
                status="available",
                title="NVDA 테마 관계",
                summary="NVIDIA Corp는 AI/반도체/데이터센터 테마에 매핑되어 있습니다.",
                raw={"relationType": "theme", "themeName": "AI/반도체/데이터센터"},
            )
        ]
        response = {
            "output_text": json.dumps({
                "summary": "GraphDB에 없는 CUDA 공급망 관계를 추가했습니다.",
                "rationale": "없는 근거",
                "confidence": 0.99,
                "tags": ["ontology", "openai"],
            })
        }

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            with patch("urllib.request.urlopen", return_value=FakeOpenAIResponse(response)) as openai:
                finding = OntologyAgent(FakeOntologyProvider(evidence)).analyze(AgentContext(symbol="NVDA", intent="관계 분석"))

        self.assertEqual(openai.call_count, 0)
        self.assertIn("GraphDB 기준", finding.summary)
        self.assertNotIn("CUDA 공급망", finding.summary)

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

    def test_ui_fallback_keeps_explicit_news_panel_placement_request(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "뉴스를 오른쪽에 띄워줘",
            "layoutContext": layout_context(),
        })

        self.assertEqual(report.route.source, "ui-fallback")
        self.assertEqual(report.route.intentType, "ui-layout")
        self.assertEqual(report.findings, [])
        self.assertTrue(any(command["type"] == "layout.panels.arrange" for command in report.layoutProposal.commands))

    def test_ui_fallback_keeps_side_by_side_chart_news_request(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "차트랑 뉴스 나란히 보여줘",
            "layoutContext": layout_context(),
        })

        self.assertEqual(report.route.source, "ui-fallback")
        self.assertEqual(report.route.intentType, "ui-layout")
        self.assertEqual(report.findings, [])
        self.assertTrue(any(command["type"] == "layout.panels.arrange" for command in report.layoutProposal.commands))

    def test_surface_action_keeps_close_request_in_ui_agent(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "주문창 닫아줘",
            "layoutContext": layout_context(),
        })

        self.assertEqual(report.route.source, "ui-fallback")
        self.assertEqual(report.route.intentType, "ui-layout")
        self.assertEqual(report.route.selectedRoles, [])
        self.assertEqual(report.findings, [])

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

    def test_ontology_final_answer_defaults_to_deterministic_even_with_openai_key(self):
        response = {
            "output_text": json.dumps({
                "title": "NVDA 관계 분석",
                "summary": "GraphDB에 없는 CUDA 공급망 관계를 추가했습니다.",
                "sections": [{"title": "근거", "bullets": ["없는 관계입니다."]}],
                "citations": [],
                "limitations": [],
            })
        }
        evidence = [
            EvidenceItem(
                provider="ontology",
                status="available",
                title="NVDA 테마 관계",
                summary="NVIDIA Corp는 AI/반도체/데이터센터 테마에 매핑되어 있습니다.",
                raw={"relationType": "theme", "themeName": "AI/반도체/데이터센터"},
            )
        ]

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            with patch("urllib.request.urlopen", return_value=FakeOpenAIResponse(response)) as openai:
                answer = FinalAnswerSynthesizer().synthesize(
                    symbol="NVDA",
                    intent="관계 분석",
                    route=IntentRoute("rule", "ontology", ["ontology"], 0.9, "test"),
                    findings=[],
                    provider_evidence=evidence,
                )

        self.assertEqual(openai.call_count, 0)
        self.assertIn("GraphDB 기준", answer.summary)
        self.assertNotIn("CUDA 공급망", answer.summary)

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
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "test-key", "AGENT_ONTOLOGY_FINAL_ANSWER_PROVIDER": "openai"},
            clear=False,
        ):
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
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "test-key", "AGENT_ONTOLOGY_FINAL_ANSWER_PROVIDER": "openai"},
            clear=False,
        ):
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
