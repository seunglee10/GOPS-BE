import json
import os
import sys
import time
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(REPO_ROOT / "systems" / "market-data" / "shared"))
sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda *args, **kwargs: None))

from gops_agents.contracts import AgentAnswer, AgentFinding, AnalysisReport, DataSnapshot, EvidenceItem, FinalAnswer, FinalResponse, IntentRoute, RoutePlan, RuntimePolicy, SynthesisInput, utc_now_iso
from gops_agents.events.detector import MarketEventDetector, MarketEventThresholds
from gops_agents.events.publisher import notification_payload
from gops_agents.intent_understanding import build_query_understanding
from gops_agents.intent_understanding.classifier import classifier_result_from_payload
from gops_agents.intent_understanding.ui_parser import parse_ui_query
from gops_agents.orchestrator import (
    AgentOrchestrator,
    canonical_analysis_intent,
    extract_relationship_symbols_from_intent,
    extract_symbol_from_intent,
    relationship_symbols_for_context,
)
from gops_agents.operations import build_agent_operation_ir, maybe_plan_operation_ir, needs_operation_planner, normalize_operation_references
from gops_agents.providers import ClickHouseNewsProvider, GraphDBOntologyProvider, ProviderRequest
from gops_agents.providers.graph_path_cache import MemoryGraphPathCache
from gops_agents.providers.news_cache import MemoryNewsEvidenceCache, RedisNewsEvidenceCache
from gops_agents.providers.news_localization import NewsLocalizationService
from gops_agents.query_understanding import EntityAliasRecord, EntityCatalogProvider, EntityResolution, KoreanEntityResolver
from gops_agents.query_understanding.alias_index import EntityAliasIndex
from gops_agents.query_understanding.supported_companies import is_supported_company_symbol, market_symbol_registry_supports, supported_company_catalog
from gops_agents.retrieval import provider_bulkhead
from gops_agents.retrieval.context import GraphExpansion, RelatedSymbol
from gops_agents.retrieval.graph_expansion import (
    GraphExpansionCache,
    GraphExpansionWriter,
    build_graph_expansion_from_evidence,
    graph_expansion_cache_key,
)
from gops_agents.retrieval.snapshots import (
    ANALYSIS_ANSWER_POLICY,
    apply_rule_guardrail,
    build_synthesis_input,
    chart_reference_evidence,
    news_reference_evidence,
    trim_cross_signals,
)
from gops_agents.roles import AgentContext, NewsAgent, OntologyAgent, VerificationGuardrailAgent
from gops_agents.runtime.admission import AdmissionPolicy, admit_analysis_request
from gops_agents.runtime.analysis_cache import MemoryAgentAnalysisCache, RedisAgentAnalysisCache
from gops_agents.runtime.delivery_gateway import AgentDeliveryGateway
from gops_agents.runtime.envelope import build_request_envelope
from gops_agents.runtime.queues import AnalysisQueueMetrics
from gops_agents.runtime.report_store import InMemoryReportStore, RedisReportStore, final_response_from_dict
from gops_agents.runtime import RuntimeRunContext
from gops_agents.runtime.workers import AgentAnalysisWorker
from gops_agents.orchestration.routing import route_intent
from gops_agents.orchestration.request import normalize_request_state
from gops_agents.orchestration.cache import analysis_cache_key_for_state
from gops_agents.synthesis import FinalAnswerSynthesizer
import alfaka.alpaca.news  # noqa: F401
import alfaka.common.secrets  # noqa: F401
from alfaka.news.relevance import classify_subject_relevance


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


class QueueSparqlClient:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.queries = []

    def query(self, sparql):
        self.queries.append(sparql)
        if not self.payloads:
            return {"results": {"bindings": []}}
        return self.payloads.pop(0)


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
    def __init__(self, rows, candles=None):
        self.rows = rows
        self.candle_rows = candles or []
        self.calls = 0
        self.requested_symbols = []

    def news_articles(self, symbol, limit, days):
        self.calls += 1
        self.requested_symbols.append(symbol)
        return self.rows

    def candles(self, symbol, interval, limit):
        return self.candle_rows[-limit:]


class FakeLocalizedClickHouseProvider(FakeClickHouseProvider):
    def __init__(self, rows, localized_rows=None, daily_rows=None, candles=None):
        super().__init__(rows)
        self.localized_rows = localized_rows or []
        self.daily_rows = daily_rows or []
        self.candle_rows = candles or []
        self.localized_calls = 0
        self.localized_requested_symbols = []
        self.daily_calls = 0
        self.daily_requested_symbols = []
        self.daily_requested_days = []

    def localized_news_articles_for_symbols(self, symbols, limit, days, locale="ko-KR"):
        self.localized_calls += 1
        self.localized_requested_symbols.append(list(symbols))
        return self.localized_rows[:limit]

    def company_daily_news_summaries(self, symbol, limit=5, days=30, locale="ko-KR"):
        self.daily_calls += 1
        self.daily_requested_symbols.append(symbol)
        self.daily_requested_days.append(days)
        return self.daily_rows[:limit]


class FakeLocalizedRedisProvider:
    def __init__(self, rows, daily_rows=None, daily_coverage=None):
        self.rows = rows
        self.daily_rows = daily_rows or []
        self.daily_coverage = daily_coverage
        self.calls = 0
        self.requested_symbols = []
        self.daily_calls = 0
        self.localized_warm_calls = []
        self.daily_warm_calls = []

    def localized_news_articles_for_symbols(self, symbols, limit, locale="ko-KR"):
        self.calls += 1
        self.requested_symbols.append(list(symbols))
        return self.rows[:limit]

    def warm_localized_news_articles(self, rows, locale="ko-KR"):
        self.localized_warm_calls.append({"rows": list(rows), "locale": locale})
        self.rows = list(rows)
        return len(rows)

    def company_daily_news_summaries(self, symbol, limit=5, locale="ko-KR"):
        self.daily_calls += 1
        return self.daily_rows[:limit]

    def company_daily_news_coverage(self, symbol, locale="ko-KR"):
        return self.daily_coverage

    def warm_company_daily_news_summaries(self, symbol, rows, days=30, limit=30, locale="ko-KR"):
        self.daily_warm_calls.append({"symbol": symbol, "rows": list(rows), "days": days, "limit": limit, "locale": locale})
        self.daily_rows = list(rows)[:limit]
        self.daily_coverage = {
            "symbol": symbol,
            "locale": locale,
            "days": days,
            "limit": limit,
            "rowCount": len(self.daily_rows),
            "coverageType": "complete",
        }
        return self.daily_rows


class ThemeClickHouseProvider:
    def __init__(self, rows_by_symbol):
        self.rows_by_symbol = rows_by_symbol
        self.calls = 0
        self.requested_symbols = []

    def news_articles(self, symbol, limit, days):
        self.calls += 1
        self.requested_symbols.append(symbol)
        return self.rows_by_symbol.get(symbol, [])


class FakeOntologyProvider:
    def __init__(self, evidence):
        self.evidence = evidence

    def fetch(self, request):
        return self.evidence


class BrokenRedisClient:
    def get(self, key):
        raise RuntimeError("redis unavailable")

    def setex(self, key, ttl, value):
        raise RuntimeError("redis unavailable")


class FakeRedisClient:
    def __init__(self):
        self.items = {}
        self.setex_calls = []

    def get(self, key):
        entry = self.items.get(key)
        return entry[1] if entry else None

    def setex(self, key, ttl, value):
        self.setex_calls.append((key, ttl, value))
        self.items[key] = (ttl, value)

    def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.items:
            return False
        self.items[key] = (ex, value)
        return True


class FakePublishRedisClient(FakeRedisClient):
    def __init__(self):
        super().__init__()
        self.publish_calls = []

    def publish(self, channel, payload):
        self.publish_calls.append((channel, payload))


class FakeAnalysisQueue:
    def __init__(self):
        self.envelopes = []

    def submit(self, envelope):
        self.envelopes.append(envelope)


class FakeClickHouseInsertClient:
    def __init__(self):
        self.insert_calls = []

    def insert_json_each_row(self, table, rows):
        self.insert_calls.append((table, rows))


class CountingNewsAgent(NewsAgent):
    def __init__(self, provider=None, localizer=None):
        super().__init__(provider=provider, localizer=localizer)
        self.calls = 0

    def analyze(self, context):
        self.calls += 1
        return super().analyze(context)


class CountingSynthesizer:
    def __init__(self):
        self.calls = 0

    def synthesize(self, **kwargs):
        self.calls += 1
        symbol = kwargs["symbol"]
        return FinalAnswer(
            title=f"{symbol} 뉴스 분석",
            summary=f"{symbol} 뉴스 근거를 정리했습니다.",
            sections=[],
            citations=[],
            limitations=[],
        )


class RoleAnswerOnlySynthesizer:
    def __init__(self):
        self.role_answer_calls = []

    def synthesize(self, **kwargs):
        raise AssertionError("multi-agent mode must not merge role answers through final synthesis")

    def synthesize_agent_answer(self, **kwargs):
        finding = kwargs["finding"]
        self.role_answer_calls.append(finding.role)
        return AgentAnswer(
            agentId=finding.agentId,
            role=finding.role,
            title=f"{finding.role} 독립 답변",
            content=f"{finding.role}가 독립적으로 작성한 답변입니다.",
            confidence=finding.confidence,
        )


class FailingUIAgent:
    def propose(self, *args, **kwargs):
        raise AssertionError("multi-agent chat mode must not call UIAgent")

    def propose_many(self, *args, **kwargs):
        raise AssertionError("multi-agent chat mode must not call UIAgent")


class InspectingDeepOrchestrator:
    def __init__(self, store):
        self.store = store
        self.seen_report = None
        self.delegate = AgentOrchestrator(store=store)

    def analyze(self, payload):
        self.seen_report = self.store.get(payload["requestId"])
        return self.delegate.analyze(payload)


class SlowNewsProvider:
    def __init__(self, delay_seconds=0.2):
        self.delay_seconds = delay_seconds

    def fetch(self, request):
        time.sleep(self.delay_seconds)
        return [
            EvidenceItem(
                provider="news",
                status="available",
                title="Slow news",
                summary="Slow news should timeout.",
                raw={"publishedAt": utc_now_iso(), "symbols": [request.symbol]},
            )
        ]


class SequencedDailyNewsProvider:
    def __init__(self):
        self.daily_calls = 0

    def fetch(self, request):
        return [
            EvidenceItem(
                provider="news",
                status="available",
                title="AAPL services growth",
                summary="Apple services revenue improved.",
                raw={
                    "articleId": "aapl-fallback-daily-1",
                    "publishedAt": utc_now_iso(),
                    "symbols": [request.symbol],
                    "dataSource": "test",
                },
            )
        ]

    def fetch_daily_summaries(self, request):
        self.daily_calls += 1
        if self.daily_calls == 1:
            return []
        return [
            {
                "date": "2026-07-01",
                "symbol": request.symbol,
                "summary": "fallback role daily summary",
                "keyPoints": ["role fallback"],
                "status": "final",
            }
        ]


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


def preset_layout_context():
    context = layout_context()
    context["presets"] = [
        {"id": "market", "kind": "default", "name": "시장분석", "aliases": ["시장분석", "시장분석 프리셋"]},
        {"id": "stock", "kind": "default", "name": "종목분석", "aliases": ["종목분석"]},
        {"id": "custom-taste", "kind": "custom", "name": "내 입맛", "aliases": ["내입맛", "내 입맛 프리셋"]},
    ]
    return context


def chart_only_layout_context(symbol="AAPL"):
    return {
        "version": 1,
        "selectedPanelId": "panel-chart-primary",
        "panels": [
            {
                "id": "panel-chart-primary",
                "type": "chart",
                "placement": {"group": "workspace", "zone": "mainContext", "col": 1, "row": 1, "colSpan": 4, "rowSpan": 4},
                "layoutPinned": False,
                "layoutWeight": 100,
                "minSpan": {"colSpan": 2, "rowSpan": 2},
                "maxSpan": {"colSpan": 4, "rowSpan": 5},
                "symbol": symbol,
                "props": {"symbol": symbol},
            },
        ],
    }


def grid_v2_layout_context(*, chart_symbol="AAPL", include_support=True, pinned_bottom=False):
    panels = []
    if include_support:
        panels.extend([
            {
                "id": "panel-news",
                "type": "newsFeed",
                "placement": {"group": "workspace", "zone": "main", "col": 1, "row": 1, "colSpan": 4, "rowSpan": 2},
                "layoutPinned": False,
                "layoutWeight": 50,
            },
            {
                "id": "panel-ontology",
                "type": "ontologyGraph",
                "placement": {"group": "workspace", "zone": "mainContext", "col": 5, "row": 1, "colSpan": 4, "rowSpan": 2},
                "layoutPinned": False,
                "layoutWeight": 45,
            },
        ])
    if chart_symbol:
        panels.append({
            "id": "panel-chart-primary",
            "type": "chart",
            "placement": {"group": "workspace", "zone": "mainContext", "col": 1, "row": 3, "colSpan": 8, "rowSpan": 3},
            "layoutPinned": False,
            "layoutWeight": 100,
            "minSpan": {"colSpan": 2, "rowSpan": 2},
            "maxSpan": {"colSpan": 8, "rowSpan": 5},
            "symbol": chart_symbol,
            "props": {"symbol": chart_symbol},
        })
    if pinned_bottom:
        panels.append({
            "id": "panel-pinned-summary",
            "type": "aiSummary",
            "placement": {"group": "workspace", "zone": "mainContext", "col": 1, "row": 4, "colSpan": 8, "rowSpan": 2},
            "layoutPinned": True,
            "layoutWeight": 70,
        })
    return {
        "version": 2,
        "grid": {"cols": 8, "rows": 5},
        "selectedPanelId": "panel-chart-primary" if chart_symbol else "panel-news",
        "panels": panels,
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


def assert_no_grid_gaps(testcase, placements, *, cols=8, rows=5):
    cells = {}
    for item in placements:
        placement = item["placement"]
        if placement.get("group") != "workspace":
            continue
        for col in range(placement["col"], placement["col"] + placement["colSpan"]):
            for row in range(placement["row"], placement["row"] + placement["rowSpan"]):
                testcase.assertGreaterEqual(col, 1)
                testcase.assertLessEqual(col, cols)
                testcase.assertGreaterEqual(row, 1)
                testcase.assertLessEqual(row, rows)
                cells[(col, row)] = cells.get((col, row), 0) + 1
    testcase.assertFalse([cell for cell, count in cells.items() if count > 1])
    testcase.assertEqual(len(cells), cols * rows)


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
                "AGENT_ALLOW_RUNTIME_NEWS_OPENAI",
                "AGENT_GRAPHDB_TIMEOUT_MS",
                "AGENT_MAX_REALTIME_LLM_CALLS",
                "AGENT_NEWS_LOCALIZATION_PROVIDER",
                "AGENT_NEWS_ROLE_ANALYSIS_PROVIDER",
                "AGENT_GRAPH_EXPANSION_CACHE_ENABLED",
                "AGENT_CROSS_SIGNAL_ENABLED",
                "AGENT_REPORT_TTL_SECONDS",
                "AGENT_ROUTER_PROVIDER",
                "AGENT_INTENT_CLASSIFIER_ALWAYS",
                "AGENT_SNAPSHOT_TIMEOUT_MS",
                "AGENT_EXPANDED_RETRIEVAL_ENABLED",
                "AGENT_INTENT_CLASSIFIER_PROVIDER",
                "AGENT_INTENT_CLASSIFIER_URL",
                "AGENT_INTENT_CLASSIFIER_TIMEOUT_SECONDS",
                "AGENT_QUERY_UNDERSTANDING_TIMEOUT_MS",
                "AGENT_QUERY_UNDERSTANDING_EVENTS_TOPIC",
                "AGENT_MAX_RELATED_SYMBOLS",
                "AGENT_MAX_RELATED_THEMES",
                "AGENT_MAX_NEWS_ITEMS_TOTAL",
                "AGENT_MAX_MARKET_PEERS",
                "AGENT_UI_ROUTER_PROVIDER",
                "AGENT_USE_SNAPSHOT_HOT_PATH",
            ]
        }

    def tearDown(self):
        for name, value in self._env_backup.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_reference_evidence_anchors_chart_and_news_context(self):
        context = AgentContext(
            symbol="NVDA",
            intent="이 뉴스랑 여기 봉이 연결돼?",
            references=[
                {
                    "type": "chart.candle",
                    "data": {
                        "symbol": "NVDA",
                        "interval": "1D",
                        "timestamp": "2026-07-04T00:00:00Z",
                        "close": 145.5,
                    },
                },
                {
                    "type": "news.article",
                    "data": {
                        "symbol": "NVDA",
                        "title": "Nvidia supply headline",
                        "summary": "Supply chain update affected chip stocks.",
                        "publishedAt": "2026-07-04T13:30:00Z",
                        "url": "https://example.com/nvda",
                    },
                },
            ],
        )

        chart_items = chart_reference_evidence(context)
        news_items = news_reference_evidence(context)

        self.assertEqual(len(chart_items), 1)
        self.assertEqual(chart_items[0].provider, "market-data")
        self.assertIn("145.5", chart_items[0].summary)
        self.assertEqual(len(news_items), 1)
        self.assertEqual(news_items[0].provider, "news")
        self.assertEqual(news_items[0].title, "Nvidia supply headline")
        self.assertEqual(news_items[0].url, "https://example.com/nvda")

    def test_operation_extractor_links_news_reference_to_chart_reference(self):
        references = [
            {
                "type": "chart.candle",
                "displayLabel": "NVDA 1D 2026-07-04",
                "data": {"symbol": "NVDA", "interval": "1D", "timestamp": "2026-07-04T00:00:00Z", "close": 145.5},
            },
            {
                "type": "news.article",
                "displayLabel": "NVDA supply headline",
                "data": {"symbol": "NVDA", "title": "NVDA supply headline", "publishedAt": "2026-07-04T13:30:00Z"},
            },
        ]

        operation_ir = build_agent_operation_ir(
            intent="이 뉴스랑 여기 봉 하락이 연결된 거야?",
            symbol="NVDA",
            references=references,
            ui_context={},
            chart_context={"chartDocument": {"symbol": "NVDA", "timeframe": "1D"}},
        )

        self.assertEqual(operation_ir["operations"][0]["type"], "link_news_to_price_move")
        self.assertEqual(operation_ir["suggestedRoles"], ["chart", "news", "ontology"])
        self.assertEqual(operation_ir["contextWindow"]["requiredSnapshots"], ["chart_analysis_snapshot", "news_snapshot", "relationship_snapshot"])
        self.assertGreaterEqual(operation_ir["confidence"], 0.9)

    def test_operation_reference_normalization_dedupes_chart_context_reference(self):
        reference = {
            "type": "chart.candle",
            "displayLabel": "NVDA 1D 2026-07-04",
            "data": {"symbol": "NVDA", "interval": "1D", "timestamp": "2026-07-04T00:00:00Z", "close": 145.5},
        }

        references = normalize_operation_references(
            [reference],
            {"selectedReference": reference},
            {"selectedReference": reference, "hoverReference": reference},
        )

        self.assertEqual(len(references), 1)
        self.assertEqual(references[0]["type"], "chart.candle")

    def test_llm_budget_reserves_single_call_for_final_synthesis(self):
        timing = {}
        context = RuntimeRunContext(policy=RuntimePolicy(max_realtime_llm_calls=1), timing=timing)

        self.assertFalse(context.acquire_llm("intent-classifier"))
        self.assertTrue(context.acquire_llm("synthesis"))
        self.assertEqual(timing["llmCalls"], 1)
        self.assertEqual(timing["llmBudgetBlocked"], 1)
        self.assertEqual(timing["llmCallLabels"], ["synthesis"])

    def test_operation_planner_fallback_returns_structured_ir(self):
        base_ir = build_agent_operation_ir(
            intent="이거랑 저거 같이 봐줘",
            symbol="NVDA",
            references=[],
            ui_context={},
            chart_context={"chartDocument": {"symbol": "NVDA", "timeframe": "1D"}},
        )
        self.assertTrue(needs_operation_planner(base_ir))

        planned = maybe_plan_operation_ir(
            base_ir=base_ir,
            intent="이거랑 저거 같이 봐줘",
            symbol="NVDA",
            references=[],
            ui_context={},
            chart_context={"chartDocument": {"symbol": "NVDA", "timeframe": "1D"}},
            response_requester=lambda _payload: {
                "output_text": json.dumps({
                    "operations": [
                        {
                            "kind": "analysis",
                            "type": "explain_price_move",
                            "target": None,
                            "visible": None,
                            "priceSource": None,
                            "requiredSources": ["market", "news", "macro"],
                            "applyMode": None,
                            "timeRange": {"dateHints": []},
                            "confidence": 0.78,
                        }
                    ],
                    "ambiguities": [],
                    "confidence": 0.78,
                    "reason": "Contextual analysis request needs market, news, and macro evidence.",
                })
            },
        )

        self.assertIsNotNone(planned)
        assert planned is not None
        self.assertEqual(planned["source"], "operation-planner-openai")
        self.assertEqual(planned["operations"][0]["type"], "explain_price_move")
        self.assertEqual(planned["suggestedRoles"], ["chart", "news", "macro"])
        self.assertEqual(planned["contextWindow"]["requiredSnapshots"], ["chart_analysis_snapshot", "news_snapshot", "macro_snapshot"])

    def test_normalize_request_operation_ir_prevents_reference_query_clarify(self):
        state = normalize_request_state({
            "request": {
                "symbol": "NVDA",
                "intent": "이 뉴스랑 여기 봉 하락이 연결된 거야?",
                "references": [
                    {
                        "type": "chart.candle",
                        "data": {"symbol": "NVDA", "interval": "1D", "timestamp": "2026-07-04T00:00:00Z", "close": 145.5},
                    },
                    {
                        "type": "news.article",
                        "data": {"symbol": "NVDA", "title": "NVDA supply headline", "publishedAt": "2026-07-04T13:30:00Z"},
                    },
                ],
            }
        })

        understanding = state["query_understanding"]
        self.assertEqual(understanding["operationIR"]["operations"][0]["type"], "link_news_to_price_move")
        self.assertNotEqual(understanding["routeMode"], "clarify")
        self.assertIn("chart", understanding["selectedRoles"])
        self.assertIn("news", understanding["selectedRoles"])
        self.assertIn("ontology", understanding["selectedRoles"])

    def test_analysis_cache_key_changes_for_different_references(self):
        route = IntentRoute(
            source="operation-extractor",
            intentType="link_news_to_price_move",
            selectedRoles=["chart", "news"],
            confidence=0.92,
            reason="test",
        )
        base_context = AgentContext(symbol="NVDA", intent="이 뉴스랑 여기 봉 연결돼?")
        base_state = {
            "symbol": "NVDA",
            "intent": "이 뉴스랑 여기 봉 연결돼?",
            "request": {"routerMode": "hybrid"},
            "route": route,
            "route_mode": "analysis",
            "events": [],
            "news_symbols": [],
            "analysis_mode": "auto",
            "context": base_context,
        }
        first_context = AgentContext(
            symbol="NVDA",
            intent="이 뉴스랑 여기 봉 연결돼?",
            references=[{"type": "news.article", "data": {"symbol": "NVDA", "title": "A", "publishedAt": "2026-07-04T13:30:00Z"}}],
            operationIR={"operations": [{"kind": "analysis", "type": "link_news_to_price_move", "requiredSources": ["market", "news"]}], "suggestedRoles": ["chart", "news"]},
        )
        second_context = AgentContext(
            symbol="NVDA",
            intent="이 뉴스랑 여기 봉 연결돼?",
            references=[{"type": "news.article", "data": {"symbol": "NVDA", "title": "B", "publishedAt": "2026-07-05T13:30:00Z"}}],
            operationIR={"operations": [{"kind": "analysis", "type": "link_news_to_price_move", "requiredSources": ["market", "news"]}], "suggestedRoles": ["chart", "news"]},
        )

        first_key = analysis_cache_key_for_state({**base_state, "context": first_context})
        second_key = analysis_cache_key_for_state({**base_state, "context": second_context})

        self.assertIsNotNone(first_key)
        self.assertIsNotNone(second_key)
        self.assertNotEqual(first_key, second_key)

    def test_reference_market_evidence_is_exposed_in_provider_evidence(self):
        orchestrator = AgentOrchestrator(analysis_cache=MemoryAgentAnalysisCache())

        report = orchestrator.analyze({
            "symbol": "NVDA",
            "intent": "이 뉴스랑 여기 봉 하락이 연결된 거야?",
            "references": [
                {
                    "type": "chart.candle",
                    "data": {
                        "symbol": "NVDA",
                        "interval": "1D",
                        "timestamp": "2026-07-04T00:00:00Z",
                        "open": 149.0,
                        "high": 150.0,
                        "low": 144.0,
                        "close": 145.5,
                        "volume": 100,
                    },
                },
                {
                    "type": "news.article",
                    "data": {
                        "symbol": "NVDA",
                        "title": "NVDA supply headline",
                        "summary": "Supply chain update affected chip stocks.",
                        "publishedAt": "2026-07-04T13:30:00Z",
                    },
                },
            ],
        })

        self.assertTrue(any(item.provider == "market-data" and item.raw.get("referenceIndex") == 0 for item in report.providerEvidence))
        self.assertTrue(any(item.provider == "news" and item.raw.get("referenceIndex") == 1 for item in report.providerEvidence))
        self.assertEqual(report.agentTrace["operationIR"]["operations"][0]["type"], "link_news_to_price_move")

    def test_query_understanding_fanout_runs_branches_in_parallel(self):
        branch_calls = []

        def slow_entity(query, chart_context=None):
            branch_calls.append("entity")
            time.sleep(0.12)
            return EntityResolution(status="not_found", needs_clarification=False, reason="test")

        def slow_content(query):
            branch_calls.append("content_rules")
            time.sleep(0.12)
            return []

        def slow_ui(query, layout_context):
            branch_calls.append("ui_rules")
            time.sleep(0.12)
            return []

        started_at = time.perf_counter()
        timing = {}
        with patch("gops_agents.intent_understanding.fanout.resolve_entity", side_effect=slow_entity):
            with patch("gops_agents.intent_understanding.fanout.deterministic_content_tasks", side_effect=slow_content):
                with patch("gops_agents.intent_understanding.fanout.deterministic_ui_tasks", side_effect=slow_ui):
                    understanding, entity_resolution = build_query_understanding("애플 뉴스 보여줘", timing=timing)
        elapsed_seconds = time.perf_counter() - started_at

        self.assertCountEqual(branch_calls, ["entity", "content_rules", "ui_rules"])
        self.assertLess(elapsed_seconds, 0.25)
        self.assertEqual(entity_resolution.status, "not_found")
        self.assertEqual(understanding.routeMode, "clarify")
        self.assertGreater(timing["queryUnderstandingMs"], 0)
        self.assertGreater(timing["entityResolveMs"], 0)
        self.assertTrue(timing["intentClassifierRequired"])

    def test_ui_only_query_understanding_returns_before_slow_entity_branch(self):
        def slow_entity(query, chart_context=None):
            time.sleep(0.3)
            return EntityResolution(status="not_found", needs_clarification=False, reason="slow entity")

        def slow_content(query):
            time.sleep(0.3)
            return []

        timing = {}
        started_at = time.perf_counter()
        with patch("gops_agents.intent_understanding.fanout.resolve_entity", side_effect=slow_entity):
            with patch("gops_agents.intent_understanding.fanout.deterministic_content_tasks", side_effect=slow_content):
                with patch(
                    "gops_agents.intent_understanding.fanout.classify_with_provider",
                    side_effect=AssertionError("clear UI route should not call classifier"),
                ):
                    understanding, entity_resolution = build_query_understanding(
                        "뉴스 패널 크게 보여줘",
                        layout_context=layout_context(),
                        timing=timing,
                    )
        elapsed_seconds = time.perf_counter() - started_at

        self.assertLess(elapsed_seconds, 0.2)
        self.assertEqual(entity_resolution.status, "not_found")
        self.assertEqual(entity_resolution.reason, "entity resolver skipped after high-confidence UI-only intent")
        self.assertEqual(understanding.routeMode, "ui_layout")
        self.assertEqual(understanding.intentType, "ui-layout")
        self.assertEqual(understanding.uiTasks[0].targetPanelType, "newsFeed")
        self.assertEqual(timing["queryUnderstandingEarlyReturn"], "ui_only")
        self.assertNotIn("entity_timeout", understanding.warnings)

    def test_entity_timeout_with_subject_fallback_does_not_force_classifier(self):
        def slow_entity(query, chart_context=None):
            time.sleep(0.2)
            return EntityResolution(status="not_found", needs_clarification=False, reason="slow entity")

        with patch.dict(os.environ, {"AGENT_QUERY_UNDERSTANDING_TIMEOUT_MS": "50"}, clear=False):
            with patch("gops_agents.intent_understanding.fanout.resolve_entity", side_effect=slow_entity):
                with patch(
                    "gops_agents.intent_understanding.fanout.classify_with_provider",
                    side_effect=AssertionError("content route with subject fallback should not call classifier"),
                ):
                    for label, kwargs in [
                        ("request symbol", {"request_symbol": "NVDA"}),
                        ("chart context", {"chart_context": {"chartDocument": {"symbol": "NVDA"}}}),
                    ]:
                        with self.subTest(label=label):
                            timing = {}
                            understanding, _ = build_query_understanding(
                                "뉴스 보여줘",
                                timing=timing,
                                **kwargs,
                            )

                            self.assertEqual(understanding.routeMode, "analysis")
                            self.assertEqual(understanding.intentType, "news")
                            self.assertEqual([task.taskType for task in understanding.contentTasks], ["news"])
                            self.assertIn("entity_timeout", understanding.warnings)
                            self.assertNotIn("intentClassifierRequired", timing)

    def test_classifier_payload_preserves_multi_panel_task(self):
        result = classifier_result_from_payload({
            "uiTasks": [
                {
                    "action": "open",
                    "targetPanelTypes": ["chart", "newsFeed", "aiSummary"],
                    "layoutPreset": "default_workspace",
                    "confidence": 0.88,
                    "reason": "Open the default panel set.",
                }
            ],
            "routeMode": "ui_layout",
            "confidence": 0.88,
        }, source="test-classifier")

        self.assertIsNotNone(result)
        self.assertEqual(result.uiTasks[0].targetPanelTypes, ["chart", "newsFeed", "aiSummary"])
        self.assertEqual(result.uiTasks[0].layoutPreset, "default_workspace")
        self.assertEqual(result.routeMode, "ui_layout")

    def test_ui_parser_loads_named_default_layout_preset(self):
        parsed = parse_ui_query("시장분석 프리셋 띄워줘", preset_layout_context())

        self.assertEqual(len(parsed.tasks), 1)
        self.assertEqual(parsed.tasks[0].action, "load")
        self.assertEqual(parsed.tasks[0].presetId, "market")
        self.assertEqual(parsed.tasks[0].presetName, "시장분석")

    def test_ui_parser_loads_default_layout_preset_without_preset_word(self):
        for query in ("시장분석 보여줘", "시장분석창 보여줘", "시장분석 대시보드 열어줘"):
            with self.subTest(query=query):
                parsed = parse_ui_query(query, preset_layout_context())

                self.assertEqual(len(parsed.tasks), 1)
                self.assertEqual(parsed.tasks[0].action, "load")
                self.assertEqual(parsed.tasks[0].presetId, "market")
                self.assertEqual(parsed.tasks[0].presetName, "시장분석")

    def test_ui_parser_loads_custom_layout_preset_without_llm(self):
        with patch("gops_agents.intent_understanding.fanout.classify_with_provider", side_effect=AssertionError("preset load should not call classifier")):
            understanding, _ = build_query_understanding(
                "내입맛 띠워줘",
                layout_context=preset_layout_context(),
            )

        self.assertEqual(understanding.routeMode, "ui_layout")
        self.assertEqual(understanding.contentTasks, [])
        self.assertEqual(understanding.uiTasks[0].action, "load")
        self.assertEqual(understanding.uiTasks[0].presetId, "custom-taste")

    def test_ui_parser_loads_any_runtime_custom_preset_name_without_llm(self):
        context = preset_layout_context()
        context["presets"].append({"id": "custom-preopen", "kind": "custom", "name": "장전 체크", "aliases": []})

        with patch("gops_agents.intent_understanding.fanout.classify_with_provider", side_effect=AssertionError("runtime custom preset load should not call classifier")):
            understanding, _ = build_query_understanding(
                "장전 체크 대시보드 열어줘",
                layout_context=context,
            )

        self.assertEqual(understanding.routeMode, "ui_layout")
        self.assertEqual(understanding.contentTasks, [])
        self.assertEqual(understanding.uiTasks[0].action, "load")
        self.assertEqual(understanding.uiTasks[0].presetId, "custom-preopen")
        self.assertEqual(understanding.uiTasks[0].presetName, "장전 체크")

    def test_ui_parser_does_not_treat_analysis_request_as_preset_load(self):
        parsed = parse_ui_query("시장 분석해줘", preset_layout_context())

        self.assertEqual(parsed.tasks, [])

        compact_preset_name = parse_ui_query("시장분석 해줘", preset_layout_context())

        self.assertEqual(compact_preset_name.tasks, [])

    def test_ui_parser_marks_ambiguous_preset_names_for_clarification(self):
        context = preset_layout_context()
        context["presets"] = [
            {"id": "taste-1", "kind": "custom", "name": "내 입맛", "aliases": ["내입맛"]},
            {"id": "taste-2", "kind": "custom", "name": "내입맛", "aliases": ["내 입맛"]},
        ]

        parsed = parse_ui_query("내입맛 프리셋 띄워줘", context)

        self.assertEqual(parsed.tasks, [])
        self.assertTrue(parsed.needs_classifier)
        self.assertIn("ui_preset_match_ambiguous", parsed.warnings)

    def test_clear_query_understanding_does_not_call_llm_classifier(self):
        with patch("gops_agents.intent_understanding.fanout.classify_with_provider", side_effect=AssertionError("clear route should not call classifier")):
            understanding, _ = build_query_understanding("NVDA 뉴스 보여줘")

        self.assertEqual(understanding.routeMode, "analysis")
        self.assertEqual(understanding.intentType, "news")
        self.assertEqual(understanding.uiTasks, [])

    def test_content_news_show_query_with_layout_context_becomes_ui_task(self):
        with patch("gops_agents.intent_understanding.fanout.classify_with_provider", side_effect=AssertionError("content display should not call classifier")):
            understanding, _ = build_query_understanding(
                "NVDA 뉴스 보여줘",
                layout_context=layout_context(),
                layout_command_preflight=True,
            )

        self.assertEqual(understanding.routeMode, "ui_layout")
        self.assertEqual(understanding.intentType, "ui-layout")
        self.assertEqual(understanding.contentTasks, [])
        self.assertEqual(understanding.uiTasks[0].targetPanelType, "newsFeed")
        self.assertEqual(understanding.uiTasks[0].source, "content-display-rule")

    def test_news_analysis_query_with_layout_context_stays_analysis(self):
        with patch("gops_agents.intent_understanding.fanout.classify_with_provider", side_effect=AssertionError("clear analysis route should not call classifier")):
            understanding, _ = build_query_understanding("NVDA 뉴스 분석해줘", layout_context=layout_context())

        self.assertEqual(understanding.routeMode, "analysis")
        self.assertEqual(understanding.intentType, "news")
        self.assertEqual([task.taskType for task in understanding.contentTasks], ["news"])
        self.assertEqual(understanding.uiTasks, [])

    def test_ui_parser_handles_korean_typos_without_llm_classifier(self):
        with patch("gops_agents.intent_understanding.fanout.classify_with_provider", side_effect=AssertionError("clear typo UI route should not call classifier")):
            understanding, _ = build_query_understanding("뉴스 페널 크개 띠워줘", layout_context=layout_context())

        self.assertEqual(understanding.routeMode, "ui_layout")
        self.assertEqual(understanding.intentType, "ui-layout")
        self.assertEqual(understanding.uiTasks[0].source, "ui-parser")
        self.assertEqual(understanding.uiTasks[0].targetPanelType, "newsFeed")
        self.assertEqual(understanding.uiTasks[0].targetPanelId, "panel-news")
        self.assertEqual(understanding.uiTasks[0].action, "resize")
        self.assertEqual(understanding.uiTasks[0].sizeIntent, "large")

    def test_ui_parser_handles_keep_only_panel_request_without_llm_classifier(self):
        with patch("gops_agents.intent_understanding.fanout.classify_with_provider", side_effect=AssertionError("clear keep-only UI route should not call classifier")):
            understanding, _ = build_query_understanding("차트 패널 빼고 나머지 패널 없애줘", layout_context=layout_context())

        self.assertEqual(understanding.routeMode, "ui_layout")
        self.assertEqual(understanding.intentType, "ui-layout")
        self.assertEqual(understanding.uiTasks[0].source, "ui-parser")
        self.assertEqual(understanding.uiTasks[0].action, "keep")
        self.assertEqual(understanding.uiTasks[0].targetPanelType, "chart")
        self.assertEqual(understanding.uiTasks[0].targetPanelId, "panel-chart-primary")

    def test_ui_parser_handles_keep_only_delete_variants_without_size_fuzzy(self):
        variants = [
            "차트 패널 빼고 다 지워줘",
            "차트 빼고 다 삭제해줘",
            "차트 패널 빼고 나머지 패널 치워줘",
        ]
        for query in variants:
            with self.subTest(query=query):
                with patch("gops_agents.intent_understanding.fanout.classify_with_provider", side_effect=AssertionError("clear delete keep-only UI route should not call classifier")):
                    understanding, _ = build_query_understanding(query, layout_context=layout_context())

                self.assertEqual(understanding.routeMode, "ui_layout")
                self.assertEqual(understanding.intentType, "ui-layout")
                self.assertEqual(understanding.uiTasks[0].source, "ui-parser")
                self.assertEqual(understanding.uiTasks[0].action, "keep")
                self.assertEqual(understanding.uiTasks[0].targetPanelType, "chart")
                self.assertEqual(understanding.uiTasks[0].targetPanelId, "panel-chart-primary")
                self.assertIsNone(understanding.uiTasks[0].sizeIntent)

    def test_ui_parser_treats_single_panel_delete_as_close_not_resize(self):
        with patch("gops_agents.intent_understanding.fanout.classify_with_provider", side_effect=AssertionError("clear panel delete UI route should not call classifier")):
            understanding, _ = build_query_understanding("주문 패널 지워줘", layout_context=layout_context())

        self.assertEqual(understanding.routeMode, "ui_layout")
        self.assertEqual(understanding.intentType, "ui-layout")
        self.assertEqual(understanding.uiTasks[0].source, "ui-parser")
        self.assertEqual(understanding.uiTasks[0].action, "close")
        self.assertEqual(understanding.uiTasks[0].targetPanelType, "orderTicket")
        self.assertEqual(understanding.uiTasks[0].targetPanelId, "panel-order")
        self.assertIsNone(understanding.uiTasks[0].sizeIntent)

    def test_ui_parser_preserves_resize_and_content_except_phrasing(self):
        with patch("gops_agents.intent_understanding.fanout.classify_with_provider", side_effect=AssertionError("clear resize UI route should not call classifier")):
            resize_understanding, _ = build_query_understanding("뉴스 패널 줄여줘", layout_context=layout_context())

        self.assertEqual(resize_understanding.routeMode, "ui_layout")
        self.assertEqual(resize_understanding.uiTasks[0].action, "resize")
        self.assertEqual(resize_understanding.uiTasks[0].sizeIntent, "small")

        parsed = parse_ui_query("차트 빼고 분석해줘", layout_context())
        self.assertEqual(parsed.tasks, [])

    def test_ui_parser_does_not_treat_market_analysis_phrasing_as_layout(self):
        cases = [
            "오늘 왜 떨어졌어?",
            "오늘 주가 왜 떨어졌어>",
            "주가 변동 원인 알려줘",
            "이 뉴스랑 여기 봉 하락이 연결된 거야?",
        ]
        for query in cases:
            with self.subTest(query=query):
                parsed = parse_ui_query(query, layout_context())
                self.assertEqual(parsed.tasks, [])
                self.assertFalse(parsed.needs_classifier)

    def test_mixed_ui_news_and_ontology_scopes_ui_clause(self):
        query = "뉴스 패널 크게 띄워주고 엔비디아 뉴스 불러와줘. 그리고 온톨로지 분석해줘"
        with patch("gops_agents.intent_understanding.fanout.classify_with_provider", side_effect=AssertionError("clear mixed route should not call classifier")):
            understanding, _ = build_query_understanding(query, layout_context=layout_context())

        self.assertEqual(understanding.routeMode, "hybrid")
        self.assertEqual(understanding.intentType, "news+ontology")
        self.assertEqual(understanding.selectedRoles, ["news", "ontology"])
        self.assertEqual(len(understanding.uiTasks), 1)
        self.assertEqual(understanding.uiTasks[0].targetPanelType, "newsFeed")
        self.assertEqual(understanding.uiTasks[0].targetPanelTypes, ["newsFeed"])
        self.assertNotIn("ontologyGraph", understanding.uiTasks[0].targetPanelTypes)

    def test_ontology_analysis_query_does_not_become_ui_task(self):
        with patch("gops_agents.intent_understanding.fanout.classify_with_provider", side_effect=AssertionError("content-only ontology should not call classifier")):
            understanding, _ = build_query_understanding("온톨로지 분석해줘", layout_context=layout_context())

        self.assertEqual(understanding.routeMode, "analysis")
        self.assertEqual(understanding.intentType, "ontology")
        self.assertEqual(understanding.uiTasks, [])

    def test_incomplete_ui_surface_uses_llm_classifier_fallback(self):
        classifier_payload = classifier_result_from_payload({
            "uiTasks": [
                {
                    "action": "focus",
                    "targetPanelType": "newsFeed",
                    "targetPanelId": "panel-news",
                    "confidence": 0.86,
                    "reason": "LLM recovered incomplete news panel UI request.",
                }
            ],
            "routeMode": "ui_layout",
            "confidence": 0.86,
        }, source="test-llm")

        with patch("gops_agents.intent_understanding.fanout.classify_with_provider", return_value=classifier_payload) as classifier:
            understanding, _ = build_query_understanding("뉴스 패널 좀", layout_context=layout_context())

        classifier.assert_called_once()
        self.assertEqual(understanding.routeMode, "ui_layout")
        self.assertEqual(understanding.uiTasks[0].source, "test-llm")

    def test_unclear_query_understanding_uses_llm_classifier_fallback(self):
        classifier_payload = classifier_result_from_payload({
            "contentTasks": [{"taskType": "news", "confidence": 0.88, "reason": "LLM recovered unclear news intent."}],
            "routeMode": "analysis",
            "confidence": 0.88,
        }, source="test-llm")

        with patch("gops_agents.intent_understanding.fanout.classify_with_provider", return_value=classifier_payload) as classifier:
            understanding, _ = build_query_understanding("이거 좀 봐줘")

        classifier.assert_called_once()
        self.assertEqual(understanding.routeMode, "analysis")
        self.assertEqual(understanding.intentType, "news")
        self.assertEqual(understanding.source, "test-llm")

    def test_gops_agents_top_level_keeps_only_package_boundaries(self):
        allowed = {"__init__.py", "orchestrator.py", "alert_commands.py"}
        top_level_files = {
            path.name
            for path in (ROOT / "shared" / "gops_agents").iterdir()
            if path.is_file() and path.suffix == ".py"
        }
        self.assertEqual(top_level_files, allowed)

    def test_orchestrator_uses_active_chart_for_generic_analyze_without_llm(self):
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
        self.assertEqual(report.route.intentType, "chart")
        self.assertEqual(report.route.selectedRoles, ["chart"])
        self.assertIsNotNone(report.finalAnswer)
        self.assertEqual(report.finalAnswer.title, "NVDA 차트 해설")
        self.assertEqual(report.routePlan.analysisQueryType, "chart_overview")
        self.assertEqual(report.timing["llmCalls"], 0)
        self.assertTrue(any(item.provider == "chart-analysis" for item in report.providerEvidence))
        self.assertIsNotNone(report.chartExplanation)
        self.assertIsNone(report.notificationDecision)

    def test_orchestrator_analyzes_selected_mu_candle_without_entity_fallback_or_llm(self):
        candles = [
            {"timestamp": "2026-07-13T00:00:00Z", "open": 120, "high": 124, "low": 119, "close": 123, "volume": 1_000},
            {"timestamp": "2026-07-14T00:00:00Z", "open": 124, "high": 126, "low": 116, "close": 118, "volume": 2_200},
        ]
        reference = {
            "type": "chart.candle",
            "sourcePanelId": "mu-chart",
            "data": {"symbol": "MU", "interval": "1D", **candles[-1]},
        }
        report = AgentOrchestrator().analyze({
            "symbol": "MU",
            "intent": "analysis",
            "messages": [{"role": "user", "content": "이 봉 분석해줘"}],
            "references": [reference],
            "chartContext": {
                "chartDocument": {"symbol": "MU", "timeframe": "1D", "sourcePanelId": "mu-chart"},
                "candles": candles,
                "selectedReference": reference,
            },
        })

        self.assertEqual(report.symbol, "MU")
        self.assertEqual(report.route.intentType, "chart")
        self.assertEqual(report.routePlan.analysisQueryType, "reference_anchor_analysis")
        self.assertEqual(report.timing["llmCalls"], 0)
        self.assertEqual(report.chartExplanation["facts"]["selectedCandle"]["timestamp"], "2026-07-14T00:00:00Z")
        self.assertEqual(report.chartExplanation["facts"]["selectedCandle"]["close"], 118.0)
        self.assertEqual(report.finalAnswer.title, "MU 선택 봉 분석")
        self.assertNotIn("ABBV", report.finalAnswer.summary)

    def test_chart_narrator_describes_confirmed_channel_exit_as_long_exit_scenario(self):
        from gops_agents.chart_intelligence import build_chart_explanation, build_chart_final_answer

        asset = {
            "symbol": "MU", "interval": "1D", "asOf": "2026-07-14T00:00:00Z",
            "algorithmVersion": "ohlcv-consensus-pattern-families-v4", "inputDigest": "mu-fixture",
            "coverage": {"state": "full", "qualityFlags": []},
            "geometry": {
                "primaryPattern": {
                    "id": "mu-channel", "kind": "ascending_channel_breakdown", "state": "confirmed",
                    "breakoutDirection": "down", "score": 0.91, "touches": 5,
                    "confirmation": {"breakoutAt": "2026-07-13T00:00:00Z", "confirmedAt": "2026-07-14T00:00:00Z", "mode": "next_close_hold"},
                },
                "supports": [{"id": "support-1", "price": 116.0}],
                "resistances": [{"id": "resistance-1", "price": 126.0}],
                "drawings": [{"id": "drawing-mu-channel"}],
                "tradePlan": {
                    "action": "sell_candidate", "direction": "exit_long", "signalAt": "2026-07-14T00:00:00Z",
                    "entryPrice": 118.0, "stopPrice": 122.0, "targetPrice": 110.0, "rewardRiskRatio": 2.0,
                    "reasons": ["confirmed_downward_breakout", "long_position_exit_only"],
                },
            },
            "indicators": {"sma60": 121.0, "sma120": 119.0, "cross": {"status": "none"}},
        }
        context = types.SimpleNamespace(symbol="MU", chartContext={"chartDocument": {"symbol": "MU", "timeframe": "1D"}}, references=[])
        explanation = build_chart_explanation(context, asset)
        answer = build_chart_final_answer("MU", explanation)

        self.assertEqual(explanation["facts"]["pattern"]["label"], "상승 채널 하단 이탈")
        self.assertEqual(explanation["facts"]["pattern"]["stateLabel"], "돌파 확인")
        self.assertEqual(explanation["facts"]["tradeScenario"]["positionMeaning"], "기존 long 포지션의 매도·청산 검토 시나리오")
        rendered = " ".join([answer.summary, *(bullet for section in answer.sections for bullet in section.bullets)])
        self.assertIn("매도·청산 후보", rendered)
        self.assertIn("기존 long 포지션", rendered)
        self.assertNotIn("공매도 검토 후보", rendered)

    def test_time_aligned_news_received_after_anchor_is_followup_not_cause_candidate(self):
        from gops_agents.retrieval.snapshots import align_news_evidence

        context = types.SimpleNamespace(references=[{
            "type": "chart.candle",
            "data": {"symbol": "MU", "interval": "1h", "timestamp": "2026-07-14T13:00:00Z"},
        }])
        evidence = [EvidenceItem(
            provider="news", status="available", title="Delayed article", summary="stored after candle close",
            observedAt="2026-07-14T12:30:00Z",
            raw={"publishedAt": "2026-07-14T12:30:00Z", "availableAt": "2026-07-14T14:10:00Z"},
        )]

        aligned = align_news_evidence(evidence, context)

        self.assertEqual(aligned[0].raw["temporalRelation"], "after")
        self.assertGreater(aligned[0].raw["availabilityLagSeconds"], 0)
        self.assertEqual(aligned[0].raw["causality"], "temporal_association_only")

    def test_memory_report_store_round_trip(self):
        store = InMemoryReportStore()
        report = AgentOrchestrator(store=store).analyze({"symbol": "NVDA", "intent": "뉴스 보여줘"})

        stored = store.get(report.analysisId)

        self.assertIsNotNone(stored)
        self.assertEqual(stored.analysisId, report.analysisId)
        self.assertEqual(stored.symbol, "NVDA")

    def test_user_pii_is_redacted_across_serialized_report(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "NVDA 뉴스 보여줘 contact pii.person@example.com 010-1234-5678 900101-1234567",
            "messages": [{"role": "user", "content": "token=sk-testsecret12345"}],
            "chartContext": {
                "chartDocument": {"symbol": "NVDA", "timeframe": "1m", "sourceUrl": "https://example.com/chart?token=secret#debug"},
                "visibleSummary": {"lastPrice": "120.00", "change": "+2.10%"},
                "dataStatus": {"candleCount": 10, "state": "ready"},
            },
        })

        payload = report.to_dict()
        encoded = json.dumps(payload, ensure_ascii=False, default=str)

        self.assertNotIn("pii.person@example.com", encoded)
        self.assertNotIn("010-1234-5678", encoded)
        self.assertNotIn("900101-1234567", encoded)
        self.assertNotIn("sk-testsecret12345", encoded)
        self.assertNotIn("token=secret", encoded)
        self.assertIn("[REDACTED_EMAIL]", encoded)
        self.assertIn("[REDACTED_PHONE]", encoded)
        self.assertIn("[REDACTED_KR_RRN]", encoded)
        self.assertIn("pii_redacted", payload.get("finalResponse", {}).get("risk_warnings", []))

    def test_report_to_dict_recursively_redacts_nested_sensitive_fields(self):
        report = AgentOrchestrator().analyze({"symbol": "NVDA", "intent": "뉴스 보여줘"})
        report.agentTrace["debug"] = {
            "authorization": "Bearer secret-token",
            "nested": [{"url": "https://example.com/path?api_key=secret#fragment"}],
        }

        encoded = json.dumps(report.to_dict(), ensure_ascii=False, default=str)

        self.assertNotIn("Bearer secret-token", encoded)
        self.assertNotIn("api_key=secret", encoded)
        self.assertIn("[REDACTED_SECRET]", encoded)
        self.assertIn("https://example.com/path", encoded)

    def test_redis_report_store_round_trip_and_latest_keys(self):
        redis = FakeRedisClient()
        store = RedisReportStore(redis, ttl_seconds=43200)
        report = AgentOrchestrator(store=store).analyze({"symbol": "NVDA", "intent": "뉴스 보여줘"})

        stored = store.get(report.analysisId)

        self.assertIsNotNone(stored)
        self.assertEqual(stored.analysisId, report.analysisId)
        self.assertEqual(stored.symbol, "NVDA")
        self.assertIsNotNone(stored.finalResponse)
        self.assertIsNotNone(report.finalResponse)
        self.assertEqual(stored.finalResponse.confidence, report.finalResponse.confidence)
        self.assertEqual(stored.finalResponse.answer_type, report.finalResponse.answer_type)
        self.assertEqual(stored.finalResponse.risk_warnings, report.finalResponse.risk_warnings)
        self.assertEqual(stored.finalResponse.data_freshness_warnings, report.finalResponse.data_freshness_warnings)
        keys = {key for key, _ttl, _value in redis.setex_calls}
        ttls = {ttl for _key, ttl, _value in redis.setex_calls}
        self.assertIn(f"agent:report:{report.analysisId}", keys)
        self.assertIn("agent:report:latest:NVDA", keys)
        self.assertIn("agent:report:latest", keys)
        self.assertEqual(ttls, {43200})

    def test_final_response_from_dict_preserves_confidence(self):
        final_response = final_response_from_dict({
            "run_id": "run-test",
            "answer_type": "news_impact_summary",
            "summary": "요약",
            "key_points": ["핵심"],
            "risk_warnings": ["partial_data_used"],
            "data_freshness_warnings": ["no_relevant_news_found"],
            "partial_data_used": True,
            "confidence": 0.72,
            "final_stance": "watch",
            "latency_ms": 12.5,
            "llm_calls_used": 1,
        })

        self.assertIsNotNone(final_response)
        self.assertEqual(final_response.confidence, 0.72)
        self.assertEqual(final_response.answer_type, "news_impact_summary")
        self.assertEqual(final_response.risk_warnings, ["partial_data_used"])
        self.assertEqual(final_response.data_freshness_warnings, ["no_relevant_news_found"])

    def test_redis_report_store_fail_open_when_redis_fails(self):
        store = RedisReportStore(BrokenRedisClient(), ttl_seconds=43200)
        report = AgentOrchestrator().analyze({"symbol": "NVDA", "intent": "뉴스 보여줘"})

        saved = store.save(report)

        self.assertEqual(saved.analysisId, report.analysisId)
        self.assertIn("reportStoreWriteFailed", saved.agentTrace)
        self.assertIsNone(store.get(report.analysisId))

    def test_report_store_idempotency_mapping_round_trip(self):
        store = InMemoryReportStore()

        store.save_idempotency_mapping("user-1", "idem-1", "agent-request-1")

        self.assertEqual(store.get_idempotency_request_id("user-1", "idem-1"), "agent-request-1")

    def test_report_store_owner_mapping_is_user_scoped(self):
        store = InMemoryReportStore()

        store.save_owner_mapping("user-1", "agent-request-1")

        self.assertTrue(store.is_owner("agent-request-1", "user-1"))
        self.assertFalse(store.is_owner("agent-request-1", "user-2"))
        self.assertFalse(store.is_owner("missing", "user-1"))

    def test_redis_report_store_owner_mapping_is_atomic_and_user_scoped(self):
        store = RedisReportStore(FakeRedisClient(), ttl_seconds=43200)

        self.assertTrue(store.save_owner_mapping("user-1", "agent-request-1"))
        self.assertTrue(store.save_owner_mapping("user-1", "agent-request-1"))
        self.assertFalse(store.save_owner_mapping("user-2", "agent-request-1"))
        self.assertTrue(store.is_owner("agent-request-1", "user-1"))
        self.assertFalse(store.is_owner("agent-request-1", "user-2"))

    def test_request_envelope_ignores_client_supplied_identity_and_budget(self):
        envelope = build_request_envelope(
            {
                "symbol": "NVDA",
                "intent": "analysis",
                "userId": "body-attacker",
                "maxLlmCalls": 999,
                "maxInputTokens": 999999,
                "llmBudgetOwner": "user",
            },
            user_id="trusted-user",
        )

        self.assertEqual(envelope.user_id, "trusted-user")
        self.assertEqual(envelope.quota_policy.max_llm_calls, 1)
        self.assertIsNone(envelope.quota_policy.max_input_tokens)
        self.assertEqual(envelope.quota_policy.llm_budget_owner, "platform")

    def test_report_store_cancel_marker_prevents_completed_overwrite(self):
        store = InMemoryReportStore()

        canceled = store.mark_canceled("agent-request-cancel", reason="test cancel", user_id="user-1")
        saved = store.save(AnalysisReport(
            analysisId="agent-request-cancel",
            symbol="NVDA",
            intent="analysis",
            status="completed",
            createdAt=utc_now_iso(),
            summary="done",
            rationale="late completion",
        ))

        self.assertEqual(canceled.status, "canceled")
        self.assertEqual(saved.status, "canceled")
        self.assertEqual(store.get("agent-request-cancel").status, "canceled")
        self.assertTrue(store.is_canceled("agent-request-cancel"))

    def test_report_store_cancel_does_not_override_completed_report(self):
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

        report = store.mark_canceled("agent-request-completed", reason="too late", user_id="user-1")

        self.assertEqual(report.status, "completed")
        self.assertEqual(store.get("agent-request-completed").status, "completed")
        self.assertFalse(store.is_canceled("agent-request-completed"))

    def test_admission_rejects_when_queue_depth_crosses_policy(self):
        envelope = build_request_envelope({"symbol": "NVDA", "intent": "analysis"}, request_id="agent-admission")

        decision = admit_analysis_request(
            envelope,
            AnalysisQueueMetrics(backend="test", queue_depth=10),
            policy=AdmissionPolicy(max_queue_depth=10),
        )

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "analysis_queue_backpressure")
        self.assertEqual(decision.status_code, 429)

    def test_admission_degrades_stream_delivery_to_poll_under_backlog(self):
        envelope = build_request_envelope(
            {"symbol": "NVDA", "intent": "analysis", "responseMode": "stream"},
            request_id="agent-admission-degrade",
        )

        decision = admit_analysis_request(
            envelope,
            AnalysisQueueMetrics(backend="test", queue_depth=1),
            policy=AdmissionPolicy(max_queue_depth=10, degrade_stream_to_poll=True),
        )

        self.assertTrue(decision.accepted)
        self.assertTrue(decision.degraded)
        self.assertEqual(envelope.delivery.response_mode, "poll")

    def test_orchestrator_uses_request_id_and_records_retrieval_context(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "뉴스 보여줘",
            "requestId": "agent-request-test",
        })

        self.assertEqual(report.analysisId, "agent-request-test")
        self.assertIn("retrievalContext", report.agentTrace)
        self.assertEqual(report.agentTrace["retrievalContext"]["primary_symbol"], "NVDA")
        self.assertEqual(report.timing["relatedSymbolsUsed"], 0)

    def test_orchestrator_returns_canceled_when_marker_exists_before_start(self):
        store = InMemoryReportStore()
        store.mark_canceled("agent-request-before-start", reason="user stopped", user_id="user-1")

        report = AgentOrchestrator(store=store).analyze({
            "symbol": "NVDA",
            "intent": "뉴스 보여줘",
            "requestId": "agent-request-before-start",
        })

        self.assertEqual(report.analysisId, "agent-request-before-start")
        self.assertEqual(report.status, "canceled")
        self.assertTrue(store.is_canceled("agent-request-before-start"))

    def test_analysis_worker_processes_envelope_to_shared_store(self):
        store = InMemoryReportStore()
        envelope = build_request_envelope(
            {"symbol": "NVDA", "intent": "뉴스 보여줘"},
            request_id="agent-request-worker",
            user_id="user-1",
            idempotency_key="idem-1",
        )
        worker = AgentAnalysisWorker(store=store, orchestrator=AgentOrchestrator(store=store))

        with patch("gops_agents.runtime.workers.publish_agent_outputs"):
            report = worker.process_envelope(envelope)

        self.assertEqual(report.analysisId, "agent-request-worker")
        self.assertEqual(report.status, "completed")
        self.assertIn("queueWaitMs", report.timing)
        self.assertEqual(report.timing["workerMode"], "hot")
        self.assertEqual(report.timing["providerBulkheadRejected"], 0)
        self.assertEqual(store.get("agent-request-worker").status, "completed")
        self.assertEqual(store.get_idempotency_request_id("user-1", "idem-1"), "agent-request-worker")

    def test_analysis_worker_skips_orchestrator_when_request_is_canceled(self):
        store = InMemoryReportStore()
        envelope = build_request_envelope(
            {"symbol": "NVDA", "intent": "뉴스 보여줘"},
            request_id="agent-request-worker-cancel",
            user_id="user-1",
        )
        store.mark_canceled(envelope.request_id, reason="user stopped", user_id="user-1")
        orchestrator = AgentOrchestrator(store=store)
        with patch.object(orchestrator, "analyze", side_effect=AssertionError("canceled request must not run orchestrator")):
            worker = AgentAnalysisWorker(store=store, orchestrator=orchestrator)
            with patch("gops_agents.runtime.workers.publish_agent_outputs") as publish:
                report = worker.process_envelope(envelope)

        self.assertEqual(report.status, "canceled")
        self.assertEqual(store.get(envelope.request_id).status, "canceled")
        self.assertEqual(publish.call_args.args[0]["status"], "canceled")

    def test_analysis_worker_queues_deep_update_and_marks_hot_report_pending(self):
        store = InMemoryReportStore()
        deep_queue = FakeAnalysisQueue()
        envelope = build_request_envelope(
            {"symbol": "NVDA", "intent": "뉴스 보여줘"},
            request_id="agent-request-deep-pending",
            user_id="user-1",
        )
        worker = AgentAnalysisWorker(
            store=store,
            orchestrator=AgentOrchestrator(store=store),
            deep_queue=deep_queue,
        )

        with patch.dict(os.environ, {"AGENT_DEEP_ANALYSIS_ENABLED": "true"}):
            with patch("gops_agents.runtime.workers.publish_agent_outputs"):
                report = worker.process_envelope(envelope)

        self.assertEqual(report.status, "deep_pending")
        self.assertEqual(store.get("agent-request-deep-pending").status, "deep_pending")
        self.assertEqual(len(deep_queue.envelopes), 1)
        self.assertEqual(deep_queue.envelopes[0].mode, "deep")
        self.assertEqual(deep_queue.envelopes[0].request_id, "agent-request-deep-pending")

    def test_deep_analysis_worker_marks_follow_up_completed_without_requeue(self):
        store = InMemoryReportStore()
        deep_queue = FakeAnalysisQueue()
        envelope = build_request_envelope(
            {"symbol": "NVDA", "intent": "뉴스 보여줘", "mode": "deep"},
            request_id="agent-request-deep-completed",
            user_id="user-1",
        )
        worker = AgentAnalysisWorker(
            store=store,
            orchestrator=AgentOrchestrator(store=store),
            deep_queue=deep_queue,
        )

        with patch.dict(os.environ, {"AGENT_DEEP_ANALYSIS_ENABLED": "true"}):
            with patch("gops_agents.runtime.workers.publish_agent_outputs"):
                report = worker.process_envelope(envelope)

        self.assertEqual(report.status, "deep_completed")
        self.assertEqual(store.get("agent-request-deep-completed").status, "deep_completed")
        self.assertEqual(len(deep_queue.envelopes), 0)
        self.assertEqual(report.agentTrace["deepAnalysis"]["status"], "deep_completed")

    def test_deep_analysis_worker_preserves_hot_report_while_running(self):
        store = InMemoryReportStore()
        hot_report = AgentOrchestrator(store=store).analyze({
            "symbol": "NVDA",
            "intent": "뉴스 보여줘",
            "requestId": "agent-request-deep-preserve",
        })
        hot_report.status = "deep_pending"
        store.save(hot_report)
        orchestrator = InspectingDeepOrchestrator(store)
        envelope = build_request_envelope(
            {"symbol": "NVDA", "intent": "뉴스 보여줘", "mode": "deep"},
            request_id="agent-request-deep-preserve",
            user_id="user-1",
        )
        worker = AgentAnalysisWorker(store=store, orchestrator=orchestrator, deep_queue=FakeAnalysisQueue())

        with patch("gops_agents.runtime.workers.publish_agent_outputs"):
            report = worker.process_envelope(envelope)

        self.assertIsNotNone(orchestrator.seen_report.finalAnswer)
        self.assertEqual(orchestrator.seen_report.status, "deep_pending")
        self.assertEqual(orchestrator.seen_report.agentTrace["deepAnalysis"]["status"], "running")
        self.assertEqual(report.status, "deep_completed")

    def test_delivery_gateway_stores_report_and_publishes_updates(self):
        source_report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "뉴스 보여줘",
            "requestId": "agent-delivery-test",
        })
        store = InMemoryReportStore()
        redis = FakePublishRedisClient()
        gateway = AgentDeliveryGateway(store=store, redis_client=redis)

        result = gateway.process_report_payload(source_report.to_dict())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(store.get("agent-delivery-test").status, "completed")
        self.assertEqual([call[0] for call in redis.publish_calls], ["agent.reports", "agent.reports:agent-delivery-test"])

    def test_graph_expansion_cache_reads_redis_payload(self):
        redis = FakeRedisClient()
        redis.setex(
            graph_expansion_cache_key("NVDA"),
            3600,
            json.dumps({
                "generated_at": "2026-01-01T00:00:00Z",
                "relation_version": "v1",
                "related_symbols": [
                    {"symbol": "AMD", "relation_type": "competitor", "score": 0.91, "reason": "GPU peer"}
                ],
                "themes": [{"name": "AI accelerators", "score": 0.8}],
                "keywords": ["GPU"],
            }),
        )

        expansion = GraphExpansionCache(redis_client=redis).load("nvda")

        self.assertEqual(expansion.source, "redis")
        self.assertTrue(expansion.cache_hit)
        self.assertEqual(expansion.related_symbols[0].symbol, "AMD")
        self.assertEqual(expansion.themes[0].name, "AI accelerators")

    def test_graph_expansion_builder_and_writer_store_payload(self):
        redis = FakeRedisClient()
        clickhouse = FakeClickHouseInsertClient()
        evidence = [
            EvidenceItem(
                provider="ontology",
                status="available",
                title="AMD related company",
                summary="AMD는 AI accelerators 테마에 포함된 기업입니다.",
                raw={
                    "relationType": "theme-company",
                    "ticker": "AMD",
                    "themeName": "AI accelerators",
                    "themeCategory": "semiconductors",
                    "sourceUrl": "https://example.test/source",
                },
            )
        ]

        expansion = build_graph_expansion_from_evidence("NVDA", evidence, relation_version="test-v1")
        GraphExpansionWriter(redis_client=redis, clickhouse_client=clickhouse).save("NVDA", expansion, ttl_seconds=60)

        self.assertEqual(expansion.related_symbols[0].symbol, "AMD")
        self.assertEqual(expansion.related_symbols[0].relation_type, "same-theme")
        self.assertEqual(expansion.themes[0].name, "AI accelerators")
        self.assertIn("semiconductors", expansion.keywords)
        self.assertEqual(redis.setex_calls[0][0], graph_expansion_cache_key("NVDA"))
        self.assertEqual(clickhouse.insert_calls[0][0], "agent_graph_expansions")
        self.assertEqual(clickhouse.insert_calls[0][1][0]["relation_version"], "test-v1")

    def test_graph_expansion_hint_feeds_bounded_news_symbols_when_enabled(self):
        rows_by_symbol = {
            "AMD": [{
                "symbol": "AMD",
                "title": "AMD GPU demand rises",
                "summary": "AMD reports stronger AI accelerator demand.",
                "publishedAt": utc_now_iso(),
                "symbols": ["AMD"],
            }]
        }
        provider = ThemeClickHouseProvider(rows_by_symbol)
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(ClickHouseNewsProvider(clickhouse_provider=provider, cache=MemoryNewsEvidenceCache()), localizer=NewsLocalizationService())
        graph_expansion = GraphExpansion(
            source="redis",
            cache_hit=True,
            related_symbols=[
                RelatedSymbol(symbol="AMD", relation_type="competitor", score=0.91, reason="GPU peer"),
                RelatedSymbol(symbol="TSM", relation_type="supplier", score=0.8, reason="Foundry"),
            ],
        )

        with patch.dict(os.environ, {
            "AGENT_GRAPH_EXPANSION_CACHE_ENABLED": "true",
            "AGENT_EXPANDED_RETRIEVAL_ENABLED": "true",
            "AGENT_MAX_RELATED_SYMBOLS": "1",
        }):
            with patch("gops_agents.retrieval.graph_expansion.load_graph_expansion", return_value=graph_expansion):
                report = orchestrator.analyze({"symbol": "NVDA", "intent": "뉴스 보여줘", "agentIds": ["agent-02"]})

        self.assertEqual(report.timing["graphExpansionCacheHit"], True)
        self.assertEqual(report.timing["relatedSymbolsRequested"], 2)
        self.assertEqual(report.timing["relatedSymbolsUsed"], 1)
        self.assertEqual(provider.requested_symbols, ["NVDA", "AMD"])

    def test_graph_expansion_hint_feeds_market_peer_snapshot_when_data_present(self):
        orchestrator = AgentOrchestrator()
        graph_expansion = GraphExpansion(
            source="redis",
            cache_hit=True,
            related_symbols=[RelatedSymbol(symbol="AMD", relation_type="competitor", score=0.91, reason="GPU peer")],
        )

        with patch.dict(os.environ, {
            "AGENT_GRAPH_EXPANSION_CACHE_ENABLED": "true",
            "AGENT_EXPANDED_RETRIEVAL_ENABLED": "true",
            "AGENT_MAX_MARKET_PEERS": "1",
        }):
            with patch("gops_agents.retrieval.graph_expansion.load_graph_expansion", return_value=graph_expansion):
                report = orchestrator.analyze({
                    "symbol": "NVDA",
                    "intent": "NVDA 시장 요약",
                    "agentIds": ["agent-01"],
                    "chartContext": {
                        "chartDocument": {"symbol": "NVDA", "timeframe": "1m"},
                        "visibleSummary": {"lastPrice": "120.00", "change": "+2.10%"},
                        "dataStatus": {"candleCount": 10, "state": "ready"},
                        "peerSummaries": [{"symbol": "AMD", "change": "+1.20%"}],
                    },
                })

        market_snapshot = next(item for item in report.snapshots if item.snapshot_type == "market_snapshot")
        self.assertEqual(report.timing["marketPeersRequested"], 1)
        self.assertEqual(report.timing["marketPeersFetched"], 1)
        self.assertTrue(any(item.raw.get("peerSymbol") == "AMD" for item in market_snapshot.evidence))

    def test_provider_bulkhead_rejection_is_recorded_in_timing(self):
        with patch.dict(os.environ, {
            "AGENT_PROVIDER_BULKHEAD_NEWS_MAX_CONCURRENCY": "1",
            "AGENT_PROVIDER_BULKHEAD_ACQUIRE_TIMEOUT_MS": "1",
        }):
            with provider_bulkhead("news"):
                report = AgentOrchestrator().analyze({"symbol": "NVDA", "intent": "뉴스 보여줘", "agentIds": ["agent-02"]})

        self.assertEqual(report.timing["providerBulkheadRejected"], 1)
        news_snapshot = next(item for item in report.snapshots if item.snapshot_type == "news_snapshot")
        self.assertEqual(news_snapshot.status, "failed")

    def test_cross_signal_join_records_single_name_news_signal_when_enabled(self):
        provider = FakeClickHouseProvider([{
            "symbol": "NVDA",
            "title": "NVDA shares rise",
            "summary": "NVDA demand stayed strong.",
            "publishedAt": utc_now_iso(),
            "symbols": ["NVDA"],
            "articleId": "article-nvda-1",
        }])
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(ClickHouseNewsProvider(clickhouse_provider=provider, cache=MemoryNewsEvidenceCache()), localizer=NewsLocalizationService())
        orchestrator.ontology_agent = OntologyAgent(FakeOntologyProvider([]))

        with patch.dict(os.environ, {"AGENT_CROSS_SIGNAL_ENABLED": "true"}):
            report = orchestrator.analyze({"symbol": "NVDA", "intent": "뉴스 보여줘", "agentIds": ["agent-02"]})

        self.assertEqual(report.timing["crossSignals"], 1)
        self.assertEqual(report.agentTrace["crossSignals"][0]["signal_type"], "single-name")
        self.assertEqual(report.synthesisInput.crossSignals[0]["evidence_refs"], ["news:article-nvda-1"])

    def test_cross_signal_join_records_related_symbol_signal_when_enabled(self):
        provider = ThemeClickHouseProvider({
            "AMD": [{
                "symbol": "AMD",
                "title": "AMD GPU demand rises",
                "summary": "AMD reports stronger AI accelerator demand.",
                "publishedAt": utc_now_iso(),
                "symbols": ["AMD"],
                "articleId": "article-amd-1",
            }]
        })
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(ClickHouseNewsProvider(clickhouse_provider=provider, cache=MemoryNewsEvidenceCache()), localizer=NewsLocalizationService())
        graph_expansion = GraphExpansion(
            source="redis",
            cache_hit=True,
            related_symbols=[RelatedSymbol(symbol="AMD", relation_type="competitor", score=0.91, reason="GPU peer")],
        )

        with patch.dict(os.environ, {
            "AGENT_GRAPH_EXPANSION_CACHE_ENABLED": "true",
            "AGENT_EXPANDED_RETRIEVAL_ENABLED": "true",
            "AGENT_CROSS_SIGNAL_ENABLED": "true",
        }):
            with patch("gops_agents.retrieval.graph_expansion.load_graph_expansion", return_value=graph_expansion):
                report = orchestrator.analyze({"symbol": "NVDA", "intent": "뉴스 보여줘", "agentIds": ["agent-02"]})

        signal = report.agentTrace["crossSignals"][0]
        self.assertEqual(signal["signal_type"], "peer-confirmed")
        self.assertEqual(signal["related_symbol"], "AMD")
        self.assertEqual(report.synthesisInput.crossSignals[0]["evidence_refs"], ["news:article-amd-1"])

    def test_cross_signal_trim_drops_low_confidence_items_first(self):
        signals = [
            {"target_symbol": "NVDA", "signal_type": "single-name", "confidence": 0.2, "explanation": "x" * 200},
            {"target_symbol": "NVDA", "signal_type": "peer-confirmed", "confidence": 0.9, "explanation": "strong"},
            {"target_symbol": "NVDA", "signal_type": "theme-wide", "confidence": 0.6, "explanation": "medium"},
        ]

        with patch.dict(os.environ, {"AGENT_MAX_SYNTHESIS_CROSS_SIGNAL_CHARS": "220", "AGENT_MAX_SYNTHESIS_CROSS_SIGNALS": "3"}):
            trimmed = trim_cross_signals(signals)

        self.assertEqual([item["confidence"] for item in trimmed], [0.9, 0.6])

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
        self.assertEqual(providers, {"news", "chart-analysis"})
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
            "layoutContext": layout_context(),
        })

        roles = {finding.role for finding in report.findings}
        self.assertEqual(report.route.intentType, "news")
        self.assertEqual(report.route.selectedRoles, ["news"])
        self.assertIn("news-analysis", roles)
        self.assertNotIn("chart-analysis", roles)
        self.assertIsNone(report.layoutProposal)

    def test_analysis_request_does_not_propose_layout_without_ui_intent(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "뉴스 보여줘",
        })

        self.assertIsNone(report.layoutProposal)

    def test_analysis_request_ignores_layout_context_without_ui_intent(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "뉴스 보여줘",
            "layoutContext": layout_context(pinned_news=True),
        })

        self.assertIsNone(report.layoutProposal)

    def test_clear_ui_router_does_not_call_llm_classifier(self):
        ui_router_response = {
            "output_text": json.dumps({
                "contentTasks": [],
                "uiTasks": [
                    {
                        "targetPanelType": "newsFeed",
                        "targetPanelId": "panel-news",
                        "action": "resize",
                        "sizeIntent": "large",
                        "positionIntent": None,
                        "confidence": 0.9,
                        "reason": "User asked to enlarge the news panel.",
                    }
                ],
                "routeMode": "ui_layout",
                "confidence": 0.9,
                "warnings": [],
            })
        }

        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-key",
            "AGENT_INTENT_CLASSIFIER_PROVIDER": "openai",
            "AGENT_MAX_REALTIME_LLM_CALLS": "1",
        }, clear=False):
            with patch("urllib.request.urlopen", return_value=FakeOpenAIResponse(ui_router_response)) as openai:
                report = AgentOrchestrator().analyze({
                    "symbol": "NVDA",
                    "intent": "뉴스 패널 크게 보여줘",
                    "layoutContext": layout_context(),
                })

        self.assertEqual(openai.call_count, 0)
        self.assertEqual(report.route.intentType, "ui-layout")
        self.assertEqual(report.route.source, "ui-parser")
        self.assertEqual(report.timing["llmCalls"], 0)
        self.assertEqual(report.timing["llmCallLabels"], [])

    def test_ui_layout_only_returns_fixed_ack_without_final_report_synthesis(self):
        orchestrator = AgentOrchestrator()
        orchestrator.workflow = None

        with patch.object(
            orchestrator,
            "_synthesize_final_answer",
            side_effect=AssertionError("UI-only fast ack must not synthesize a final answer"),
        ):
            with patch.object(
                orchestrator,
                "_finalize_report",
                side_effect=AssertionError("UI-only fast ack must not build a final report response"),
            ):
                report = orchestrator.analyze({
                    "symbol": "NVDA",
                    "intent": "뉴스 패널 크게 보여줘",
                    "layoutContext": layout_context(),
                })

        self.assertEqual(report.summary, "변경했습니다.")
        self.assertEqual(report.rationale, "시장 뉴스 패널 크기를 조정했습니다.")
        self.assertEqual(report.route.intentType, "ui-layout")
        self.assertEqual(report.findings, [])
        self.assertEqual(report.providerEvidence, [])
        self.assertEqual(report.snapshots, [])
        self.assertIsNone(report.finalAnswer)
        self.assertIsNone(report.finalResponse)
        self.assertIsNone(report.latencyTrace)
        self.assertIsNone(report.routePlan)
        self.assertIsNotNone(report.layoutProposal)
        self.assertEqual(report.agentTrace["analysisMode"], "auto")
        self.assertTrue(report.agentTrace["uiLayoutFastAck"])
        self.assertEqual(report.agentTrace["queryUnderstanding"]["routeMode"], "ui_layout")

    def test_layout_resolve_returns_ui_layout_without_analysis_report_pipeline(self):
        orchestrator = AgentOrchestrator()

        with patch.object(
            orchestrator,
            "_build_snapshot_plan",
            side_effect=AssertionError("Layout resolve must not build snapshots"),
        ):
            response = orchestrator.resolve_layout({
                "symbol": "NVDA",
                "intent": "뉴스 패널 크게 보여줘",
                "layoutContext": layout_context(),
            })

        self.assertEqual(response["status"], "ui_layout")
        self.assertEqual(response["summary"], "변경했습니다.")
        self.assertEqual(response["rationale"], "시장 뉴스 패널 크기를 조정했습니다.")
        self.assertEqual(response["route"]["intentType"], "ui-layout")
        self.assertTrue(response["agentTrace"]["uiLayoutFastAck"])
        self.assertIsNotNone(response["layoutProposal"])

    def test_layout_resolve_opens_news_panel_for_display_request(self):
        response = AgentOrchestrator().resolve_layout({
            "symbol": "AAPL",
            "intent": "NVDA 뉴스 보여줘",
            "layoutContext": chart_only_layout_context("AAPL"),
        })

        self.assertEqual(response["status"], "ui_layout")
        self.assertEqual(response["agentTrace"]["queryUnderstanding"]["routeMode"], "ui_layout")
        self.assertEqual(response["agentTrace"]["queryUnderstanding"]["resolvedSymbol"], "NVDA")
        proposal = response["layoutProposal"]
        # The 4x5 grid is dominated by a 4x4 chart, so the news panel's minimum
        # span cannot fit into free space: the agent must offer reflow-based
        # placement candidates for the user to pick instead of force-placing.
        open_command = next(
            command
            for command in proposal["commands"]
            if command["type"] in {"layout.panel.add", "layout.placement.pick"}
        )
        self.assertEqual(open_command["payload"]["panelType"], "newsFeed")
        self.assertEqual(open_command["payload"]["panelId"], "panel-news")
        self.assertEqual(open_command["payload"]["props"]["symbol"], "NVDA")
        if open_command["type"] == "layout.placement.pick":
            self.assertTrue(open_command["payload"]["candidates"])
        self.assertNotIn("대상 패널을 찾지 못했습니다", proposal["rationale"])

    def test_layout_resolve_loads_named_preset_without_snapshots(self):
        orchestrator = AgentOrchestrator()

        with patch.object(
            orchestrator,
            "_build_snapshot_plan",
            side_effect=AssertionError("Preset layout resolve must not build snapshots"),
        ):
            response = orchestrator.resolve_layout({
                "symbol": "NVDA",
                "intent": "시장분석 프리셋 띄워줘",
                "layoutContext": preset_layout_context(),
            })

        self.assertEqual(response["status"], "ui_layout")
        self.assertEqual(response["agentTrace"]["queryUnderstanding"]["routeMode"], "ui_layout")
        proposal = response["layoutProposal"]
        self.assertEqual(proposal["commands"][0]["type"], "layout.load")
        self.assertEqual(proposal["commands"][0]["payload"]["presetId"], "market")
        self.assertEqual(proposal["commands"][0]["payload"]["presetName"], "시장분석")

    def test_layout_resolve_loads_named_preset_without_preset_word(self):
        response = AgentOrchestrator().resolve_layout({
            "symbol": "NVDA",
            "intent": "시장분석창 보여줘",
            "layoutContext": preset_layout_context(),
        })

        self.assertEqual(response["status"], "ui_layout")
        proposal = response["layoutProposal"]
        self.assertEqual(proposal["commands"][0]["type"], "layout.load")
        self.assertEqual(proposal["commands"][0]["payload"]["presetId"], "market")

    def test_layout_resolve_clarifies_ambiguous_preset_names(self):
        context = preset_layout_context()
        context["presets"] = [
            {"id": "taste-1", "kind": "custom", "name": "내 입맛", "aliases": ["내입맛"]},
            {"id": "taste-2", "kind": "custom", "name": "내입맛", "aliases": ["내 입맛"]},
        ]

        response = AgentOrchestrator().resolve_layout({
            "symbol": "NVDA",
            "intent": "내입맛 프리셋 띄워줘",
            "layoutContext": context,
        })

        self.assertEqual(response["status"], "ui_clarify")
        self.assertIsNone(response["layoutProposal"])
        self.assertIn("ui_preset_match_ambiguous", response["agentTrace"]["warnings"])

    def test_layout_resolve_reopens_closed_named_panels_from_korean_show_variants(self):
        cases = [
            ("온톨로지 패널 보여주세요", "ontologyGraph", "panel-ontology"),
            ("관계 그래프 보여주세요", "ontologyGraph", "panel-ontology"),
            ("주문 패널 보여주세요", "orderTicket", "panel-order"),
            ("주문창 다시 띄워줘", "orderTicket", "panel-order"),
        ]
        for prompt, panel_type, panel_id in cases:
            with self.subTest(prompt=prompt):
                response = AgentOrchestrator().resolve_layout({
                    "symbol": "NVDA",
                    "intent": prompt,
                    "layoutContext": chart_only_layout_context("NVDA"),
                })

                self.assertEqual(response["status"], "ui_layout")
                proposal = response["layoutProposal"]
                # Direct add when the panel fits into free space, otherwise a
                # placement pick offering reflow-based candidates.
                open_command = next(
                    command
                    for command in proposal["commands"]
                    if command["type"] in {"layout.panel.add", "layout.placement.pick"}
                )
                self.assertEqual(open_command["payload"]["panelType"], panel_type)
                self.assertEqual(open_command["payload"]["panelId"], panel_id)
                if open_command["type"] == "layout.placement.pick":
                    self.assertTrue(open_command["payload"]["candidates"])
                self.assertNotIn("대상 패널을 찾지 못했습니다", proposal["rationale"])

    def test_layout_resolve_returns_not_ui_for_analysis_request(self):
        cases = [
            "NVDA 뉴스 분석해줘",
            "오늘 왜 떨어졌어?",
            "오늘 주가 왜 떨어졌어>",
            "주가 변동 원인 알려줘",
        ]
        for query in cases:
            with self.subTest(query=query):
                response = AgentOrchestrator().resolve_layout({
                    "symbol": "NVDA",
                    "intent": query,
                    "layoutContext": layout_context(),
                })

                self.assertEqual(response["status"], "not_ui")
                self.assertIsNone(response["layoutProposal"])
                self.assertFalse(response["agentTrace"]["uiLayoutFastAck"])

    def test_layout_resolve_returns_not_ui_for_reference_anchored_analysis_request(self):
        response = AgentOrchestrator().resolve_layout({
            "symbol": "NVDA",
            "intent": "이 뉴스랑 여기 봉 하락이 연결된 거야?",
            "references": [
                {
                    "type": "chart.candle",
                    "data": {"symbol": "NVDA", "interval": "1D", "timestamp": "2026-07-04T00:00:00Z", "close": 145.5},
                },
                {
                    "type": "news.article",
                    "data": {"symbol": "NVDA", "title": "NVDA supply headline", "publishedAt": "2026-07-04T13:30:00Z"},
                },
            ],
            "layoutContext": layout_context(),
        })

        self.assertEqual(response["status"], "not_ui")
        self.assertIsNone(response["layoutProposal"])
        self.assertFalse(response["agentTrace"]["uiLayoutFastAck"])

    def test_layout_resolve_returns_ui_clarify_for_unclear_ui_request(self):
        response = AgentOrchestrator().resolve_layout({
            "symbol": "NVDA",
            "intent": "패널 좀 해줘",
            "layoutContext": layout_context(),
        })

        self.assertEqual(response["status"], "ui_clarify")
        self.assertIn("어떤 패널", response["summary"])
        self.assertIsNone(response["layoutProposal"])
        self.assertEqual(response["route"]["intentType"], "ui-clarify")

    def test_layout_resolve_keep_only_removes_non_target_panels(self):
        response = AgentOrchestrator().resolve_layout({
            "symbol": "NVDA",
            "intent": "차트 패널 빼고 나머지 패널 없애줘",
            "layoutContext": layout_context(),
        })

        self.assertEqual(response["status"], "ui_layout")
        proposal = response["layoutProposal"]
        self.assertEqual(proposal["autoApply"], True)
        self.assertIn("차트만 남기고", proposal["rationale"])
        remove_ids = [
            command["payload"]["panelId"]
            for command in proposal["commands"]
            if command["type"] == "layout.panel.remove"
        ]
        self.assertIn("panel-news", remove_ids)
        self.assertIn("panel-ontology", remove_ids)
        self.assertNotIn("panel-chart-primary", remove_ids)

    def test_layout_resolve_keep_only_handles_delete_variants(self):
        variants = [
            "차트 패널 빼고 다 지워줘",
            "차트 빼고 다 삭제해줘",
            "차트 패널 빼고 나머지 패널 치워줘",
        ]
        for query in variants:
            with self.subTest(query=query):
                response = AgentOrchestrator().resolve_layout({
                    "symbol": "NVDA",
                    "intent": query,
                    "layoutContext": layout_context(),
                })

                self.assertEqual(response["status"], "ui_layout")
                proposal = response["layoutProposal"]
                self.assertEqual(proposal["autoApply"], True)
                self.assertIn("차트만 남기고", proposal["rationale"])
                remove_ids = {
                    command["payload"]["panelId"]
                    for command in proposal["commands"]
                    if command["type"] == "layout.panel.remove"
                }
                self.assertIn("panel-news", remove_ids)
                self.assertIn("panel-ontology", remove_ids)
                self.assertIn("panel-order", remove_ids)
                self.assertNotIn("panel-chart-primary", remove_ids)

    def test_layout_resolve_removes_multiple_named_panels(self):
        response = AgentOrchestrator().resolve_layout({
            "symbol": "NVDA",
            "intent": "뉴스랑 주문 패널 없애줘",
            "layoutContext": layout_context(),
        })

        self.assertEqual(response["status"], "ui_layout")
        proposal = response["layoutProposal"]
        remove_ids = {
            command["payload"]["panelId"]
            for command in proposal["commands"]
            if command["type"] == "layout.panel.remove"
        }
        self.assertEqual(remove_ids, {"panel-news", "panel-order"})
        self.assertIn("패널을 숨겼습니다", proposal["rationale"])

    def test_ui_router_budget_block_falls_back_without_openai_call(self):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-key",
            "AGENT_INTENT_CLASSIFIER_PROVIDER": "openai",
            "AGENT_MAX_REALTIME_LLM_CALLS": "0",
        }, clear=False):
            with patch("urllib.request.urlopen", side_effect=AssertionError("UI router should not call OpenAI after budget block")):
                report = AgentOrchestrator().analyze({
                    "symbol": "NVDA",
                    "intent": "뉴스 패널 크게 보여줘",
                    "layoutContext": layout_context(),
                })

        self.assertEqual(report.route.intentType, "ui-layout")
        self.assertEqual(report.route.source, "ui-parser")
        self.assertEqual(report.timing["llmCalls"], 0)
        self.assertEqual(report.timing["llmBudgetBlocked"], 0)

    def test_conductor_routes_market_move_to_all_visible_roles(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "NVDA 왜 올랐어?",
            "chartContext": {
                "chartDocument": {"symbol": "NVDA", "timeframe": "1m"},
                "dataStatus": {"candleCount": 10, "state": "ready"},
            },
            "layoutContext": layout_context(),
        })

        self.assertEqual(report.route.intentType, "market-move")
        self.assertEqual(report.route.selectedRoles, ["chart", "news", "macro", "ontology"])

    def test_conductor_uses_subject_fallback_for_market_move_without_query_symbol(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "주가 변동 원인 알려줘",
            "chartContext": {
                "chartDocument": {"symbol": "NVDA", "timeframe": "1D"},
                "dataStatus": {"candleCount": 20, "state": "ready"},
                "visibleSummary": {"lastPrice": 145.5, "change": "-2.10%"},
            },
            "layoutContext": layout_context(),
        })

        self.assertEqual(report.route.intentType, "market-move")
        self.assertEqual(report.route.selectedRoles, ["chart", "news", "macro", "ontology"])
        self.assertIsNotNone(report.routePlan)
        assert report.routePlan is not None
        self.assertEqual(report.routePlan.analysisQueryType, "price_move_cause")

    def test_conductor_preserves_news_based_market_question_as_composite_intent(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "NVDA 뉴스 기반으로 왜 올랐는지 알려줘",
            "layoutContext": layout_context(),
        })

        self.assertEqual(report.route.intentType, "market-move+news")
        self.assertEqual(report.route.selectedRoles, ["chart", "news", "macro", "ontology"])
        self.assertEqual(report.routePlan.intent, "investment_opinion")

    def test_analysis_query_policy_examples_select_expected_snapshot_bundles(self):
        cases = [
            ("NVDA 왜 떨어졌어?", [], "price_move_cause", {"market_snapshot", "news_snapshot", "relationship_snapshot", "risk_policy_snapshot"}),
            ("뉴스는 좋은데 왜 주가는 빠져?", [], "news_price_mismatch", {"news_snapshot", "market_snapshot", "relationship_snapshot", "risk_policy_snapshot"}),
            ("이거 개별 이슈야 시장 이슈야?", [], "factor_decomposition", {"market_snapshot", "relationship_snapshot", "news_snapshot", "risk_policy_snapshot"}),
            (
                "이 뉴스 영향받는 종목 뭐야?",
                [{"type": "news.article", "data": {"symbol": "NVDA", "title": "NVDA supply headline", "publishedAt": "2026-07-04T13:30:00Z"}}],
                "impact_mapping",
                {"news_snapshot", "relationship_snapshot", "market_snapshot", "risk_policy_snapshot"},
            ),
            (
                "선택한 봉 왜 저렇게 움직였어?",
                [{"type": "chart.candle", "data": {"symbol": "NVDA", "interval": "1D", "timestamp": "2026-07-04T00:00:00Z", "close": 145.5}}],
                "reference_anchor_analysis",
                {"chart_analysis_snapshot", "news_snapshot", "risk_policy_snapshot"},
            ),
        ]

        for intent, references, query_type, expected_bundle in cases:
            with self.subTest(intent=intent):
                report = AgentOrchestrator(analysis_cache=MemoryAgentAnalysisCache()).analyze({
                    "symbol": "NVDA",
                    "intent": intent,
                    "references": references,
                    "chartContext": {
                        "chartDocument": {"symbol": "NVDA", "timeframe": "1D"},
                        "dataStatus": {"candleCount": 20, "state": "ready"},
                        "visibleSummary": {"lastPrice": 145.5, "change": "-2.10%"},
                    },
                })

                self.assertEqual(report.routePlan.analysisQueryType, query_type)
                self.assertEqual(set(report.routePlan.snapshot_bundle), expected_bundle)
                self.assertEqual(report.agentTrace["analysisPolicy"]["analysisQueryType"], query_type)
                self.assertEqual(report.synthesisInput.output_policy["analysis_query_type"], query_type)
                final_answer_text = json.dumps(report.finalAnswer.to_dict(), ensure_ascii=False, default=str)
                self.assertNotRegex(final_answer_text, r"GraphDB|ClickHouse|Redis|providerEvidence|Provider status")

    def test_multi_agent_mode_posts_independent_role_answers_without_merge_synthesis(self):
        synthesizer = RoleAnswerOnlySynthesizer()
        orchestrator = AgentOrchestrator()
        orchestrator.synthesizer = synthesizer

        report = orchestrator.analyze({
            "symbol": "NVDA",
            "intent": "NVDA 뉴스랑 차트 멀티 에이전트로 분석해줘",
            "analysisMode": "multi_agent",
            "chartContext": {
                "chartDocument": {"symbol": "NVDA", "timeframe": "1m"},
                "visibleSummary": {"lastPrice": "120.00", "change": "+2.10%"},
                "dataStatus": {"candleCount": 10, "state": "ready"},
            },
            "layoutContext": layout_context(),
        })

        self.assertEqual(report.agentTrace["analysisMode"], "multi_agent")
        self.assertTrue(report.agentTrace["multiAgent"]["mergeSynthesisSkipped"])
        self.assertEqual(report.snapshots, [])
        self.assertEqual([answer.role for answer in report.agentAnswers], ["chart-analysis", "news-analysis"])
        self.assertCountEqual(synthesizer.role_answer_calls, ["chart-analysis", "news-analysis"])
        self.assertEqual(report.finalAnswer.summary, "각 에이전트가 독립 답변을 작성했습니다.")

    def test_multi_agent_text_does_not_switch_mode_without_request_field(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "NVDA 뉴스랑 차트 멀티 에이전트로 분석해줘",
            "chartContext": {
                "chartDocument": {"symbol": "NVDA", "timeframe": "1m"},
                "visibleSummary": {"lastPrice": "120.00", "change": "+2.10%"},
                "dataStatus": {"candleCount": 10, "state": "ready"},
            },
            "layoutContext": layout_context(),
        })

        self.assertEqual(report.agentTrace["analysisMode"], "auto")
        self.assertNotIn("multiAgent", report.agentTrace)
        self.assertEqual(report.agentAnswers, [])
        self.assertIsNotNone(report.finalAnswer)

    def test_multi_agent_ui_layout_request_stays_chat_only_without_ui_side_effects(self):
        orchestrator = AgentOrchestrator()
        orchestrator.ui_agent = FailingUIAgent()

        report = orchestrator.analyze({
            "symbol": "NVDA",
            "intent": "뉴스 패널 크게 보여줘",
            "analysisMode": "multi_agent",
            "layoutContext": layout_context(),
        })

        self.assertEqual(report.agentTrace["analysisMode"], "multi_agent")
        self.assertEqual(report.route.intentType, "ui-layout")
        self.assertEqual(report.findings, [])
        self.assertEqual(report.providerEvidence, [])
        self.assertEqual(report.snapshots, [])
        self.assertIsNone(report.layoutProposal)
        self.assertEqual(report.agentAnswers, [])
        self.assertEqual(report.finalAnswer.summary, "채팅 모드에서는 UI 변경을 실행하지 않습니다.")
        self.assertTrue(report.agentTrace["multiAgent"]["mergeSynthesisSkipped"])

    def test_multi_agent_hybrid_query_does_not_emit_layout_proposal(self):
        synthesizer = RoleAnswerOnlySynthesizer()
        orchestrator = AgentOrchestrator()
        orchestrator.synthesizer = synthesizer
        orchestrator.ui_agent = FailingUIAgent()

        report = orchestrator.analyze({
            "symbol": "NVDA",
            "intent": "NVDA 뉴스 분석해주고 뉴스 패널 크게 보여줘",
            "analysisMode": "multi_agent",
            "layoutContext": layout_context(),
        })

        self.assertEqual(report.agentTrace["analysisMode"], "multi_agent")
        self.assertEqual(report.agentTrace["queryUnderstanding"]["routeMode"], "hybrid")
        self.assertTrue(report.agentTrace["multiAgent"]["mergeSynthesisSkipped"])
        self.assertIsNone(report.layoutProposal)
        self.assertEqual([answer.role for answer in report.agentAnswers], ["news-analysis"])
        self.assertCountEqual(synthesizer.role_answer_calls, ["news-analysis"])
        self.assertEqual(report.finalAnswer.summary, "각 에이전트가 독립 답변을 작성했습니다.")

    def test_route_intent_preserves_composite_content_keywords(self):
        news_chart = route_intent("NVDA 뉴스랑 차트 보여줘")
        self.assertEqual(news_chart.intentType, "news+chart")
        self.assertEqual(news_chart.selectedRoles, ["chart", "news"])

        market_news_chart = route_intent("NVDA 왜 올랐는지 뉴스랑 차트로 봐줘")
        self.assertEqual(market_news_chart.intentType, "market-move+news+chart")
        self.assertEqual(market_news_chart.selectedRoles, ["chart", "news", "macro", "ontology"])

    def test_query_understanding_expands_composite_route_into_content_tasks(self):
        understanding, _ = build_query_understanding("NVDA 왜 올랐는지 뉴스랑 차트로 봐줘")

        self.assertEqual(understanding.intentType, "market-move+news+chart")
        self.assertEqual([task.taskType for task in understanding.contentTasks], ["market_move", "news", "chart"])
        self.assertEqual(understanding.selectedRoles, ["chart", "news", "macro", "ontology"])

    def test_query_company_entity_wins_over_chart_entity_fallback(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "애플 뉴스 보여줘",
            "chartContext": {
                "entityFallback": {
                    "source": "selected-chart",
                    "panelId": "panel-chart",
                    "chartDocumentId": "chart-doc-nvda",
                    "symbol": "NVDA",
                },
                "chartDocument": {"symbol": "NVDA", "timeframe": "1m"},
            },
            "layoutContext": layout_context(),
        })

        self.assertEqual(report.symbol, "AAPL")
        self.assertEqual(report.agentTrace["queryUnderstanding"]["resolvedSymbolSource"], "query_company")

    def test_query_without_company_uses_explicit_chart_entity_fallback(self):
        report = AgentOrchestrator().analyze({
            "intent": "뉴스 보여줘",
            "chartContext": {
                "entityFallback": {
                    "source": "selected-chart",
                    "panelId": "panel-chart",
                    "chartDocumentId": "chart-doc-msft",
                    "symbol": "MSFT",
                },
                "chartDocument": {"symbol": "NVDA", "timeframe": "1m"},
            },
            "layoutContext": layout_context(),
        })

        self.assertEqual(report.symbol, "MSFT")
        self.assertEqual(report.agentTrace["queryUnderstanding"]["resolvedSymbolSource"], "chart_context_entity_fallback")

    def test_query_without_company_uses_legacy_chart_document_symbol(self):
        report = AgentOrchestrator().analyze({
            "intent": "뉴스 보여줘",
            "chartContext": {"chartDocument": {"symbol": "MSFT", "timeframe": "1m"}},
            "layoutContext": layout_context(),
        })

        self.assertEqual(report.symbol, "MSFT")
        self.assertEqual(report.agentTrace["queryUnderstanding"]["resolvedSymbolSource"], "chart_context_chart_document")

    def test_request_symbol_not_in_supported_catalog_stops_before_provider_calls(self):
        with patch("gops_agents.orchestration.request.is_supported_company_symbol", return_value=False):
            report = AgentOrchestrator().analyze({
                "symbol": "ZZZZ",
                "intent": "ZZZZ 뉴스 보여줘",
                "layoutContext": layout_context(),
            })

        self.assertEqual(report.symbol, "ZZZZ")
        self.assertEqual(report.route.intentType, "unsupported-company")
        self.assertEqual(report.findings, [])
        self.assertEqual(report.providerEvidence, [])
        self.assertEqual(report.finalAnswer.title, "지원되지 않는 기업")
        self.assertEqual(report.agentTrace["queryUnderstanding"]["subjectValidation"]["status"], "unsupported")

    def test_request_without_subject_does_not_default_to_aapl(self):
        report = AgentOrchestrator().analyze({
            "intent": "뉴스 분석해줘",
            "layoutContext": layout_context(),
        })

        self.assertEqual(report.symbol, "UNKNOWN")
        self.assertEqual(report.route.intentType, "unsupported-company")
        self.assertEqual(report.finalAnswer.title, "기업 인식 실패")
        self.assertIn("지원 기업으로 인식하지 못했습니다", report.finalAnswer.summary)

    def test_event_detector_keeps_trade_price_signal_but_ignores_trade_size_for_volume(self):
        detector = MarketEventDetector(MarketEventThresholds(price_change_percent=3.0, volume_spike_multiplier=2.0))
        self.assertEqual(detector.detect({"symbol": "NVDA", "price": 100, "size": 100}, "market.layer.trades.v1"), [])

        events = detector.detect({"symbol": "NVDA", "price": 105, "size": 250}, "market.layer.trades.v1")

        event_types = {event.eventType for event in events}
        self.assertIn("price_surge", event_types)
        self.assertNotIn("volume_spike", event_types)

    def test_event_detector_uses_completed_candle_rolling_volume_baseline(self):
        detector = MarketEventDetector(MarketEventThresholds(
            volume_spike_multiplier=2.0,
            volume_baseline_window=3,
            volume_min_samples=3,
            volume_event_cooldown_seconds=0,
        ))
        topic = "market.layer.candles.1m.closed.v1"

        for timestamp, volume in (
            ("2026-07-14T00:00:00Z", 100),
            ("2026-07-14T00:01:00Z", 120),
            ("2026-07-14T00:02:00Z", 80),
        ):
            events = detector.detect({
                "symbol": "NVDA",
                "interval": "1m",
                "timestamp": timestamp,
                "volume": volume,
            }, topic)
            self.assertNotIn("volume_spike", {event.eventType for event in events})

        events = detector.detect({
            "symbol": "NVDA",
            "interval": "1m",
            "timestamp": "2026-07-14T00:03:00Z",
            "volume": 240,
        }, topic)

        volume_event = next(event for event in events if event.eventType == "volume_spike")
        self.assertEqual(volume_event.metrics["previousVolume"], 100.0)
        self.assertEqual(volume_event.metrics["baselineVolume"], 100.0)
        self.assertEqual(volume_event.metrics["baselineSamples"], 3)
        self.assertEqual(volume_event.metrics["interval"], "1m")
        self.assertEqual(volume_event.metrics["multiplier"], 2.4)

    def test_event_detector_keeps_volume_baselines_separate_by_interval(self):
        detector = MarketEventDetector(MarketEventThresholds(
            volume_spike_multiplier=2.0,
            volume_baseline_window=2,
            volume_min_samples=2,
            volume_event_cooldown_seconds=0,
        ))

        for interval, volumes in (("1m", (100, 100)), ("5m", (1000, 1000))):
            topic = f"market.layer.candles.{interval}.closed.v1"
            for index, volume in enumerate(volumes):
                events = detector.detect({
                    "symbol": "NVDA",
                    "interval": interval,
                    "timestamp": f"2026-07-14T00:0{index}:00Z",
                    "volume": volume,
                }, topic)
                self.assertNotIn("volume_spike", {event.eventType for event in events})

        events = detector.detect({
            "symbol": "NVDA",
            "interval": "1m",
            "timestamp": "2026-07-14T00:02:00Z",
            "volume": 250,
        }, "market.layer.candles.1m.closed.v1")

        volume_event = next(event for event in events if event.eventType == "volume_spike")
        self.assertEqual(volume_event.metrics["baselineVolume"], 100.0)
        self.assertEqual(volume_event.metrics["interval"], "1m")

    def test_event_detector_applies_volume_spike_cooldown_per_symbol_interval(self):
        detector = MarketEventDetector(MarketEventThresholds(
            volume_spike_multiplier=2.0,
            volume_baseline_window=2,
            volume_min_samples=2,
            volume_event_cooldown_seconds=1800,
        ))
        topic = "market.layer.candles.1m.closed.v1"

        for timestamp, volume in (
            ("2026-07-14T00:00:00Z", 100),
            ("2026-07-14T00:01:00Z", 100),
        ):
            detector.detect({"symbol": "NVDA", "interval": "1m", "timestamp": timestamp, "volume": volume}, topic)

        first = detector.detect({
            "symbol": "NVDA", "interval": "1m", "timestamp": "2026-07-14T00:02:00Z", "volume": 300,
        }, topic)
        suppressed = detector.detect({
            "symbol": "NVDA", "interval": "1m", "timestamp": "2026-07-14T00:03:00Z", "volume": 500,
        }, topic)
        detector.detect({
            "symbol": "NVDA", "interval": "1m", "timestamp": "2026-07-14T00:31:00Z", "volume": 100,
        }, topic)
        detector.detect({
            "symbol": "NVDA", "interval": "1m", "timestamp": "2026-07-14T00:32:00Z", "volume": 100,
        }, topic)
        after_cooldown = detector.detect({
            "symbol": "NVDA", "interval": "1m", "timestamp": "2026-07-14T00:33:00Z", "volume": 300,
        }, topic)

        self.assertIn("volume_spike", {event.eventType for event in first})
        self.assertNotIn("volume_spike", {event.eventType for event in suppressed})
        self.assertIn("volume_spike", {event.eventType for event in after_cooldown})

    def test_notification_payload_preserves_decision(self):
        payload = notification_payload({"symbol": "NVDA", "level": "alert", "showToast": True})

        self.assertEqual(payload["type"], "AGENT_ALERT")
        self.assertTrue(payload["showToast"])

    def test_notification_payload_promotes_watch_market_event_to_toast(self):
        payload = notification_payload({
            "symbol": "NVDA",
            "eventType": "volume_spike",
            "severity": "watch",
        })

        self.assertEqual(payload["level"], "watch")
        self.assertTrue(payload["showToast"])
        self.assertEqual(payload["decision"]["eventType"], "volume_spike")

    def test_notification_payload_preserves_explicit_toast_suppression(self):
        payload = notification_payload({
            "symbol": "NVDA",
            "severity": "critical",
            "showToast": False,
        })

        self.assertFalse(payload["showToast"])

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
                "headline": "Nvidia AI demand supports chip stocks",
                "summary": "NVDA suppliers gained as AI chip demand stayed strong.",
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
        self.assertEqual(evidence[0].raw["relevanceScore"], evidence[1].raw["relevanceScore"])
        self.assertGreater(evidence[0].raw["importanceScore"], evidence[1].raw["importanceScore"])
        self.assertGreater(evidence[0].raw["importanceScore"], 0)

    def test_news_provider_does_not_default_empty_symbol_to_aapl(self):
        clickhouse = FakeClickHouseProvider([])
        provider = ClickHouseNewsProvider(clickhouse_provider=clickhouse, limit=5, direct_fallback=False)

        provider.fetch(ProviderRequest("", "뉴스 보여줘"))

        self.assertEqual(clickhouse.requested_symbols, ["UNKNOWN"])

    def test_news_provider_uses_prelocalized_redis_when_cache_has_enough_rows(self):
        redis = FakeLocalizedRedisProvider([
            {
                "articleId": "redis-aapl-1",
                "symbol": "AAPL",
                "symbols": ["AAPL"],
                "headline": "Redis Apple supplier expands",
                "summary": "Redis Apple supplier plans expansion.",
                "localizedHeadline": "Redis 애플 공급사",
                "localizedSummary": "Redis hot cache 요약입니다.",
                "publishedAt": utc_now_iso(),
            }
        ])
        clickhouse = FakeLocalizedClickHouseProvider(
            [],
            localized_rows=[
                {
                    "articleId": "ko-aapl-1",
                    "symbol": "AAPL",
                    "symbols": ["AAPL"],
                    "headline": "Apple supplier expands",
                    "summary": "Apple supplier plans expansion.",
                    "localizedHeadline": "애플 공급사, 사업 확장 추진",
                    "localizedSummary": "애플 공급사가 사업 확장을 추진한다는 내용입니다.",
                    "publishedAt": utc_now_iso(),
                }
            ],
        )
        provider = ClickHouseNewsProvider(
            clickhouse_provider=clickhouse,
            redis_provider=redis,
            limit=1,
            direct_fallback=False,
        )

        evidence = provider.fetch(ProviderRequest("AAPL", "애플 뉴스 보여줘"))

        self.assertEqual(redis.calls, 1)
        self.assertEqual(clickhouse.localized_calls, 0)
        self.assertEqual(clickhouse.calls, 0)
        self.assertEqual(evidence[0].title, "Redis 애플 공급사")

    def test_news_provider_uses_clickhouse_when_prelocalized_redis_is_insufficient(self):
        redis = FakeLocalizedRedisProvider([
            {
                "articleId": "redis-aapl-1",
                "symbol": "AAPL",
                "symbols": ["AAPL"],
                "headline": "Redis Apple supplier expands",
                "summary": "Redis Apple supplier plans expansion.",
                "localizedHeadline": "Redis 애플 공급사",
                "localizedSummary": "Redis hot cache 요약입니다.",
                "publishedAt": utc_now_iso(),
            }
        ])
        clickhouse = FakeLocalizedClickHouseProvider(
            [],
            localized_rows=[
                {
                    "articleId": "ko-aapl-1",
                    "symbol": "AAPL",
                    "symbols": ["AAPL"],
                    "headline": "Apple supplier expands",
                    "summary": "Apple supplier plans expansion.",
                    "localizedHeadline": "애플 공급사, 사업 확장 추진",
                    "localizedSummary": "애플 공급사가 사업 확장을 추진한다는 내용입니다.",
                    "publishedAt": utc_now_iso(),
                    "url": "https://example.com/aapl-ko",
                    "eventType": "corporate-action",
                    "sentiment": "neutral",
                    "impactDirection": "neutral",
                    "keyPoints": ["애플 공급망 관련 뉴스"],
                    "positivePoints": [],
                    "concerns": [],
                    "whyItMatters": "애플 공급망 이슈입니다.",
                }
            ],
        )
        provider = ClickHouseNewsProvider(
            clickhouse_provider=clickhouse,
            redis_provider=redis,
            limit=5,
            direct_fallback=False,
        )

        evidence = provider.fetch(ProviderRequest("AAPL", "애플 뉴스 보여줘"))

        self.assertEqual(clickhouse.localized_calls, 1)
        self.assertEqual(clickhouse.localized_requested_symbols, [["AAPL"]])
        self.assertEqual(redis.calls, 1)
        self.assertEqual(len(redis.localized_warm_calls[0]["rows"]), 1)
        self.assertEqual(clickhouse.calls, 0)
        self.assertEqual(evidence[0].title, "애플 공급사, 사업 확장 추진")
        self.assertEqual(evidence[0].raw["localizedTitle"], "애플 공급사, 사업 확장 추진")
        self.assertEqual(evidence[0].raw["localizedSummary"], "애플 공급사가 사업 확장을 추진한다는 내용입니다.")
        self.assertEqual(evidence[0].raw["keyPoints"], ["애플 공급망 관련 뉴스"])
        self.assertEqual(evidence[0].raw["dataSource"], "prelocalized")

    def test_news_provider_uses_evidence_cache_before_prelocalized_sources(self):
        cache = MemoryNewsEvidenceCache()
        cache.set(
            symbol="AAPL",
            limit=5,
            days=7,
            fallback_enabled=False,
            items=[
                EvidenceItem(
                    provider="news",
                    status="available",
                    title="Cached Apple news",
                    summary="Cached Apple summary.",
                    raw={"articleId": "cached-aapl-1", "dataSource": "cache"},
                )
            ],
            ttl_seconds=60,
        )
        redis = FakeLocalizedRedisProvider([
            {
                "articleId": "ko-aapl-1",
                "symbol": "AAPL",
                "symbols": ["AAPL"],
                "headline": "Apple supplier expands",
                "summary": "Apple supplier plans expansion.",
                "localizedHeadline": "애플 공급사, 사업 확장 추진",
                "localizedSummary": "애플 공급사가 사업 확장을 추진한다는 내용입니다.",
                "publishedAt": utc_now_iso(),
            }
        ])
        clickhouse = FakeClickHouseProvider([])
        provider = ClickHouseNewsProvider(
            clickhouse_provider=clickhouse,
            redis_provider=redis,
            limit=5,
            days=7,
            direct_fallback=False,
            cache=cache,
        )

        evidence = provider.fetch(ProviderRequest("AAPL", "애플 뉴스 보여줘"))

        self.assertEqual(redis.calls, 0)
        self.assertEqual(clickhouse.calls, 0)
        self.assertEqual(evidence[0].title, "Cached Apple news")
        self.assertEqual(evidence[0].raw["articleId"], "cached-aapl-1")

    def test_news_provider_falls_back_to_redis_when_prelocalized_clickhouse_misses(self):
        redis = FakeLocalizedRedisProvider([
            {
                "articleId": "ko-ddog-1",
                "symbol": "DDOG",
                "symbols": ["DDOG"],
                "headline": "Datadog launches observability feature",
                "summary": "Datadog announced a new feature.",
                "localizedHeadline": "데이터독, 관측성 기능 출시",
                "localizedSummary": "데이터독이 새 관측성 기능을 발표했습니다.",
                "publishedAt": utc_now_iso(),
                "url": "https://example.com/ddog-ko",
                "eventType": "product-market",
                "sentiment": "positive",
                "impactDirection": "positive",
                "whyItMatters": "제품 경쟁력과 관련된 뉴스입니다.",
            }
        ])
        clickhouse = FakeLocalizedClickHouseProvider(
            [],
            localized_rows=[],
        )
        provider = ClickHouseNewsProvider(
            clickhouse_provider=clickhouse,
            redis_provider=redis,
            limit=5,
            direct_fallback=False,
        )

        evidence = provider.fetch(ProviderRequest("DDOG", "DDOG 뉴스 알려줘"))

        self.assertEqual(clickhouse.localized_calls, 1)
        self.assertEqual(redis.calls, 1)
        self.assertEqual(clickhouse.calls, 0)
        self.assertEqual(evidence[0].title, "데이터독, 관측성 기능 출시")
        self.assertEqual(evidence[0].raw["eventType"], "product-market")
        self.assertEqual(evidence[0].raw["impactDirection"], "positive")

    def test_news_provider_uses_redis_daily_summary_when_coverage_is_valid(self):
        redis = FakeLocalizedRedisProvider(
            [],
            daily_rows=[
                {
                    "date": "2026-07-01",
                    "symbol": "AAPL",
                    "summary": "Redis coverage 일일 요약입니다.",
                    "articleIds": ["redis-daily-1"],
                    "articleCount": 1,
                }
            ],
            daily_coverage={
                "symbol": "AAPL",
                "locale": "ko-KR",
                "days": 30,
                "limit": 5,
                "rowCount": 1,
                "coverageType": "complete",
            },
        )
        clickhouse = FakeLocalizedClickHouseProvider(
            [],
            daily_rows=[
                {
                    "date": "2026-07-01",
                    "symbol": "AAPL",
                    "summary": "ClickHouse 일일 요약입니다.",
                }
            ],
        )
        provider = ClickHouseNewsProvider(
            clickhouse_provider=clickhouse,
            redis_provider=redis,
            direct_fallback=False,
        )

        summaries = provider.fetch_daily_summaries(ProviderRequest("AAPL", "애플 뉴스 보여줘"))

        self.assertEqual(redis.daily_calls, 1)
        self.assertEqual(clickhouse.daily_calls, 0)
        self.assertEqual(summaries[0]["summary"], "Redis coverage 일일 요약입니다.")

    def test_news_provider_keeps_direct_subject_news_before_metadata_mentions(self):
        redis = FakeLocalizedRedisProvider([
            {
                "articleId": "aapl-primary-1",
                "symbol": "AAPL",
                "targetSymbol": "AAPL",
                "symbols": ["AAPL"],
                "headline": "Apple CEO Tim Cook flags memory chip shortage",
                "summary": "Apple CEO Tim Cook discussed memory chip shortages.",
                "localizedHeadline": "팀 쿡, 메모리칩 부족 언급",
                "localizedSummary": "애플 CEO 팀 쿡이 메모리칩 부족을 언급했습니다.",
                "publishedAt": utc_now_iso(),
                "url": "https://example.com/aapl-primary",
                "subjectRelevance": "primary",
                "relevanceScoreV2": 0.95,
            },
            {
                "articleId": "aapl-mention-1",
                "symbol": "AAPL",
                "targetSymbol": "AAPL",
                "symbols": ["AAPL", "NVDA", "MSFT", "META"],
                "headline": "Jim Cramer says Nvidia recommendation made a fortune",
                "summary": "AAPL appears only in provider metadata.",
                "localizedHeadline": "짐 크레이머, 엔비디아 추천 사례 언급",
                "localizedSummary": "엔비디아 추천 사례를 다룬 기사입니다.",
                "publishedAt": utc_now_iso(),
                "url": "https://example.com/aapl-mention",
                "subjectRelevance": "mention",
                "relevanceScoreV2": 0.35,
            },
        ])
        provider = ClickHouseNewsProvider(redis_provider=redis, clickhouse_provider=FakeClickHouseProvider([]), limit=5)

        evidence = provider.fetch(ProviderRequest("AAPL", "애플 뉴스 보여줘"))

        self.assertEqual([item.raw["articleId"] for item in evidence], ["aapl-primary-1"])
        self.assertEqual(evidence[0].raw["subjectRelevance"], "primary")

    def test_news_relevance_demotes_final_trades_lists_to_mentions(self):
        list_only = classify_subject_relevance(
            target_symbol="AAPL",
            headline="CNBC Final Trades: Apple, Amgen, Micron Technology, Palo Alto Networks",
            summary="The segment listed several stocks as final trades.",
            symbols=["AAPL", "AMGN", "MU", "PANW"],
        )
        contextual = classify_subject_relevance(
            target_symbol="AAPL",
            headline="CNBC Final Trades: Apple, Amgen, Micron Technology, Palo Alto Networks",
            summary="Brown said Apple shares could stay bullish into the second half.",
            symbols=["AAPL", "AMGN", "MU", "PANW"],
        )

        self.assertEqual(list_only["subjectRelevance"], "mention")
        self.assertEqual(contextual["subjectRelevance"], "secondary")

    def test_news_provider_uses_alpaca_fallback_when_clickhouse_is_empty(self):
        clickhouse = FakeClickHouseProvider([])
        provider = ClickHouseNewsProvider(clickhouse_provider=clickhouse, limit=5, direct_fallback=True, publish_fallback=False)
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

    def test_news_provider_caches_clickhouse_evidence(self):
        clickhouse = FakeClickHouseProvider([
            {
                "articleId": "cache-1",
                "headline": "NVDA shares rise after strong earnings",
                "summary": "NVDA revenue beat expectations.",
                "publishedAt": utc_now_iso(),
                "url": "https://example.com/cache",
                "symbols": ["NVDA"],
            }
        ])
        provider = ClickHouseNewsProvider(
            clickhouse_provider=clickhouse,
            limit=5,
            publish_fallback=False,
            cache=MemoryNewsEvidenceCache(),
        )

        first = provider.fetch(ProviderRequest("NVDA", "뉴스 보여줘"))
        second = provider.fetch(ProviderRequest("NVDA", "뉴스 다시 보여줘"))

        self.assertEqual(clickhouse.calls, 1)
        self.assertEqual(first[0].raw["articleId"], "cache-1")
        self.assertEqual(second[0].raw["articleId"], "cache-1")

    def test_news_provider_caches_no_data_with_short_ttl(self):
        now = [1000.0]
        clickhouse = FakeClickHouseProvider([])
        provider = ClickHouseNewsProvider(
            clickhouse_provider=clickhouse,
            limit=5,
            direct_fallback=False,
            cache=MemoryNewsEvidenceCache(now_fn=lambda: now[0]),
        )
        provider.no_data_cache_ttl_seconds = 60

        first = provider.fetch(ProviderRequest("NVDA", "뉴스 보여줘"))
        second = provider.fetch(ProviderRequest("NVDA", "뉴스 다시 보여줘"))
        now[0] += 61
        third = provider.fetch(ProviderRequest("NVDA", "뉴스 다시 보여줘"))

        self.assertEqual(clickhouse.calls, 2)
        self.assertEqual(first[0].status, "no-data")
        self.assertEqual(second[0].status, "no-data")
        self.assertEqual(third[0].status, "no-data")

    def test_news_provider_ignores_redis_cache_failures(self):
        clickhouse = FakeClickHouseProvider([
            {
                "articleId": "redis-fail-1",
                "headline": "NVDA shares rise after strong earnings",
                "summary": "NVDA revenue beat expectations.",
                "publishedAt": utc_now_iso(),
                "url": "https://example.com/redis-fail",
                "symbols": ["NVDA"],
            }
        ])
        provider = ClickHouseNewsProvider(
            clickhouse_provider=clickhouse,
            limit=5,
            publish_fallback=False,
            cache=RedisNewsEvidenceCache(BrokenRedisClient()),
        )

        evidence = provider.fetch(ProviderRequest("NVDA", "뉴스 보여줘"))

        self.assertEqual(clickhouse.calls, 1)
        self.assertEqual(evidence[0].status, "available")
        self.assertEqual(evidence[0].raw["articleId"], "redis-fail-1")

    def test_news_provider_caches_alpaca_fallback_success(self):
        clickhouse = FakeClickHouseProvider([])
        provider = ClickHouseNewsProvider(
            clickhouse_provider=clickhouse,
            limit=5,
            direct_fallback=True,
            publish_fallback=False,
            cache=MemoryNewsEvidenceCache(),
        )
        article = {
            "id": "fallback-cache-1",
            "headline": "NVDA shares rise after new AI chip launch",
            "summary": "NVIDIA announced a new data center GPU.",
            "url": "https://example.com/fallback-cache",
            "source": "alpaca",
            "created_at": "2026-06-30T01:02:03Z",
            "symbols": ["NVDA"],
        }

        with patch("alfaka.common.secrets.load_alpaca_credentials", return_value=("key", "secret")):
            with patch("alfaka.alpaca.news.fetch_alpaca_news", return_value=[article]) as fetch:
                first = provider.fetch(ProviderRequest("NVDA", "뉴스 보여줘"))
                second = provider.fetch(ProviderRequest("NVDA", "뉴스 다시 보여줘"))

        fetch.assert_called_once()
        self.assertEqual(clickhouse.calls, 1)
        self.assertEqual(first[0].raw["articleId"], "fallback-cache-1")
        self.assertEqual(second[0].raw["articleId"], "fallback-cache-1")

    def test_news_evidence_cache_round_trip_preserves_article_fields(self):
        cache = MemoryNewsEvidenceCache()
        item = EvidenceItem(
            provider="news",
            status="available",
            title="NVDA shares rise",
            summary="NVIDIA news summary",
            url="https://example.com/round-trip",
            raw={"articleId": "round-trip-1"},
        )

        cache.set(symbol="NVDA", limit=5, days=30, fallback_enabled=True, items=[item], ttl_seconds=60)
        cached = cache.get(symbol="NVDA", limit=5, days=30, fallback_enabled=True)

        self.assertIsNotNone(cached)
        self.assertEqual(cached[0].title, "NVDA shares rise")
        self.assertEqual(cached[0].summary, "NVIDIA news summary")
        self.assertEqual(cached[0].url, "https://example.com/round-trip")
        self.assertEqual(cached[0].raw["articleId"], "round-trip-1")

    def test_agent_redis_cache_clients_use_short_socket_timeouts(self):
        calls = []

        def fake_from_url(url, **kwargs):
            calls.append((url, kwargs))
            return FakeRedisClient()

        with patch.dict(os.environ, {
            "REDIS_CONNECT_TIMEOUT_SECONDS": "0.11",
            "REDIS_SOCKET_TIMEOUT_SECONDS": "0.22",
            "REDIS_HEALTH_CHECK_INTERVAL_SECONDS": "12",
        }):
            with patch.dict(sys.modules, {"redis": types.SimpleNamespace(from_url=fake_from_url)}):
                RedisNewsEvidenceCache(redis_url="redis://news-cache")
                RedisAgentAnalysisCache(redis_url="redis://analysis-cache")

        self.assertEqual([call[0] for call in calls], ["redis://news-cache", "redis://analysis-cache"])
        for _, kwargs in calls:
            self.assertTrue(kwargs["decode_responses"])
            self.assertEqual(kwargs["socket_connect_timeout"], 0.11)
            self.assertEqual(kwargs["socket_timeout"], 0.22)
            self.assertEqual(kwargs["health_check_interval"], 12)

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

    def test_analysis_news_request_does_not_add_news_panel_layout_payload(self):
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

        self.assertIsNotNone(report.finalAnswer)
        self.assertIsNone(report.layoutProposal)

    def test_analysis_news_request_does_not_update_existing_news_panel_layout_payload(self):
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

        self.assertIsNotNone(report.finalAnswer)
        self.assertIsNone(report.layoutProposal)

    def test_orchestrator_analysis_cache_reuses_final_answer_without_layout_side_effects(self):
        provider = ClickHouseNewsProvider(
            clickhouse_provider=FakeClickHouseProvider([
                {
                    "articleId": "analysis-cache-1",
                    "headline": "NVDA shares rise after strong earnings",
                    "summary": "NVDA revenue beat expectations.",
                    "publishedAt": utc_now_iso(),
                    "url": "https://example.com/analysis-cache",
                    "symbols": ["NVDA"],
                    "source": "alpaca",
                }
            ]),
            publish_fallback=False,
        )
        news_agent = CountingNewsAgent(provider)
        synthesizer = CountingSynthesizer()
        orchestrator = AgentOrchestrator(analysis_cache=MemoryAgentAnalysisCache())
        orchestrator.news_agent = news_agent
        orchestrator.synthesizer = synthesizer

        first = orchestrator.analyze({
            "symbol": "NVDA",
            "intent": "뉴스 보여줘",
            "agentIds": ["agent-02"],
            "layoutContext": layout_context(),
        })
        second = orchestrator.analyze({
            "symbol": "NVDA",
            "intent": "뉴스 보여줘",
            "agentIds": ["agent-02"],
            "layoutContext": layout_context(pinned_news=True),
        })

        self.assertFalse(first.timing["cacheHit"])
        self.assertTrue(second.timing["cacheHit"])
        self.assertEqual(second.timing["cacheLayer"], "analysis")
        self.assertEqual(second.timing["directNewsCount"], first.timing["directNewsCount"])
        self.assertEqual(second.timing["mentionNewsCount"], first.timing["mentionNewsCount"])
        self.assertEqual(news_agent.calls, 0)
        self.assertEqual(provider.clickhouse_provider.calls, 1)
        self.assertEqual(synthesizer.calls, 1)
        self.assertEqual(first.finalAnswer.summary, second.finalAnswer.summary)
        self.assertIsNone(first.layoutProposal)
        self.assertIsNone(second.layoutProposal)

    def test_news_paraphrases_share_overview_analysis_cache(self):
        provider = ClickHouseNewsProvider(
            clickhouse_provider=FakeClickHouseProvider([
                {
                    "articleId": "paraphrase-cache-1",
                    "headline": "Datadog shares rise after cloud software demand improves",
                    "summary": "DDOG cloud software demand improved.",
                    "publishedAt": utc_now_iso(),
                    "url": "https://example.com/paraphrase-cache",
                    "symbols": ["DDOG"],
                    "source": "alpaca",
                }
            ]),
            publish_fallback=False,
        )
        news_agent = CountingNewsAgent(provider)
        synthesizer = CountingSynthesizer()
        orchestrator = AgentOrchestrator(analysis_cache=MemoryAgentAnalysisCache())
        orchestrator.news_agent = news_agent
        orchestrator.synthesizer = synthesizer

        first = orchestrator.analyze({"symbol": "DDOG", "intent": "DDOG 뉴스 알려줘", "agentIds": ["agent-02"], "layoutContext": layout_context()})
        second = orchestrator.analyze({"symbol": "DDOG", "intent": "DDOG 핵심 뉴스 찾아줘", "agentIds": ["agent-02"], "layoutContext": layout_context()})
        third = orchestrator.analyze({"symbol": "DDOG", "intent": "DDOG 최신뉴스 보여줘", "agentIds": ["agent-02"], "layoutContext": layout_context()})
        fourth = orchestrator.analyze({"symbol": "DDOG", "intent": "DDOG 관련 기사 요약해줘", "agentIds": ["agent-02"], "layoutContext": layout_context()})

        self.assertFalse(first.timing["cacheHit"])
        self.assertTrue(second.timing["cacheHit"])
        self.assertTrue(third.timing["cacheHit"])
        self.assertTrue(fourth.timing["cacheHit"])
        self.assertEqual(news_agent.calls, 0)
        self.assertEqual(provider.clickhouse_provider.calls, 1)
        self.assertEqual(synthesizer.calls, 1)

    def test_news_canonical_intent_separates_filtered_news_requests(self):
        route = IntentRoute(source="rule", intentType="news", selectedRoles=["news"], confidence=0.9, reason="test")

        self.assertEqual(canonical_analysis_intent("DDOG 뉴스 알려줘", route, ["news"]), "news-overview")
        self.assertEqual(canonical_analysis_intent("DDOG 핵심 뉴스 찾아줘", route, ["news"]), "news-overview")
        self.assertEqual(canonical_analysis_intent("DDOG 최신뉴스 보여줘", route, ["news"]), "news-overview")
        self.assertEqual(canonical_analysis_intent("DDOG 관련 기사 요약해줘", route, ["news"]), "news-overview")
        self.assertEqual(canonical_analysis_intent("DDOG 악재 뉴스만 찾아줘", route, ["news"]), "news-negative")
        self.assertEqual(canonical_analysis_intent("DDOG 호재 뉴스만 찾아줘", route, ["news"]), "news-positive")
        self.assertEqual(canonical_analysis_intent("DDOG 실적 뉴스 알려줘", route, ["news"]), "news-earnings")
        self.assertEqual(canonical_analysis_intent("DDOG 애널리스트 목표가 뉴스", route, ["news"]), "news-analyst")

        chart_route = IntentRoute(source="rule", intentType="chart", selectedRoles=["chart"], confidence=0.9, reason="test")
        self.assertEqual(canonical_analysis_intent("DDOG 뉴스 알려줘", chart_route, ["chart"]), "ddog 뉴스 알려줘")

    def test_orchestrator_adds_snapshot_contract_and_hides_risk_snapshot_from_visible_trace(self):
        provider = ClickHouseNewsProvider(
            clickhouse_provider=FakeClickHouseProvider([
                {
                    "articleId": "snapshot-news-1",
                    "headline": "NVDA shares rise after AI demand update",
                    "summary": "NVDA demand for AI chips stayed strong.",
                    "publishedAt": utc_now_iso(),
                    "url": "https://example.com/snapshot-news",
                    "symbols": ["NVDA"],
                    "source": "alpaca",
                }
            ]),
            publish_fallback=False,
        )
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(provider)
        orchestrator.ontology_agent = OntologyAgent(FakeOntologyProvider([]))

        report = orchestrator.analyze({
            "symbol": "NVDA",
            "intent": "NVDA 뉴스 기반으로 영향 분석해줘",
            "agentIds": ["agent-02"],
        })

        snapshot_types = {snapshot.snapshot_type for snapshot in report.snapshots}
        self.assertEqual(report.routePlan.intent, "news_impact_analysis")
        self.assertIn("news_snapshot", snapshot_types)
        self.assertIn("relationship_snapshot", snapshot_types)
        self.assertIn("risk_policy_snapshot", snapshot_types)
        self.assertEqual(report.synthesisInput.run_id, report.routePlan.run_id)
        self.assertEqual(report.finalResponse.answer_type, "news_impact_summary")
        visible_types = {item["snapshot_type"] for item in report.agentTrace["visibleSnapshots"]}
        self.assertIn("news_snapshot", visible_types)
        self.assertNotIn("risk_policy_snapshot", visible_types)
        self.assertEqual(report.agentTrace["hiddenSnapshots"], ["risk_policy_snapshot"])
        self.assertLessEqual(report.finalResponse.llm_calls_used, 1)
        self.assertTrue(report.finalResponse.partial_data_used)
        self.assertIn("partial_data_used", report.finalResponse.risk_warnings)

    def test_provider_evidence_pii_and_profanity_are_redacted_before_display_and_report(self):
        provider = ClickHouseNewsProvider(
            clickhouse_provider=FakeClickHouseProvider([
                {
                    "articleId": "redaction-1",
                    "headline": "NVDA contact pii.news@example.com after earnings",
                    "summary": "Analyst note included phone 010-9876-5432 and shit language.",
                    "publishedAt": utc_now_iso(),
                    "url": "https://example.com/news?token=secret#debug",
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
            "intent": "NVDA 뉴스 기반으로 영향 분석해줘",
            "agentIds": ["agent-02"],
        })
        payload = report.to_dict()
        encoded = json.dumps(payload, ensure_ascii=False, default=str)

        self.assertNotIn("pii.news@example.com", encoded)
        self.assertNotIn("010-9876-5432", encoded)
        self.assertNotIn("shit", encoded.lower())
        self.assertNotIn("token=secret", encoded)
        self.assertIn("[REDACTED_EMAIL]", encoded)
        self.assertIn("[REDACTED_PHONE]", encoded)
        self.assertIn("[FILTERED]", encoded)
        self.assertIn("pii_redacted", report.finalResponse.risk_warnings)
        self.assertIn("profanity_removed", report.finalResponse.risk_warnings)

    def test_rule_guardrail_sanitizes_all_final_response_sections(self):
        response = FinalResponse(
            run_id="run-redaction",
            answer_type="investment_opinion",
            summary="Buy now after checking pii.final@example.com",
            key_points=["place order after 010-1111-2222"],
            bullish_points=["지금 매수하세요"],
            bearish_points=["This includes shit wording."],
            relationship_impacts=["relationship contact 900101-1234567"],
            confidence=0.9,
            final_stance="buy",
            llm_calls_used=0,
        )
        route_plan = RoutePlan(
            run_id="run-redaction",
            intent="investment_opinion",
            route_confidence=0.9,
            llm_calls_allowed=1,
        )

        guarded = apply_rule_guardrail(response, route_plan, [])
        encoded = json.dumps(guarded.to_dict(), ensure_ascii=False, default=str)

        self.assertEqual(guarded.final_stance, "watch")
        self.assertLessEqual(guarded.confidence, 0.55)
        self.assertNotIn("Buy now", encoded)
        self.assertNotIn("place order", encoded)
        self.assertNotIn("pii.final@example.com", encoded)
        self.assertNotIn("010-1111-2222", encoded)
        self.assertNotIn("900101-1234567", encoded)
        self.assertNotIn("shit", encoded.lower())
        self.assertIn("direct_investment_command_removed", guarded.risk_warnings)
        self.assertIn("pii_redacted", guarded.risk_warnings)
        self.assertIn("profanity_removed", guarded.risk_warnings)

    def test_hybrid_router_does_not_call_openai_only_because_api_key_exists(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            with patch("urllib.request.urlopen", side_effect=AssertionError("route LLM should be degraded-only")) as urlopen:
                route = route_intent("분석해줘", router_mode="hybrid")

        urlopen.assert_not_called()
        self.assertEqual(route.source, "fallback")
        self.assertEqual(route.selectedRoles, ["chart", "news", "macro", "ontology"])

    def test_snapshot_hot_path_does_not_call_news_role_llm_even_when_enabled(self):
        provider = ClickHouseNewsProvider(
            clickhouse_provider=FakeClickHouseProvider([
                {
                    "articleId": "no-role-llm-1",
                    "headline": "NVDA shares rise after strong earnings",
                    "summary": "NVDA revenue beat expectations.",
                    "publishedAt": utc_now_iso(),
                    "url": "https://example.com/no-role-llm",
                    "symbols": ["NVDA"],
                    "source": "alpaca",
                }
            ]),
            publish_fallback=False,
        )
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(provider)

        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-key",
            "AGENT_NEWS_ROLE_ANALYSIS_PROVIDER": "openai",
            "AGENT_FINAL_ANSWER_PROVIDER": "deterministic",
            "AGENT_NEWS_LOCALIZATION_PROVIDER": "deterministic",
        }, clear=False):
            with patch("urllib.request.urlopen", side_effect=AssertionError("news role LLM should not run in snapshot hot path")):
                report = orchestrator.analyze({
                    "symbol": "NVDA",
                    "intent": "NVDA 왜 올랐어?",
                    "routerMode": "rules",
                    "agentIds": ["agent-02"],
                })

        self.assertEqual(report.timing["llmCalls"], 0)
        self.assertIn("news_snapshot", {snapshot.snapshot_type for snapshot in report.snapshots})

    def test_snapshot_timeout_returns_failed_snapshot_and_partial_report(self):
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(SlowNewsProvider(delay_seconds=0.2))
        orchestrator.ontology_agent = OntologyAgent(FakeOntologyProvider([
            EvidenceItem(
                provider="ontology",
                status="available",
                title="NVDA theme",
                summary="NVDA is mapped to an AI theme.",
                raw={"relationType": "theme", "ticker": "NVDA", "themeName": "AI"},
            )
        ]))

        with patch.dict(os.environ, {
            "AGENT_FINAL_ANSWER_PROVIDER": "deterministic",
            "AGENT_NEWS_LOCALIZATION_PROVIDER": "deterministic",
            "AGENT_SNAPSHOT_TIMEOUT_MS": "10",
            "AGENT_UI_ROUTER_PROVIDER": "deterministic",
        }, clear=False):
            report = orchestrator.analyze({
                "symbol": "NVDA",
                "intent": "NVDA 뉴스 기반으로 영향 분석해줘",
                "agentIds": ["agent-02"],
            })

        news_snapshot = next(snapshot for snapshot in report.snapshots if snapshot.snapshot_type == "news_snapshot")
        self.assertEqual(news_snapshot.status, "failed")
        self.assertIn("timeout", news_snapshot.warnings)
        self.assertTrue(report.finalResponse.partial_data_used)
        self.assertEqual(report.latencyTrace.stages[2].stage, "snapshot_fetch")
        self.assertEqual(report.latencyTrace.stages[2].status, "timeout")

    def test_llm_budget_blocks_second_realtime_call(self):
        provider = ClickHouseNewsProvider(
            clickhouse_provider=FakeClickHouseProvider([
                {
                    "articleId": "budget-1",
                    "headline": "NVDA shares rise after strong earnings",
                    "summary": "NVDA revenue beat expectations.",
                    "publishedAt": utc_now_iso(),
                    "url": "https://example.com/budget",
                    "symbols": ["NVDA"],
                    "source": "alpaca",
                }
            ]),
            publish_fallback=False,
        )
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(provider)
        orchestrator.ontology_agent = OntologyAgent(FakeOntologyProvider([]))
        openai_synthesis_response = {
            "output_text": json.dumps({
                "title": "NVDA 종합 판단",
                "summary": "NVDA 상승은 뉴스 근거와 일부 관련 가능성이 있지만 단일 원인으로 확정하기는 어렵습니다.",
                "sections": [{"title": "핵심 이유", "bullets": ["뉴스 근거가 있습니다."]}],
                "citations": [],
                "limitations": [],
            })
        }

        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-key",
            "AGENT_FINAL_ANSWER_PROVIDER": "openai",
            "AGENT_MAX_REALTIME_LLM_CALLS": "1",
            "AGENT_NEWS_LOCALIZATION_PROVIDER": "deterministic",
            "AGENT_NEWS_ROLE_ANALYSIS_PROVIDER": "openai",
            "AGENT_UI_ROUTER_PROVIDER": "deterministic",
            "AGENT_USE_SNAPSHOT_HOT_PATH": "false",
        }, clear=False):
            with patch("urllib.request.urlopen", return_value=FakeOpenAIResponse(openai_synthesis_response)) as openai:
                report = orchestrator.analyze({
                    "symbol": "NVDA",
                    "intent": "NVDA 왜 올랐어?",
                    "agentIds": ["agent-02"],
                })

        self.assertEqual(openai.call_count, 1)
        self.assertEqual(report.timing["llmCalls"], 1)
        self.assertEqual(report.timing["llmBudgetBlocked"], 1)
        self.assertEqual(report.timing["llmCallLabels"], ["synthesis"])
        self.assertEqual(report.timing["synthesisProvider"], "openai")
        self.assertEqual(report.finalAnswer.title, "NVDA 종합 판단")
        self.assertIn("llm_call_budget_exceeded", report.finalResponse.risk_warnings)

    def test_orchestrator_analysis_cache_fail_open_when_redis_fails(self):
        provider = ClickHouseNewsProvider(
            clickhouse_provider=FakeClickHouseProvider([
                {
                    "articleId": "analysis-cache-fail-open-1",
                    "headline": "NVDA shares rise after strong earnings",
                    "summary": "NVDA revenue beat expectations.",
                    "publishedAt": utc_now_iso(),
                    "url": "https://example.com/analysis-cache-fail-open",
                    "symbols": ["NVDA"],
                    "source": "alpaca",
                }
            ]),
            publish_fallback=False,
        )
        news_agent = CountingNewsAgent(provider)
        orchestrator = AgentOrchestrator(analysis_cache=RedisAgentAnalysisCache(BrokenRedisClient()))
        orchestrator.news_agent = news_agent
        orchestrator.synthesizer = CountingSynthesizer()

        report = orchestrator.analyze({
            "symbol": "NVDA",
            "intent": "뉴스 보여줘",
            "agentIds": ["agent-02"],
            "layoutContext": layout_context(),
        })

        self.assertFalse(report.timing["cacheHit"])
        self.assertEqual(news_agent.calls, 0)
        self.assertEqual(provider.clickhouse_provider.calls, 1)
        self.assertTrue(report.finalAnswer.summary)

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
                self.assertEqual(report.finalAnswer.title, "뉴스를 가져왔습니다")
                self.assertIsNone(report.layoutProposal)

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
            "AGENT_ALLOW_RUNTIME_NEWS_OPENAI": "true",
            "AGENT_ROLE_ANALYSIS_PROVIDER": "deterministic",
            "AGENT_FINAL_ANSWER_PROVIDER": "deterministic",
        }, clear=False):
            with patch("urllib.request.urlopen", return_value=FakeOpenAIResponse(response)):
                report = orchestrator.analyze({"symbol": "NVDA", "intent": "NVDA 왜 올랐는지 봐줘", "agentIds": ["agent-01", "agent-02"]})

        evidence = report.providerEvidence[0]
        self.assertEqual(evidence.raw["originalTitle"], "NVDA shares rise after strong earnings")
        self.assertEqual(evidence.raw["originalSummary"], "NVDA revenue beat expectations.")
        self.assertEqual(evidence.raw["localizedTitle"], "엔비디아, 실적 호조에 주가 상승")
        self.assertEqual(evidence.raw["localizedSummary"], "엔비디아 매출이 기대치를 웃돌며 투자심리가 개선됐습니다.")
        self.assertEqual(evidence.url, "https://example.com/panel-ko")
        self.assertIsNone(report.layoutProposal)
        final_text = json.dumps(report.finalAnswer.to_dict(), ensure_ascii=False)
        self.assertIn("엔비디아, 실적 호조에 주가 상승", final_text)
        self.assertIn("엔비디아 매출이 기대치를 웃돌며 투자심리가 개선됐습니다.", final_text)

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
            "AGENT_ALLOW_RUNTIME_NEWS_OPENAI": "true",
            "AGENT_ROLE_ANALYSIS_PROVIDER": "deterministic",
            "AGENT_FINAL_ANSWER_PROVIDER": "deterministic",
        }, clear=False):
            with patch("urllib.request.urlopen", return_value=FakeOpenAIResponse({"output_text": "{invalid"})):
                report = orchestrator.analyze({"symbol": "NVDA", "intent": "뉴스 보여줘", "agentIds": ["agent-02"]})

        evidence = report.providerEvidence[0]
        self.assertNotIn("localizedTitle", evidence.raw)
        self.assertIsNone(report.layoutProposal)
        final_text = json.dumps(report.finalAnswer.to_dict(), ensure_ascii=False)
        self.assertIn("NVDA shares rise after strong earnings", final_text)
        self.assertIn("NVDA revenue beat expectations.", final_text)

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
            "AGENT_ALLOW_RUNTIME_NEWS_OPENAI": "true",
            "AGENT_ROLE_ANALYSIS_PROVIDER": "deterministic",
        }, clear=False):
            with patch("urllib.request.urlopen", return_value=FakeOpenAIResponse(response)) as urlopen:
                first = agent.analyze(context)
                second = agent.analyze(context)

        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(first.evidence[0].raw["localizedTitle"], "엔비디아 실적 호조")
        self.assertEqual(second.evidence[0].raw["localizedTitle"], "엔비디아 실적 호조")

    def test_news_localization_skips_prelocalized_evidence(self):
        provider = ClickHouseNewsProvider(
            clickhouse_provider=FakeLocalizedClickHouseProvider(
                [],
                localized_rows=[
                    {
                        "articleId": "prelocalized-1",
                        "symbol": "NVDA",
                        "symbols": ["NVDA"],
                        "headline": "NVDA shares rise after strong earnings",
                        "summary": "NVDA revenue beat expectations.",
                        "localizedHeadline": "엔비디아, 실적 호조에 상승",
                        "localizedSummary": "엔비디아 매출이 기대치를 웃돌았습니다.",
                        "publishedAt": utc_now_iso(),
                        "url": "https://example.com/prelocalized",
                    }
                ],
            ),
            publish_fallback=False,
        )
        agent = NewsAgent(provider, localizer=NewsLocalizationService())
        context = AgentContext(symbol="NVDA", intent="뉴스 보여줘")

        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-key",
            "AGENT_NEWS_LOCALIZATION_PROVIDER": "openai",
            "AGENT_ROLE_ANALYSIS_PROVIDER": "deterministic",
        }, clear=False):
            with patch("urllib.request.urlopen") as urlopen:
                finding = agent.analyze(context)

        urlopen.assert_not_called()
        self.assertEqual(finding.evidence[0].raw["localizedTitle"], "엔비디아, 실적 호조에 상승")
        self.assertEqual(finding.evidence[0].raw["localizedSummary"], "엔비디아 매출이 기대치를 웃돌았습니다.")

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
        self.assertEqual(answer.citations, [])

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
            "layoutContext": layout_context(),
            "chartContext": {
                "chartDocument": {"symbol": "NVDA", "timeframe": "1m"},
                "dataStatus": {"candleCount": 10, "state": "ready"},
            },
        })

        self.assertEqual(report.symbol, "XLV")
        self.assertEqual(clickhouse.requested_symbols, ["XLV"])
        self.assertEqual(report.finalAnswer.title, "뉴스를 가져왔습니다")
        self.assertIsNone(report.layoutProposal)
        self.assertEqual(report.providerEvidence[0].raw.get("symbol"), "XLV")

    def test_lowercase_ticker_in_intent_normalizes_against_known_universe(self):
        self.assertEqual(extract_symbol_from_intent("acgl 뉴스 보여줘"), "ACGL")
        self.assertIsNone(extract_symbol_from_intent("show news about it"))

        clickhouse = FakeClickHouseProvider([
            {
                "articleId": "acgl-1",
                "headline": "Arch Capital shares rise after insurance update",
                "summary": "ACGL insurance results improved.",
                "publishedAt": utc_now_iso(),
                "url": "https://example.com/acgl",
                "symbols": ["ACGL"],
                "source": "alpaca",
            }
        ])
        provider = ClickHouseNewsProvider(clickhouse_provider=clickhouse, publish_fallback=False)
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(provider)

        report = orchestrator.analyze({
            "symbol": "NVDA",
            "intent": "acgl 뉴스 보여줘",
            "agentIds": ["agent-02"],
            "chartContext": {
                "chartDocument": {"symbol": "NVDA", "timeframe": "1m"},
                "dataStatus": {"candleCount": 10, "state": "ready"},
            },
            "layoutContext": layout_context(),
        })

        self.assertEqual(report.symbol, "ACGL")
        self.assertEqual(clickhouse.requested_symbols, ["ACGL"])
        self.assertIsNone(report.layoutProposal)
        self.assertEqual(report.providerEvidence[0].raw.get("symbol"), "ACGL")

    def test_explicit_ticker_with_no_news_does_not_fallback_to_current_chart_symbol(self):
        clickhouse = FakeClickHouseProvider([])
        provider = ClickHouseNewsProvider(clickhouse_provider=clickhouse, redis_provider=FakeLocalizedRedisProvider([]), publish_fallback=False)
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(provider)

        report = orchestrator.analyze({
            "symbol": "NVDA",
            "intent": "acgl 뉴스 보여줘",
            "agentIds": ["agent-02"],
            "chartContext": {
                "chartDocument": {"symbol": "NVDA", "timeframe": "1m"},
                "dataStatus": {"candleCount": 10, "state": "ready"},
            },
            "layoutContext": layout_context(),
        })

        self.assertEqual(report.symbol, "ACGL")
        self.assertEqual(clickhouse.requested_symbols, ["ACGL"])
        self.assertEqual(report.finalAnswer.summary, "ACGL 관련 저장 뉴스가 없습니다.")
        self.assertIsNone(report.layoutProposal)

    def test_company_name_alias_in_intent_overrides_current_chart_symbol_for_news(self):
        self.assertEqual(extract_symbol_from_intent("애플 뉴스 찾아줘"), "AAPL")
        self.assertEqual(extract_symbol_from_intent("Apple latest news"), "AAPL")
        self.assertEqual(extract_symbol_from_intent("엔비디아 관련 뉴스"), "NVDA")

        clickhouse = FakeClickHouseProvider([
            {
                "articleId": "aapl-1",
                "headline": "Apple shares rise after services growth improves",
                "summary": "Apple services revenue improved investor sentiment.",
                "publishedAt": utc_now_iso(),
                "url": "https://example.com/aapl",
                "symbols": ["AAPL"],
                "source": "alpaca",
            }
        ])
        provider = ClickHouseNewsProvider(clickhouse_provider=clickhouse, publish_fallback=False)
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(provider)

        report = orchestrator.analyze({
            "symbol": "NVDA",
            "intent": "애플 뉴스 찾아줘",
            "agentIds": ["agent-02"],
            "chartContext": {
                "chartDocument": {"symbol": "NVDA", "timeframe": "1m"},
                "dataStatus": {"candleCount": 10, "state": "ready"},
            },
        })

        self.assertEqual(report.symbol, "AAPL")
        self.assertEqual(clickhouse.requested_symbols, ["AAPL"])
        self.assertEqual(report.finalAnswer.title, "뉴스를 가져왔습니다")
        self.assertIsNone(report.layoutProposal)
        self.assertEqual(report.providerEvidence[0].raw.get("symbol"), "AAPL")

    def test_korean_entity_resolver_handles_compact_typos_and_choseong(self):
        cases = {
            "애플뉴스알려줘": "AAPL",
            "apple뉴스알려줘": "AAPL",
            "애플ㄹ 뉴스": "AAPL",
            "엔비댜 왜 오름": "NVDA",
            "테슬랴 급등 이유": "TSLA",
            "ㅇㅂㄷㅇ 왜오름": "NVDA",
            "ㅌㅅㄹ 뉴스": "TSLA",
            "마소 실적 어때": "MSFT",
        }
        for intent, symbol in cases.items():
            with self.subTest(intent=intent):
                self.assertEqual(extract_symbol_from_intent(intent), symbol)

        self.assertIsNone(extract_symbol_from_intent("ㅇㅍ 뉴스"))
        self.assertIsNone(extract_symbol_from_intent("ㅁㅌ 뉴스"))
        self.assertIsNone(extract_symbol_from_intent("사과 뉴스"))
        self.assertIsNone(extract_symbol_from_intent("차트랑 뉴스 나란히 보여줘"))
        self.assertIsNone(extract_symbol_from_intent("차트를 아래로 옮겨줘"))
        self.assertIsNone(extract_symbol_from_intent("이 봉 분석해줘"))
        self.assertIsNone(extract_symbol_from_intent("UI 바꿔줘 온톨로지 기반으로"))

    def test_dynamic_entity_index_resolves_non_seed_company_alias(self):
        records = [
            EntityAliasRecord(
                alias="샘플전자",
                entity_id="company:ZZZZ",
                entity_type="company",
                symbol="ZZZZ",
                canonical_name="Sample Electronics Inc.",
                source="test-catalog",
                confidence=0.99,
                priority=0.99,
            ),
            EntityAliasRecord(
                alias="sample electronics",
                entity_id="company:ZZZZ",
                entity_type="company",
                symbol="ZZZZ",
                canonical_name="Sample Electronics Inc.",
                source="test-catalog",
                confidence=0.99,
                priority=0.99,
            ),
        ]
        resolver = KoreanEntityResolver(index=EntityAliasIndex.from_records(records, known_symbols=["ZZZZ"]))

        resolution = resolver.resolve("샘플전자 뉴스 알려줘")

        self.assertEqual(resolution.status, "confirmed")
        self.assertEqual(resolution.symbol, "ZZZZ")
        self.assertEqual(resolution.entity_type, "company")
        self.assertEqual(resolution.catalog_source, "test-catalog")
        self.assertEqual(resolver.resolve("zzzz 뉴스").symbol, "ZZZZ")

    def test_default_entity_catalog_uses_operational_alias_file_without_seed_aliases(self):
        catalog = EntityCatalogProvider(
            sparql_client=FakeSparqlClient(),
            include_dynamic_company_sources=False,
            include_fallback_seed=True,
        ).load()
        resolver = KoreanEntityResolver(index=EntityAliasIndex.from_catalog(catalog))

        self.assertEqual(resolver.resolve("어도비 뉴스").symbol, "ADBE")
        self.assertEqual(resolver.resolve("브로드컴 분석").symbol, "AVGO")
        self.assertEqual(resolver.resolve("버크셔 해서웨이").symbol, "BRK.B")
        self.assertIn("GOOGL", catalog.known_symbols)
        self.assertIn("GOOG", catalog.known_symbols)
        self.assertNotIn("fallback-seed", catalog.source_counts)

    def test_entity_catalog_falls_back_to_seed_artifact_when_primary_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing-aliases.json"
            catalog = EntityCatalogProvider(
                alias_catalog_path=missing,
                sparql_client=FakeSparqlClient(),
                include_dynamic_company_sources=False,
                include_fallback_seed=True,
                strict=False,
            ).load()

        self.assertIn("AAPL", catalog.known_symbols)
        self.assertIn("NVDA", catalog.known_symbols)
        self.assertNotIn("ADBE", catalog.known_symbols)

    def test_entity_catalog_strict_mode_rejects_broken_primary_catalog(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            broken = Path(temp_dir) / "broken-aliases.json"
            broken.write_text("{not json", encoding="utf-8")
            provider = EntityCatalogProvider(
                alias_catalog_path=broken,
                sparql_client=FakeSparqlClient(),
                include_dynamic_company_sources=False,
                strict=True,
            )

            with self.assertRaises(ValueError):
                provider.load()

    def test_supported_company_catalog_uses_market_symbol_registry_not_alias_catalog(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = Path(temp_dir) / "market-registry.json"
            registry.write_text(json.dumps({"symbols": ["NVDA"]}), encoding="utf-8")
            with patch.dict(os.environ, {"AGENT_MARKET_SYMBOL_REGISTRY_PATH": str(registry)}, clear=False):
                supported_company_catalog.cache_clear()
                market_symbol_registry_supports.cache_clear()

                self.assertTrue(is_supported_company_symbol("NVDA"))
                self.assertFalse(is_supported_company_symbol("ADBE"))

            supported_company_catalog.cache_clear()
            market_symbol_registry_supports.cache_clear()

    def test_dynamic_entity_index_handles_large_symbol_catalog(self):
        records = []
        symbols = []
        for index in range(6000):
            symbol = f"Z{index:04d}"
            symbols.append(symbol)
            records.extend([
                EntityAliasRecord(
                    alias=f"테스트기업{index}",
                    entity_id=f"company:{symbol}",
                    entity_type="company",
                    symbol=symbol,
                    canonical_name=f"Test Company {index}",
                    source="large-test-catalog",
                    confidence=0.99,
                    priority=0.99,
                ),
                EntityAliasRecord(
                    alias=f"test company {index}",
                    entity_id=f"company:{symbol}",
                    entity_type="company",
                    symbol=symbol,
                    canonical_name=f"Test Company {index}",
                    source="large-test-catalog",
                    confidence=0.99,
                    priority=0.99,
                ),
                EntityAliasRecord(
                    alias=f"테기{index}",
                    entity_id=f"company:{symbol}",
                    entity_type="company",
                    symbol=symbol,
                    canonical_name=f"Test Company {index}",
                    source="large-test-catalog",
                    confidence=0.99,
                    priority=0.99,
                ),
            ])
        resolver = KoreanEntityResolver(index=EntityAliasIndex.from_records(records, known_symbols=symbols))

        resolution = resolver.resolve("테스트기업5999 뉴스")

        self.assertEqual(resolution.status, "confirmed")
        self.assertEqual(resolution.symbol, "Z5999")
        self.assertEqual(resolution.catalog_source, "large-test-catalog")

    def test_dynamic_theme_entity_resolution_returns_theme_symbols(self):
        records = [
            EntityAliasRecord(
                alias="헬스케어",
                entity_id="theme:healthcare",
                entity_type="theme",
                canonical_name="헬스케어",
                source="test-graphdb",
                confidence=0.98,
                priority=0.98,
                theme_name="헬스케어",
                theme_symbols=("UNH", "PFE", "MRK"),
                theme_category="sector",
            )
        ]
        resolver = KoreanEntityResolver(index=EntityAliasIndex.from_records(records, known_symbols=["UNH", "PFE", "MRK"]))

        resolution = resolver.resolve("헬스케어 주 뉴스")

        self.assertEqual(resolution.status, "confirmed")
        self.assertEqual(resolution.entity_type, "theme")
        self.assertIsNone(resolution.symbol)
        self.assertEqual(resolution.theme_name, "헬스케어")
        self.assertEqual(resolution.theme_symbols, ("UNH", "PFE", "MRK"))

    def test_theme_entity_resolution_feeds_news_symbol_fanout(self):
        clickhouse = ThemeClickHouseProvider({
            "UNH": [
                {
                    "articleId": "unh-1",
                    "headline": "UnitedHealth rises with healthcare peers",
                    "summary": "Healthcare stocks gained.",
                    "publishedAt": utc_now_iso(),
                    "url": "https://example.com/unh",
                    "symbols": ["UNH"],
                    "source": "alpaca",
                }
            ],
            "PFE": [
                {
                    "articleId": "pfe-1",
                    "headline": "Pfizer shares move after sector update",
                    "summary": "Healthcare sector news lifted pharma shares.",
                    "publishedAt": utc_now_iso(),
                    "url": "https://example.com/pfe",
                    "symbols": ["PFE"],
                    "source": "alpaca",
                }
            ],
        })
        provider = ClickHouseNewsProvider(clickhouse_provider=clickhouse, publish_fallback=False)
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(provider)
        resolution = EntityResolution(
            status="confirmed",
            canonical_name="헬스케어",
            confidence=0.98,
            match_type="alias_exact",
            matched_text="헬스케어",
            matched_alias="헬스케어",
            entity_type="theme",
            entity_id="theme:healthcare",
            catalog_source="test-graphdb",
            theme_name="헬스케어",
            theme_symbols=("UNH", "PFE"),
        )

        with patch("gops_agents.intent_understanding.fanout.resolve_entity", return_value=resolution):
            report = orchestrator.analyze({
                "symbol": "NVDA",
                "intent": "헬스케어 주 뉴스",
                "agentIds": ["agent-02"],
                "layoutContext": layout_context(),
            })

        self.assertEqual(report.symbol, "헬스케어")
        self.assertEqual(clickhouse.requested_symbols, ["UNH", "PFE"])
        self.assertEqual(report.agentTrace["entityResolution"]["entityType"], "theme")
        self.assertEqual(report.agentTrace["entityResolution"]["themeSymbols"], ["UNH", "PFE"])

    def test_korean_entity_resolution_trace_is_reported(self):
        clickhouse = FakeClickHouseProvider([
            {
                "articleId": "aapl-typo-1",
                "headline": "Apple shares rise after services growth improves",
                "summary": "Apple services revenue improved investor sentiment.",
                "publishedAt": utc_now_iso(),
                "url": "https://example.com/aapl-typo",
                "symbols": ["AAPL"],
                "source": "alpaca",
            }
        ])
        provider = ClickHouseNewsProvider(clickhouse_provider=clickhouse, publish_fallback=False)
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(provider)

        report = orchestrator.analyze({
            "symbol": "NVDA",
            "intent": "얘플 뉴스 찾아줘",
            "agentIds": ["agent-02"],
            "chartContext": {
                "chartDocument": {"symbol": "NVDA", "timeframe": "1m"},
                "dataStatus": {"candleCount": 10, "state": "ready"},
            },
        })

        self.assertEqual(report.symbol, "NVDA")
        self.assertEqual(clickhouse.requested_symbols, ["NVDA"])
        self.assertNotEqual(report.agentTrace["entityResolution"]["matchType"], "alias_fuzzy")

    def test_news_only_request_skips_runtime_openai_and_uses_fast_deterministic_answer(self):
        clickhouse = FakeClickHouseProvider([
            {
                "articleId": "aapl-fast-1",
                "headline": "Apple CEO Tim Cook flags memory chip shortage",
                "summary": "Apple CEO Tim Cook discussed an extreme memory chip shortage.",
                "publishedAt": utc_now_iso(),
                "url": "https://example.com/aapl-fast",
                "symbols": ["AAPL"],
                "source": "alpaca",
            }
        ])
        provider = ClickHouseNewsProvider(
            clickhouse_provider=clickhouse,
            redis_provider=FakeLocalizedRedisProvider([]),
            publish_fallback=False,
        )
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(provider)

        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-key",
            "AGENT_NEWS_LOCALIZATION_PROVIDER": "openai",
        }, clear=False):
            with patch("urllib.request.urlopen", side_effect=AssertionError("runtime OpenAI should not be called for news-only requests")):
                report = orchestrator.analyze({
                    "symbol": "NVDA",
                    "intent": "애플 뉴스 보여줘",
                    "agentIds": ["agent-02"],
                    "chartContext": {
                        "chartDocument": {"symbol": "NVDA", "timeframe": "1m"},
                        "dataStatus": {"candleCount": 10, "state": "ready"},
                    },
                })

        self.assertEqual(report.symbol, "AAPL")
        self.assertEqual(report.timing["llmCalls"], 0)
        self.assertEqual(report.timing["directNewsCount"], 1)
        self.assertLess(report.timing["roleAnalysisMs"], 10)
        self.assertLess(report.timing["finalAnswerMs"], 100)
        self.assertEqual(report.finalAnswer.title, "뉴스를 가져왔습니다")

    def test_news_panel_receives_company_daily_summaries(self):
        redis = FakeLocalizedRedisProvider(
            [],
            daily_rows=[
                {
                    "date": "2026-07-02",
                    "symbol": "AAPL",
                    "summary": "Redis hot cache 일일 뉴스 요약입니다.",
                    "articleIds": ["redis-daily-1"],
                }
            ],
        )
        clickhouse = FakeLocalizedClickHouseProvider(
            [],
            daily_rows=[
                {
                    "date": "2026-07-01",
                    "symbol": "AAPL",
                    "summary": "애플 관련 일일 뉴스 요약입니다.",
                    "keyPoints": ["서비스 성장", "공급망 점검"],
                    "positivePoints": ["서비스 매출 기대"],
                    "concerns": [],
                    "impactDirection": "positive",
                    "sentiment": "positive",
                    "articleIds": ["aapl-daily-1"],
                    "articleCount": 1,
                    "mentionCount": 0,
                    "status": "final",
                    "generatedAt": "2026-07-01T22:00:00.000Z",
                    "sources": [
                        {
                            "articleId": "aapl-daily-1",
                            "title": "Apple services revenue grows",
                            "name": "Example News",
                            "url": "https://example.com/aapl-services",
                            "publishedAt": "2026-07-01T12:00:00.000Z",
                        }
                    ],
                }
            ],
            candles=[
                {"timestamp": "2026-06-30T00:00:00.000Z", "close": 199.85},
                {"timestamp": "2026-07-01T00:00:00.000Z", "close": 200.00},
            ],
        )
        provider = ClickHouseNewsProvider(
            clickhouse_provider=clickhouse,
            redis_provider=redis,
            publish_fallback=False,
        )
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(provider)

        report = orchestrator.analyze({
            "symbol": "NVDA",
            "intent": "애플 뉴스 보여줘",
            "agentIds": ["agent-02"],
        })

        self.assertEqual(report.symbol, "AAPL")
        self.assertEqual(clickhouse.daily_calls, 1)
        self.assertEqual(clickhouse.daily_requested_days, [30])
        self.assertEqual(redis.daily_calls, 1)
        self.assertEqual(redis.daily_warm_calls[0]["days"], 30)
        self.assertEqual(report.dailySummaries[0]["summary"], "애플 관련 일일 뉴스 요약입니다.")
        self.assertIn("일일 뉴스 요약", report.finalAnswer.summary)
        self.assertIsNone(report.layoutProposal)
        self.assertEqual(report.dailySummaries[0]["date"], "2026-07-01")
        self.assertEqual(report.dailySummaries[0]["keyPoints"], ["서비스 성장", "공급망 점검"])
        self.assertEqual(report.dailySummaries[0]["sources"][0]["url"], "https://example.com/aapl-services")
        self.assertEqual(report.dailySummaries[0]["priceChange"]["change"], 0.15)
        self.assertEqual(report.finalAnswer.sections, [])

    def test_news_role_fallback_applies_daily_summaries_after_join(self):
        provider = SequencedDailyNewsProvider()
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(provider)

        with patch.dict(os.environ, {
            "AGENT_FINAL_ANSWER_PROVIDER": "deterministic",
            "AGENT_NEWS_LOCALIZATION_PROVIDER": "deterministic",
            "AGENT_UI_ROUTER_PROVIDER": "deterministic",
            "AGENT_USE_SNAPSHOT_HOT_PATH": "false",
        }, clear=False):
            report = orchestrator.analyze({
                "symbol": "AAPL",
                "intent": "AAPL 뉴스 보여줘",
                "agentIds": ["agent-02"],
            })

        self.assertEqual(provider.daily_calls, 2)
        self.assertEqual(report.dailySummaries[0]["summary"], "fallback role daily summary")
        self.assertIsNone(report.layoutProposal)
        self.assertEqual(report.dailySummaries[0]["keyPoints"], ["role fallback"])

    def test_topic_news_intent_uses_theme_basket_instead_of_current_chart_symbol(self):
        clickhouse = ThemeClickHouseProvider({
            "AMD": [
                {
                    "articleId": "semi-amd-1",
                    "headline": "AMD shares rise on AI chip demand",
                    "summary": "Semiconductor demand lifted AMD shares.",
                    "publishedAt": utc_now_iso(),
                    "url": "https://example.com/amd-semi",
                    "symbols": ["AMD"],
                    "source": "alpaca",
                }
            ],
            "MU": [
                {
                    "articleId": "semi-mu-1",
                    "headline": "Micron gains as memory chip demand improves",
                    "summary": "HBM and memory demand supported Micron.",
                    "publishedAt": utc_now_iso(),
                    "url": "https://example.com/mu-semi",
                    "symbols": ["MU"],
                    "source": "alpaca",
                }
            ],
        })
        provider = ClickHouseNewsProvider(clickhouse_provider=clickhouse, publish_fallback=False)
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(provider)

        report = orchestrator.analyze({
            "symbol": "NVDA",
            "intent": "반도체 관련 뉴스 보여줘",
            "agentIds": ["agent-02"],
            "layoutContext": layout_context(),
            "chartContext": {
                "chartDocument": {"symbol": "NVDA", "timeframe": "1m"},
                "dataStatus": {"candleCount": 10, "state": "ready"},
            },
        })

        self.assertEqual(report.symbol, "반도체")
        self.assertIn("AMD", clickhouse.requested_symbols)
        self.assertIn("MU", clickhouse.requested_symbols)
        self.assertNotEqual(clickhouse.requested_symbols, ["NVDA"])
        self.assertEqual(report.finalAnswer.title, "뉴스를 가져왔습니다")
        self.assertIsNone(report.layoutProposal)
        self.assertTrue({"AMD", "MU"}.issubset({item.raw.get("symbol") for item in report.providerEvidence}))

    def test_topic_news_intent_can_use_prelocalized_redis_basket_evidence(self):
        redis = FakeLocalizedRedisProvider([
            {
                "articleId": "semi-amd-ko-1",
                "symbol": "AMD",
                "symbols": ["AMD"],
                "headline": "AMD rises on AI chip demand",
                "summary": "AI chip demand lifted AMD shares.",
                "localizedHeadline": "AMD, AI 칩 수요에 상승",
                "localizedSummary": "AI 칩 수요가 AMD 주가를 끌어올렸습니다.",
                "publishedAt": utc_now_iso(),
                "url": "https://example.com/amd-ko",
            },
            {
                "articleId": "semi-mu-ko-1",
                "symbol": "MU",
                "symbols": ["MU"],
                "headline": "Micron gains on memory demand",
                "summary": "Memory demand supported Micron.",
                "localizedHeadline": "마이크론, 메모리 수요 개선에 상승",
                "localizedSummary": "메모리 수요 개선이 마이크론에 긍정적으로 작용했습니다.",
                "publishedAt": utc_now_iso(),
                "url": "https://example.com/mu-ko",
            },
        ])
        clickhouse = ThemeClickHouseProvider({})
        provider = ClickHouseNewsProvider(clickhouse_provider=clickhouse, redis_provider=redis, publish_fallback=False)
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(provider)

        report = orchestrator.analyze({
            "symbol": "NVDA",
            "intent": "반도체 관련 뉴스 보여줘",
            "agentIds": ["agent-02"],
            "chartContext": {
                "chartDocument": {"symbol": "NVDA", "timeframe": "1m"},
                "dataStatus": {"candleCount": 10, "state": "ready"},
            },
        })

        requested = redis.requested_symbols[0]
        self.assertIn("AMD", requested)
        self.assertIn("MU", requested)
        self.assertEqual(clickhouse.calls, 0)
        self.assertEqual(report.symbol, "반도체")
        self.assertTrue(any(item.raw.get("localizedTitle") == "AMD, AI 칩 수요에 상승" for item in report.providerEvidence))

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

        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-key",
            "AGENT_NEWS_LOCALIZATION_PROVIDER": "deterministic",
            "AGENT_ROLE_ANALYSIS_PROVIDER": "openai",
        }, clear=False):
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
                        "sector": {"value": "Information Technology"},
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
        self.assertEqual(evidence[0].raw["sector"], "Information Technology")
        self.assertEqual(evidence[0].raw["controlledName"], "Mellanox Technologies")
        self.assertEqual(evidence[0].raw["accession"], "0001045810-24-000001")
        self.assertEqual(evidence[0].url, "https://www.sec.gov/example")
        self.assertTrue(any(item.raw.get("relationType") == "control" for item in evidence))

    def test_graphdb_provider_filters_control_relationship_note_labels(self):
        sparql_client = FakeSparqlClient()
        provider = GraphDBOntologyProvider(sparql_client=sparql_client, limit=3, cache=MemoryGraphPathCache())

        provider.fetch(ProviderRequest("AMAT", "자회사 관계 분석"))

        control_queries = [
            query
            for query in sparql_client.queries
            if "DerivedControlRelationship" in query and 'FILTER (UCASE(STR(?ticker)) = "AMAT")' in query
        ]
        self.assertTrue(control_queries)
        self.assertIn("following subsidiar", control_queries[0])
        self.assertIn("partially own", control_queries[0])
        self.assertIn("legal entity name", control_queries[0])

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

    def test_graphdb_provider_multi_symbol_shared_theme_evidence(self):
        payload = {
            "results": {
                "bindings": [
                    {
                        "ticker": {"value": "NVDA"},
                        "companyName": {"value": "NVIDIA Corp"},
                        "themeName": {"value": "AI/반도체/데이터센터"},
                    }
                ]
            }
        }
        provider = GraphDBOntologyProvider(sparql_client=FakeSparqlClient(payload), limit=10, cache=MemoryGraphPathCache())

        evidence = provider.fetch(ProviderRequest("NVDA", "NVDA와 AMD 관계 분석", symbols=("NVDA", "AMD")))

        shared_theme_items = [item for item in evidence if item.raw.get("relationType") == "shared-theme"]
        self.assertTrue(shared_theme_items)
        self.assertEqual(shared_theme_items[0].raw["themeName"], "AI/반도체/데이터센터")
        self.assertEqual(set(shared_theme_items[0].raw["symbols"]), {"NVDA", "AMD"})

    def test_graphdb_provider_multi_symbol_cross_control_evidence(self):
        payloads = [
            {"results": {"bindings": [{
                "ticker": {"value": "MSFT"},
                "companyName": {"value": "MICROSOFT CORP"},
                "themeName": {"value": "클라우드/소프트웨어/사이버보안"},
            }]}},
            {"results": {"bindings": [{
                "ticker": {"value": "MSFT"},
                "companyName": {"value": "MICROSOFT CORP"},
                "controlledName": {"value": "Activision Blizzard, Inc."},
                "confidence": {"value": "explicit"},
                "sourceUrl": {"value": "https://www.sec.gov/msft-ex21"},
            }]}},
            {"results": {"bindings": [{
                "ticker": {"value": "ATVI"},
                "companyName": {"value": "Activision Blizzard, Inc."},
                "themeName": {"value": "인터넷 플랫폼/미디어/광고"},
            }]}},
            {"results": {"bindings": []}},
        ]
        provider = GraphDBOntologyProvider(sparql_client=QueueSparqlClient(payloads), limit=10, cache=MemoryGraphPathCache())

        evidence = provider.fetch(ProviderRequest("MSFT", "MSFT와 ATVI 관계 분석", symbols=("MSFT", "ATVI")))

        cross_control_items = [item for item in evidence if item.raw.get("relationType") == "cross-control"]
        self.assertTrue(cross_control_items)
        self.assertEqual(cross_control_items[0].raw["controllerTicker"], "MSFT")
        self.assertEqual(cross_control_items[0].raw["controlledTicker"], "ATVI")
        self.assertEqual(cross_control_items[0].url, "https://www.sec.gov/msft-ex21")

    def test_graphdb_provider_multi_symbol_no_shared_relationship(self):
        payloads = [
            {"results": {"bindings": [{
                "ticker": {"value": "NVDA"},
                "companyName": {"value": "NVIDIA Corp"},
                "themeName": {"value": "AI/반도체/데이터센터"},
            }]}},
            {"results": {"bindings": []}},
            {"results": {"bindings": [{
                "ticker": {"value": "KO"},
                "companyName": {"value": "COCA COLA CO"},
                "themeName": {"value": "필수소비재"},
            }]}},
            {"results": {"bindings": []}},
        ]
        provider = GraphDBOntologyProvider(sparql_client=QueueSparqlClient(payloads), limit=10, cache=MemoryGraphPathCache())

        evidence = provider.fetch(ProviderRequest("NVDA", "NVDA와 KO 관계 분석", symbols=("NVDA", "KO")))

        self.assertTrue(any(item.raw.get("relationType") == "no-shared-relationship" for item in evidence))

    def test_graphdb_provider_caches_results_and_skips_repeat_sparql_queries(self):
        payload = {
            "results": {
                "bindings": [
                    {
                        "ticker": {"value": "NVDA"},
                        "companyName": {"value": "NVIDIA Corp"},
                        "themeName": {"value": "AI/반도체/데이터센터"},
                    }
                ]
            }
        }
        sparql_client = FakeSparqlClient(payload)
        provider = GraphDBOntologyProvider(sparql_client=sparql_client, limit=5, cache=MemoryGraphPathCache())
        request = ProviderRequest("NVDA", "관계 분석")

        first = provider.fetch(request)
        queries_after_first = len(sparql_client.queries)
        second = provider.fetch(request)

        self.assertGreater(queries_after_first, 0)
        self.assertEqual(len(sparql_client.queries), queries_after_first)
        self.assertEqual([item.summary for item in first], [item.summary for item in second])

    def test_extract_relationship_symbols_from_intent_finds_multiple_tickers_in_order(self):
        symbols = extract_relationship_symbols_from_intent("NVDA와 AMD 비교해서 관계 분석해줘")
        self.assertEqual(symbols, ("NVDA", "AMD"))

    def test_relationship_symbols_for_context_includes_primary_symbol(self):
        symbols = relationship_symbols_for_context("엔비디아랑 AMD 관계 어때", "NVDA")
        self.assertEqual(symbols, ("NVDA", "AMD"))

        fallback = relationship_symbols_for_context("아무 종목도 언급 안 함", "TSLA")
        self.assertEqual(fallback, ("TSLA",))

        unresolved_primary = relationship_symbols_for_context("NVDA와 AMD 관계 어때", "UNKNOWN")
        self.assertEqual(unresolved_primary, ("NVDA", "AMD"))

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
        self.assertIn("관계 데이터 기준", finding.summary)
        self.assertNotIn("CUDA 공급망", finding.summary)

    def test_legacy_agent_ids_do_not_route_unclear_query_to_role(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "analyze",
            "agentIds": ["agent-04"],
        })

        roles = {finding.role for finding in report.findings}
        self.assertEqual(report.route.selectedRoles, [])
        self.assertEqual(report.route.intentType, "clarify")
        self.assertEqual(roles, set())
        self.assertEqual(report.providerEvidence, [])
        self.assertEqual(report.finalAnswer.title, "추가 확인 필요")

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

    def test_clear_order_panel_resize_skips_llm_classifier(self):
        response = {
            "output_text": json.dumps({
                "contentTasks": [],
                "uiTasks": [
                    {
                        "targetPanelType": "orderTicket",
                        "targetPanelId": "panel-order",
                        "action": "resize",
                        "sizeIntent": "max",
                        "confidence": 0.94,
                        "reason": "The user asked to maximize the order entry panel.",
                    }
                ],
                "routeMode": "ui_layout",
                "confidence": 0.94,
                "warnings": [],
            })
        }

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "AGENT_INTENT_CLASSIFIER_PROVIDER": "openai"}, clear=False):
            with patch("urllib.request.urlopen", return_value=FakeOpenAIResponse(response)) as openai:
                report = AgentOrchestrator().analyze({
                    "symbol": "NVDA",
                    "intent": "주문 입력 패널 제일 크게 만들어줘",
                    "agentIds": ["agent-01"],
                    "layoutContext": layout_context(),
                })

        self.assertEqual(openai.call_count, 0)
        self.assertEqual(report.route.source, "ui-parser")
        self.assertEqual(report.route.intentType, "ui-layout")
        self.assertEqual(report.route.selectedRoles, [])
        self.assertEqual(report.findings, [])
        self.assertEqual(report.providerEvidence, [])
        # max is now an exact full-grid request. This legacy 4x5 fixture has
        # three supporting panels, so the agent refuses instead of silently
        # downgrading to the former 3x5 cap or a merely-large 4x4 span.
        self.assertFalse(report.layoutProposal.autoApply)
        self.assertEqual(report.layoutProposal.commands, [])
        self.assertIn("공간", report.layoutProposal.rationale)

    def test_ui_fallback_handles_informal_order_panel_resize_when_llm_unavailable(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "주문창좀 젤 크게",
            "agentIds": ["agent-01"],
            "layoutContext": layout_context(),
        })

        self.assertEqual(report.route.source, "ui-parser")
        self.assertEqual(report.route.intentType, "ui-layout")
        self.assertEqual(report.route.selectedRoles, [])
        self.assertEqual(report.findings, [])
        self.assertFalse(report.layoutProposal.autoApply)
        self.assertEqual(report.layoutProposal.commands, [])
        self.assertIn("공간", report.layoutProposal.rationale)

    def test_mixed_news_and_ui_query_runs_analysis_and_ui_agent(self):
        clickhouse = FakeClickHouseProvider([
            {
                "articleId": "aapl-hybrid-1",
                "headline": "Apple announces new services revenue milestone",
                "summary": "Apple services revenue reached a new high.",
                "publishedAt": utc_now_iso(),
                "url": "https://example.com/aapl-hybrid",
                "symbols": ["AAPL"],
                "source": "alpaca",
            }
        ])
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(ClickHouseNewsProvider(clickhouse_provider=clickhouse, publish_fallback=False))

        with patch.dict(os.environ, {
            "AGENT_FINAL_ANSWER_PROVIDER": "deterministic",
            "AGENT_NEWS_LOCALIZATION_PROVIDER": "deterministic",
        }, clear=False):
            report = orchestrator.analyze({
                "symbol": "NVDA",
                "intent": "뉴스 패널 키워주고 애플 뉴스 보여줘",
                "layoutContext": layout_context(),
            })

        self.assertEqual(report.symbol, "AAPL")
        self.assertEqual(clickhouse.requested_symbols, ["AAPL"])
        self.assertEqual(report.route.intentType, "news")
        self.assertEqual(report.route.selectedRoles, ["news"])
        self.assertEqual(report.agentTrace["queryUnderstanding"]["routeMode"], "hybrid")
        self.assertEqual(report.agentTrace["queryUnderstanding"]["resolvedSymbol"], "AAPL")
        self.assertTrue(report.agentTrace["queryUnderstanding"]["uiTasks"])
        self.assertTrue(any(finding.role == "news-analysis" for finding in report.findings))
        self.assertTrue(any(command["type"] == "layout.panels.arrange" for command in report.layoutProposal.commands))
        self.assertNotEqual(report.finalAnswer.summary, "UIAgent arranged 시장 뉴스 for the requested UI action.")

    def test_generic_many_panels_opens_default_workspace_set(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "패널 여러개 띄워줘",
            "layoutContext": layout_context(),
        })

        understanding = report.agentTrace["queryUnderstanding"]
        self.assertEqual(report.route.intentType, "ui-layout")
        self.assertEqual(understanding["routeMode"], "ui_layout")
        self.assertEqual(understanding["uiTasks"][0]["layoutPreset"], "default_workspace")
        self.assertEqual(understanding["uiTasks"][0]["targetPanelTypes"], ["chart", "newsFeed", "aiSummary"])
        add_commands = [command for command in report.layoutProposal.commands if command["type"] == "layout.panel.add"]
        self.assertEqual(add_commands[0]["payload"]["panelType"], "aiSummary")
        arrange_command = next(command for command in report.layoutProposal.commands if command["type"] == "layout.panels.arrange")
        placement_ids = {item["panelId"] for item in arrange_command["payload"]["placements"]}
        self.assertTrue({"panel-chart-primary", "panel-news", "panel-ai-summary"}.issubset(placement_ids))

    def test_explicit_multi_panel_command_uses_named_panel_set(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "차트 뉴스 온톨로지 패널 띄워줘",
            "layoutContext": layout_context(),
        })

        understanding = report.agentTrace["queryUnderstanding"]
        self.assertEqual(report.route.intentType, "ui-layout")
        self.assertEqual(report.findings, [])
        self.assertEqual(understanding["uiTasks"][0]["targetPanelTypes"], ["chart", "newsFeed", "ontologyGraph"])
        self.assertIsNone(understanding["uiTasks"][0]["layoutPreset"])
        self.assertFalse(any(command["type"] == "layout.panel.add" for command in report.layoutProposal.commands))
        arrange_command = next(command for command in report.layoutProposal.commands if command["type"] == "layout.panels.arrange")
        placement_ids = {item["panelId"] for item in arrange_command["payload"]["placements"]}
        self.assertTrue({"panel-chart-primary", "panel-news", "panel-ontology"}.issubset(placement_ids))

    def test_hybrid_news_and_generic_many_panels_runs_both_paths(self):
        clickhouse = FakeClickHouseProvider([
            {
                "articleId": "aapl-hybrid-panels-1",
                "headline": "Apple expands services business",
                "summary": "Apple services growth improved sentiment.",
                "publishedAt": utc_now_iso(),
                "url": "https://example.com/aapl-panels",
                "symbols": ["AAPL"],
                "source": "alpaca",
            }
        ])
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(ClickHouseNewsProvider(clickhouse_provider=clickhouse, publish_fallback=False))

        with patch.dict(os.environ, {
            "AGENT_FINAL_ANSWER_PROVIDER": "deterministic",
            "AGENT_NEWS_LOCALIZATION_PROVIDER": "deterministic",
        }, clear=False):
            report = orchestrator.analyze({
                "symbol": "NVDA",
                "intent": "애플 뉴스 보여주고 패널 여러개 띄워줘",
                "layoutContext": layout_context(),
            })

        understanding = report.agentTrace["queryUnderstanding"]
        self.assertEqual(report.symbol, "AAPL")
        self.assertEqual(report.route.intentType, "news")
        self.assertEqual(understanding["routeMode"], "hybrid")
        self.assertEqual(understanding["uiTasks"][0]["layoutPreset"], "default_workspace")
        self.assertTrue(any(finding.role == "news-analysis" for finding in report.findings))
        self.assertTrue(any(command["type"] == "layout.panel.add" and command["payload"]["panelType"] == "aiSummary" for command in report.layoutProposal.commands))

    def test_ui_agent_preserves_pinned_panels_and_uses_largest_available_slot(self):
        context = layout_context()
        context["panels"][0]["layoutPinned"] = True

        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "뉴스 패널 제일 크게 만들어줘",
            "layoutContext": context,
        })

        self.assertFalse(report.layoutProposal.autoApply)
        self.assertEqual(report.layoutProposal.commands, [])
        self.assertIn("공간", report.layoutProposal.rationale)

    def test_ui_fallback_keeps_explicit_news_panel_placement_request(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "뉴스를 오른쪽에 띄워줘",
            "layoutContext": layout_context(),
        })

        self.assertEqual(report.route.source, "ui-parser")
        self.assertEqual(report.route.intentType, "ui-layout")
        self.assertEqual(report.findings, [])
        self.assertTrue(any(command["type"] == "layout.panels.arrange" for command in report.layoutProposal.commands))

    def test_ui_fallback_keeps_side_by_side_chart_news_request(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "차트랑 뉴스 나란히 보여줘",
            "layoutContext": layout_context(),
        })

        self.assertEqual(report.route.source, "ui-parser")
        self.assertEqual(report.route.intentType, "ui-layout")
        self.assertEqual(report.findings, [])
        self.assertTrue(any(command["type"] == "layout.panels.arrange" for command in report.layoutProposal.commands))

    def test_surface_action_keeps_close_request_in_ui_agent(self):
        report = AgentOrchestrator().analyze({
            "symbol": "NVDA",
            "intent": "주문창 닫아줘",
            "layoutContext": layout_context(),
        })

        self.assertEqual(report.route.source, "ui-parser")
        self.assertEqual(report.route.intentType, "ui-layout")
        self.assertEqual(report.route.selectedRoles, [])
        self.assertEqual(report.findings, [])
        self.assertTrue(any(
            command["type"] == "layout.panel.remove" and command["payload"]["panelId"] == "panel-order"
            for command in report.layoutProposal.commands
        ))
        self.assertIn("주문", report.layoutProposal.rationale)

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

    def test_layout_resolve_adds_chart_panel_from_chart_shortcut_action(self):
        response = AgentOrchestrator().resolve_layout({
            "symbol": "AAPL",
            "intent": "애플도 같이 보여줘",
            "chartAction": "add",
            "chartTargetSymbol": "AAPL",
            "chartPlacementIntent": "bottom",
            "layoutContext": layout_context(),
        })

        self.assertEqual(response["status"], "ui_layout")
        proposal = response["layoutProposal"]
        add_command = next(command for command in proposal["commands"] if command["type"] == "layout.panel.add")
        self.assertEqual(add_command["payload"]["panelType"], "chart")
        self.assertEqual(add_command["payload"]["props"]["symbol"], "AAPL")
        self.assertEqual(add_command["payload"]["layoutWeight"], 120)
        arrange_command = next(command for command in proposal["commands"] if command["type"] == "layout.panels.arrange")
        placements = {item["panelId"]: item["placement"] for item in arrange_command["payload"]["placements"]}
        self.assertEqual(placements["panel-chart-primary"]["row"], 2)
        self.assertEqual(placements[add_command["payload"]["panelId"]]["row"], 4)
        self.assertEqual(response["agentTrace"]["queryUnderstanding"]["routeMode"], "ui_layout")

    def test_chart_open_replaces_existing_single_chart_symbol_without_reflow(self):
        response = AgentOrchestrator().resolve_layout({
            "symbol": "TSLA",
            "intent": "테슬라 차트 띄워줘",
            "layoutContext": grid_v2_layout_context(chart_symbol="AAPL", include_support=True),
        })

        proposal = response["layoutProposal"]
        command_types = [command["type"] for command in proposal["commands"]]
        self.assertIn("layout.panel.props.update", command_types)
        self.assertNotIn("layout.panel.add", command_types)
        self.assertFalse(any(command["type"] == "layout.panels.arrange" for command in proposal["commands"]))
        props_command = next(command for command in proposal["commands"] if command["type"] == "layout.panel.props.update")
        self.assertEqual(props_command["payload"]["panelId"], "panel-chart-primary")
        self.assertEqual(props_command["payload"]["props"]["symbol"], "TSLA")

    def test_chart_open_without_existing_chart_uses_bottom_chart_and_no_gaps(self):
        response = AgentOrchestrator().resolve_layout({
            "symbol": "TSLA",
            "intent": "테슬라 차트 띄워줘",
            "layoutContext": grid_v2_layout_context(chart_symbol=None, include_support=True),
        })

        proposal = response["layoutProposal"]
        add_command = next(command for command in proposal["commands"] if command["type"] == "layout.panel.add")
        arrange_command = next(command for command in proposal["commands"] if command["type"] == "layout.panels.arrange")
        placements = {item["panelId"]: item["placement"] for item in arrange_command["payload"]["placements"]}
        chart_id = add_command["payload"]["panelId"]
        self.assertEqual(placements[chart_id], {"group": "workspace", "zone": "mainContext", "col": 1, "row": 4, "colSpan": 8, "rowSpan": 2})
        self.assertEqual(placements["panel-news"], {"group": "workspace", "zone": "main", "col": 1, "row": 1, "colSpan": 4, "rowSpan": 3})
        self.assertEqual(placements["panel-ontology"], {"group": "workspace", "zone": "mainContext", "col": 5, "row": 1, "colSpan": 4, "rowSpan": 3})
        assert_no_grid_gaps(self, arrange_command["payload"]["placements"])

    def test_multi_symbol_chart_request_creates_pair_layout(self):
        response = AgentOrchestrator().resolve_layout({
            "symbol": "TSLA",
            "intent": "테슬라랑 엔비디아 차트 보여줘",
            "layoutContext": grid_v2_layout_context(chart_symbol=None, include_support=True),
        })

        understanding = response["agentTrace"]["queryUnderstanding"]
        self.assertEqual([task["symbol"] for task in understanding["uiTasks"][:2]], ["TSLA", "NVDA"])
        proposal = response["layoutProposal"]
        add_commands = [command for command in proposal["commands"] if command["type"] == "layout.panel.add"]
        self.assertEqual([command["payload"]["props"]["symbol"] for command in add_commands], ["TSLA", "NVDA"])
        arrange_command = next(command for command in proposal["commands"] if command["type"] == "layout.panels.arrange")
        placements = {item["panelId"]: item["placement"] for item in arrange_command["payload"]["placements"]}
        self.assertEqual(placements[add_commands[0]["payload"]["panelId"]]["row"], 2)
        self.assertEqual(placements[add_commands[0]["payload"]["panelId"]]["rowSpan"], 2)
        self.assertEqual(placements[add_commands[1]["payload"]["panelId"]]["row"], 4)
        self.assertEqual(placements[add_commands[1]["payload"]["panelId"]]["rowSpan"], 2)
        self.assertEqual(placements["panel-news"]["row"], 1)
        self.assertEqual(placements["panel-ontology"]["row"], 1)
        self.assertEqual(placements["panel-news"]["colSpan"] + placements["panel-ontology"]["colSpan"], 8)
        assert_no_grid_gaps(self, arrange_command["payload"]["placements"])

    def test_chart_open_with_bottom_blocked_returns_placement_picker(self):
        response = AgentOrchestrator().resolve_layout({
            "symbol": "TSLA",
            "intent": "테슬라 차트 띄워줘",
            "layoutContext": grid_v2_layout_context(chart_symbol=None, include_support=True, pinned_bottom=True),
        })

        proposal = response["layoutProposal"]
        self.assertFalse(proposal["autoApply"])
        pick_command = next(command for command in proposal["commands"] if command["type"] == "layout.placement.pick")
        self.assertEqual(pick_command["payload"]["panelType"], "chart")
        self.assertEqual(pick_command["payload"]["symbol"], "TSLA")
        self.assertGreaterEqual(len(pick_command["payload"]["candidates"]), 2)
        for candidate in pick_command["payload"]["candidates"]:
            assert_no_grid_gaps(self, candidate["arrangement"])

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
        self.assertIn("관계 데이터 기준", answer.summary)
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

    def test_openai_synthesizer_uses_synthesis_input_payload_when_available(self):
        response = {
            "output_text": json.dumps({
                "title": "NVDA 통합 분석",
                "summary": "제 의견은, snapshot 근거를 바탕으로 요약했습니다.",
                "sections": [{"title": "근거", "bullets": ["뉴스 snapshot이 있습니다.", "차트 OHLCV를 사용했습니다."]}],
                "citations": [],
                "limitations": [],
            })
        }
        evidence = [
            EvidenceItem(
                provider="news",
                status="available",
                title="NVDA shares rise",
                summary="NVDA demand stayed strong.",
            )
        ]
        synthesis_input = SynthesisInput(
            run_id="run-test",
            original_prompt="NVDA 영향 분석",
            intent="news_impact_analysis",
            snapshots=[
                DataSnapshot(
                    snapshot_id="snapshot-test",
                    run_id="run-test",
                    snapshot_type="news_snapshot",
                    status="success",
                    source="cache",
                    cache_hit=True,
                    summary="뉴스 snapshot",
                    evidence=evidence,
                    confidence=0.7,
                )
            ],
        )

        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "test-key", "AGENT_ONTOLOGY_FINAL_ANSWER_PROVIDER": "openai"},
            clear=False,
        ):
            with patch("urllib.request.urlopen", return_value=FakeOpenAIResponse(response)) as urlopen:
                answer = FinalAnswerSynthesizer().synthesize(
                    symbol="NVDA",
                    intent="NVDA 영향 분석",
                    route=IntentRoute("rule", "ontology", ["ontology"], 0.9, "test"),
                    findings=[],
                    provider_evidence=evidence,
                    synthesis_input=synthesis_input,
                )

        request_payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        user_payload = json.loads(request_payload["input"][1]["content"])
        self.assertEqual(answer.title, "NVDA 통합 분석")
        self.assertEqual(answer.summary, "제 의견은 자료 근거를 바탕으로 요약했습니다.")
        self.assertEqual(answer.sections[0].bullets[0], "뉴스 자료가 있습니다.")
        self.assertEqual(answer.sections[0].bullets[1], "차트 시가·고가·저가·종가·거래량을 사용했습니다.")
        self.assertIn("synthesisInput", user_payload)
        self.assertNotIn("providerEvidence", user_payload)
        self.assertNotIn("findings", user_payload)

    def test_analysis_synthesis_policy_forbids_delegated_followups(self):
        synthesis_input = build_synthesis_input(
            run_id="run-policy",
            intent="price_move_cause",
            original_prompt="GE 주가 왜 내려갔어?",
            entities=[],
            snapshots=[],
            policy=RuntimePolicy(max_synthesis_output_tokens=350),
            route_plan=None,
        )

        policy = synthesis_input.output_policy
        delegated_section_title = "다음 " + "확인 " + "포인트"
        self.assertNotIn(delegated_section_title, policy["allowed_sections"])
        self.assertNotIn("데이터 한계", policy["allowed_sections"])
        self.assertIn("판단 근거", policy["allowed_sections"])
        self.assertIn("분석한 지표", policy["allowed_sections"])
        self.assertNotIn("prohibited_sections", policy)
        self.assertTrue(policy["forbid_user_todo_sections"])
        self.assertTrue(policy["do_not_delegate_missing_data"])
        self.assertTrue(policy["show_used_indicators"])
        self.assertEqual(ANALYSIS_ANSWER_POLICY["allowed_sections"], policy["allowed_sections"])

    def test_openai_synthesizer_filters_delegated_followup_sections(self):
        delegated_section_title = "다음 " + "확인 " + "포인트"
        legacy_evidence_title = "왜 " + "그렇게 " + "보나"
        response = {
            "output_text": json.dumps({
                "title": "GE 주가 변동 원인 분석",
                "summary": "GE 하락은 단일 뉴스보다 선택 구간 가격 흐름과 뉴스 성격을 함께 봐야 합니다.",
                "sections": [
                    {
                        "title": "핵심 판단",
                        "bullets": ["GE 하락은 확인된 뉴스만으로 단일 원인으로 확정하기 어렵습니다."],
                    },
                    {
                        "title": delegated_section_title,
                        "bullets": ["같은 구간의 지수/섹터 ETF/주요 peer 수익률을 비교하세요."],
                    },
                    {
                        "title": legacy_evidence_title,
                        "bullets": [
                            "장기 수익률 소개 뉴스는 선택 봉 하락의 직접 원인으로 보기 어렵습니다.",
                            "뉴스 발생 시각과 선택 봉의 가격·거래량 변화를 맞춰 보세요.",
                        ],
                    },
                ],
                "citations": [],
                "limitations": ["거래량 snapshot은 제공되지 않았습니다.", "추가 공시는 확인하세요."],
            })
        }
        evidence = [
            EvidenceItem(
                provider="market-data",
                status="available",
                title="GE selected chart",
                summary="GE selected range changed -1.23%.",
                raw={"visibleSummary": {"change": "-1.23%"}},
            ),
            EvidenceItem(
                provider="news",
                status="available",
                title="GE long-term return",
                summary="GE five-year return article.",
            ),
        ]
        timing = {}

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "AGENT_FINAL_ANSWER_PROVIDER": "openai"}, clear=False):
            with patch("urllib.request.urlopen", return_value=FakeOpenAIResponse(response)) as urlopen:
                answer = FinalAnswerSynthesizer().synthesize(
                    symbol="GE",
                    intent="GE 주가 왜 내려갔어?",
                    route=IntentRoute("rule", "market-move", ["chart", "news"], 0.9, "test"),
                    findings=[],
                    provider_evidence=evidence,
                    timing=timing,
                )

        request_payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        system_prompt = request_payload["input"][0]["content"]
        section_titles = [section.title for section in answer.sections]
        all_bullets = [bullet for section in answer.sections for bullet in section.bullets]

        self.assertEqual(timing["synthesisProvider"], "openai")
        self.assertIn("Do not create action-item, checklist, or user-to-do sections", system_prompt)
        self.assertIn("분석한 지표", system_prompt)
        self.assertNotIn("snapshot types", system_prompt)
        self.assertNotIn("missing snapshots", system_prompt)
        self.assertNotIn(delegated_section_title, system_prompt)
        self.assertNotIn(delegated_section_title, section_titles)
        self.assertIn("판단 근거", section_titles)
        self.assertNotIn(legacy_evidence_title, section_titles)
        self.assertFalse(any("비교하세요" in bullet or "맞춰 보세요" in bullet for bullet in all_bullets))
        self.assertEqual(answer.limitations, [])

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

        self.assertIn("관계 데이터 기준", answer.summary)

    def test_synthesizer_records_fallback_reason_when_openai_key_missing(self):
        timing = {}
        evidence = [
            EvidenceItem(provider="news", status="available", title="NVDA news", summary="NVDA demand stayed strong.")
        ]
        with patch.dict(os.environ, {"OPENAI_API_KEY": "", "AGENT_FINAL_ANSWER_PROVIDER": "openai"}, clear=False):
            answer = FinalAnswerSynthesizer().synthesize(
                symbol="NVDA",
                intent="NVDA 왜 올랐어?",
                route=IntentRoute("rule", "market-move", ["chart", "news"], 0.9, "test"),
                findings=[],
                provider_evidence=evidence,
                timing=timing,
            )

        self.assertEqual(timing["synthesisProvider"], "deterministic")
        self.assertEqual(timing["synthesisSkippedReason"], "missing_openai_api_key")
        self.assertEqual(timing["synthesisFallbackReason"], "missing_openai_api_key")
        self.assertNotIn("근거를 확인했습니다", answer.summary)

    def test_synthesizer_records_invalid_openai_response_fallback_reason(self):
        timing = {}
        evidence = [
            EvidenceItem(provider="news", status="available", title="NVDA news", summary="NVDA demand stayed strong.")
        ]
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "AGENT_FINAL_ANSWER_PROVIDER": "openai"}, clear=False):
            with patch("urllib.request.urlopen", return_value=FakeOpenAIResponse({"output_text": "{invalid"})):
                answer = FinalAnswerSynthesizer().synthesize(
                    symbol="NVDA",
                    intent="NVDA 왜 올랐어?",
                    route=IntentRoute("rule", "market-move", ["chart", "news"], 0.9, "test"),
                    findings=[],
                    provider_evidence=evidence,
                    timing=timing,
                )

        self.assertEqual(timing["synthesisProvider"], "deterministic")
        self.assertEqual(timing["synthesisFallbackReason"], "openai_JSONDecodeError")
        self.assertNotIn("근거를 확인했습니다", answer.summary)

    def test_explain_price_move_uses_market_move_final_answer_builder(self):
        evidence = [
            EvidenceItem(provider="market", status="available", title="Selected candle", summary="NVDA selected candle closed lower."),
            EvidenceItem(provider="news", status="available", title="NVDA supply headline", summary="Supply chain update affected chip stocks."),
        ]
        with patch.dict(os.environ, {"AGENT_FINAL_ANSWER_PROVIDER": "deterministic"}, clear=False):
            answer = FinalAnswerSynthesizer().synthesize(
                symbol="NVDA",
                intent="선택한 봉이 왜 내려갔어?",
                route=IntentRoute("operation-extractor", "explain_price_move", ["chart", "news", "macro"], 0.86, "test"),
                findings=[],
                provider_evidence=evidence,
            )

        self.assertEqual(answer.title, "NVDA 주가 변동 원인 분석")
        self.assertTrue(answer.summary.startswith("제 의견은"))
        self.assertIn("종합", answer.summary)
        self.assertNotIn("근거를 확인했습니다", answer.summary)

    def test_price_move_policy_fallback_uses_chart_direction_not_positive_news(self):
        chart_evidence = EvidenceItem(
            provider="market-data",
            status="available",
            title="Chart market snapshot",
            summary="GE chart snapshot is available.",
            raw={"visibleSummary": {"change": "-2.10%", "lastPrice": 263.14}, "dataStatus": {"candleCount": 120}},
        )
        news_evidence = EvidenceItem(
            provider="news",
            status="available",
            title="GE Aerospace five-year return highlighted",
            summary="A $100 investment five years ago would be worth $590.98 today.",
            url="https://example.com/ge-five-year-return",
            raw={
                "localizedTitle": "GE 에어로스페이스 5년 수익률 소개",
                "localizedSummary": "5년 전 100달러 투자 가치는 590.98달러로 증가했습니다.",
                "impactDirection": "positive",
                "subjectRelevance": "primary",
                "relevanceScore": 0.92,
                "publishedAt": "2026-07-07T00:00:00Z",
            },
        )
        duplicate_news = EvidenceItem(
            provider="news",
            status="available",
            title="Duplicate GE return article",
            summary="Duplicate article.",
            url="https://example.com/ge-five-year-return",
        )
        synthesis_input = SynthesisInput(
            run_id="run-ge",
            original_prompt="주가 왜 내려갔어?",
            intent="주가 왜 내려갔어?",
            snapshots=[
                DataSnapshot(
                    snapshot_id="snapshot-ge-market",
                    run_id="run-ge",
                    snapshot_type="market_snapshot",
                    status="success",
                    source="computed",
                    cache_hit=False,
                    summary="GE market snapshot.",
                    evidence=[chart_evidence],
                )
            ],
            output_policy={"analysis_query_type": "price_move_cause"},
        )

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "AGENT_FINAL_ANSWER_PROVIDER": "deterministic"}, clear=False):
            answer = FinalAnswerSynthesizer().synthesize(
                symbol="GE",
                intent="주가 왜 내려갔어?",
                route=IntentRoute("rule", "market-move", ["chart", "news", "ontology"], 0.9, "test"),
                findings=[],
                provider_evidence=[news_evidence, duplicate_news],
                synthesis_input=synthesis_input,
            )

        all_bullets = [bullet for section in answer.sections for bullet in section.bullets]
        delegated_section_title = "다음 " + "확인 " + "포인트"
        self.assertIn("하락", answer.summary)
        self.assertTrue(answer.summary.startswith("제 의견은"))
        self.assertNotIn("상승은", answer.summary)
        self.assertFalse(any(section.title == delegated_section_title for section in answer.sections))
        self.assertTrue(any(section.title == "분석한 지표" for section in answer.sections))
        self.assertFalse(any("비교하세요" in bullet or "확인하세요" in bullet for bullet in all_bullets))
        self.assertTrue(any("-2.10%" in bullet for bullet in all_bullets))
        self.assertIn("뉴스", all_bullets)
        self.assertIn("차트 가격·일자별 거래량", all_bullets)
        self.assertEqual(answer.limitations, [])
        self.assertEqual(len(answer.citations), 1)

    def test_analysis_cache_skips_missing_openai_key_fallback(self):
        provider = ClickHouseNewsProvider(
            clickhouse_provider=FakeClickHouseProvider([
                {
                    "articleId": "openai-cache-skip-1",
                    "headline": "NVDA shares rise after strong earnings",
                    "summary": "NVDA revenue beat expectations.",
                    "publishedAt": utc_now_iso(),
                    "url": "https://example.com/openai-cache-skip",
                    "symbols": ["NVDA"],
                    "source": "alpaca",
                }
            ]),
            publish_fallback=False,
        )
        orchestrator = AgentOrchestrator(analysis_cache=MemoryAgentAnalysisCache())
        orchestrator.news_agent = NewsAgent(provider)
        request = {
            "symbol": "NVDA",
            "intent": "NVDA 주가 왜 내려갔어?",
            "agentIds": ["agent-01", "agent-02"],
            "chartContext": {
                "visibleSummary": {"change": "-1.20%", "lastPrice": 100.0},
                "dataStatus": {"candleCount": 20},
            },
        }

        with patch.dict(os.environ, {"OPENAI_API_KEY": "", "AGENT_FINAL_ANSWER_PROVIDER": "openai"}, clear=False):
            first = orchestrator.analyze(request)
            second = orchestrator.analyze(request)

        self.assertFalse(first.timing["cacheHit"])
        self.assertFalse(second.timing["cacheHit"])
        self.assertEqual(first.timing["synthesisFallbackReason"], "missing_openai_api_key")
        self.assertEqual(second.timing["synthesisFallbackReason"], "missing_openai_api_key")

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
        self.assertEqual(answer.limitations, [])
        self.assertTrue(any(section.title == "분석한 지표" for section in answer.sections))


if __name__ == "__main__":
    unittest.main()
