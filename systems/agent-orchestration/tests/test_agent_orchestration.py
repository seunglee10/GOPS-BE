import json
import os
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(REPO_ROOT / "systems" / "market-data" / "shared"))
sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda *args, **kwargs: None))

from gops_agents.contracts import AgentFinding, DataSnapshot, EvidenceItem, FinalAnswer, IntentRoute, SynthesisInput, utc_now_iso
from gops_agents.events.detector import MarketEventDetector, MarketEventThresholds
from gops_agents.events.publisher import notification_payload
from gops_agents.orchestrator import AgentOrchestrator, canonical_analysis_intent, extract_symbol_from_intent
from gops_agents.providers import ClickHouseNewsProvider, GraphDBOntologyProvider, ProviderRequest
from gops_agents.providers.news_cache import MemoryNewsEvidenceCache, RedisNewsEvidenceCache
from gops_agents.providers.news_localization import NewsLocalizationService
from gops_agents.query_understanding import EntityAliasRecord, EntityResolution, KoreanEntityResolver
from gops_agents.query_understanding.alias_index import EntityAliasIndex
from gops_agents.retrieval import provider_bulkhead
from gops_agents.retrieval.context import GraphExpansion, RelatedSymbol
from gops_agents.retrieval.graph_expansion import (
    GraphExpansionCache,
    GraphExpansionWriter,
    build_graph_expansion_from_evidence,
    graph_expansion_cache_key,
)
from gops_agents.retrieval.snapshots import trim_cross_signals
from gops_agents.roles import AgentContext, NewsAgent, OntologyAgent, VerificationGuardrailAgent
from gops_agents.runtime.admission import AdmissionPolicy, admit_analysis_request
from gops_agents.runtime.analysis_cache import MemoryAgentAnalysisCache, RedisAgentAnalysisCache
from gops_agents.runtime.delivery_gateway import AgentDeliveryGateway
from gops_agents.runtime.envelope import build_request_envelope
from gops_agents.runtime.queues import AnalysisQueueMetrics
from gops_agents.runtime.report_store import InMemoryReportStore, RedisReportStore
from gops_agents.runtime.workers import AgentAnalysisWorker
from gops_agents.orchestration.routing import route_intent
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


class FakeLocalizedClickHouseProvider(FakeClickHouseProvider):
    def __init__(self, rows, localized_rows=None):
        super().__init__(rows)
        self.localized_rows = localized_rows or []
        self.localized_calls = 0
        self.localized_requested_symbols = []

    def localized_news_articles_for_symbols(self, symbols, limit, days, locale="ko-KR"):
        self.localized_calls += 1
        self.localized_requested_symbols.append(list(symbols))
        return self.localized_rows[:limit]


class FakeLocalizedRedisProvider:
    def __init__(self, rows, daily_rows=None):
        self.rows = rows
        self.daily_rows = daily_rows or []
        self.calls = 0
        self.requested_symbols = []
        self.daily_calls = 0

    def localized_news_articles_for_symbols(self, symbols, limit, locale="ko-KR"):
        self.calls += 1
        self.requested_symbols.append(list(symbols))
        return self.rows[:limit]

    def company_daily_news_summaries(self, symbol, limit=5, locale="ko-KR"):
        self.daily_calls += 1
        return self.daily_rows[:limit]


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
                "AGENT_ALLOW_RUNTIME_NEWS_OPENAI",
                "AGENT_GRAPHDB_TIMEOUT_MS",
                "AGENT_MAX_REALTIME_LLM_CALLS",
                "AGENT_NEWS_LOCALIZATION_PROVIDER",
                "AGENT_NEWS_ROLE_ANALYSIS_PROVIDER",
                "AGENT_GRAPH_EXPANSION_CACHE_ENABLED",
                "AGENT_CROSS_SIGNAL_ENABLED",
                "AGENT_REPORT_TTL_SECONDS",
                "AGENT_ROUTER_PROVIDER",
                "AGENT_SNAPSHOT_TIMEOUT_MS",
                "AGENT_EXPANDED_RETRIEVAL_ENABLED",
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

    def test_gops_agents_top_level_keeps_only_package_boundaries(self):
        allowed = {"__init__.py", "orchestrator.py"}
        top_level_files = {
            path.name
            for path in (ROOT / "shared" / "gops_agents").iterdir()
            if path.is_file() and path.suffix == ".py"
        }
        self.assertEqual(top_level_files, allowed)

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

    def test_memory_report_store_round_trip(self):
        store = InMemoryReportStore()
        report = AgentOrchestrator(store=store).analyze({"symbol": "NVDA", "intent": "뉴스 보여줘"})

        stored = store.get(report.analysisId)

        self.assertIsNotNone(stored)
        self.assertEqual(stored.analysisId, report.analysisId)
        self.assertEqual(stored.symbol, "NVDA")

    def test_redis_report_store_round_trip_and_latest_keys(self):
        redis = FakeRedisClient()
        store = RedisReportStore(redis, ttl_seconds=43200)
        report = AgentOrchestrator(store=store).analyze({"symbol": "NVDA", "intent": "뉴스 보여줘"})

        stored = store.get(report.analysisId)

        self.assertIsNotNone(stored)
        self.assertEqual(stored.analysisId, report.analysisId)
        self.assertEqual(stored.symbol, "NVDA")
        keys = {key for key, _ttl, _value in redis.setex_calls}
        ttls = {ttl for _key, ttl, _value in redis.setex_calls}
        self.assertIn(f"agent:report:{report.analysisId}", keys)
        self.assertIn("agent:report:latest:NVDA", keys)
        self.assertIn("agent:report:latest", keys)
        self.assertEqual(ttls, {43200})

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
            "layoutContext": layout_context(),
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

    def test_ui_router_uses_shared_llm_budget(self):
        ui_router_response = {
            "output_text": json.dumps({
                "isUiIntent": True,
                "intentKind": "layout",
                "targetPanelType": "newsFeed",
                "targetPanelId": "panel-news",
                "action": "resize",
                "sizeIntent": "large",
                "positionIntent": None,
                "confidence": 0.9,
                "reason": "User asked to enlarge the news panel.",
            })
        }

        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-key",
            "AGENT_MAX_REALTIME_LLM_CALLS": "1",
        }, clear=False):
            with patch("urllib.request.urlopen", return_value=FakeOpenAIResponse(ui_router_response)) as openai:
                report = AgentOrchestrator().analyze({
                    "symbol": "NVDA",
                    "intent": "뉴스 패널 크게 보여줘",
                    "layoutContext": layout_context(),
                })

        self.assertEqual(openai.call_count, 1)
        self.assertEqual(report.route.intentType, "ui-layout")
        self.assertEqual(report.route.source, "ui-llm")
        self.assertEqual(report.timing["llmCalls"], 1)
        self.assertEqual(report.timing["llmCallLabels"], ["ui-router"])

    def test_ui_router_budget_block_falls_back_without_openai_call(self):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-key",
            "AGENT_MAX_REALTIME_LLM_CALLS": "0",
        }, clear=False):
            with patch("urllib.request.urlopen", side_effect=AssertionError("UI router should not call OpenAI after budget block")):
                report = AgentOrchestrator().analyze({
                    "symbol": "NVDA",
                    "intent": "뉴스 패널 크게 보여줘",
                    "layoutContext": layout_context(),
                })

        self.assertEqual(report.route.intentType, "ui-layout")
        self.assertEqual(report.route.source, "ui-fallback")
        self.assertEqual(report.timing["llmCalls"], 0)
        self.assertEqual(report.timing["llmBudgetBlocked"], 1)

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

    def test_news_provider_reads_prelocalized_redis_before_clickhouse_originals(self):
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
                "url": "https://example.com/aapl-ko",
                "eventType": "corporate-action",
                "sentiment": "neutral",
                "impactDirection": "neutral",
                "keyPoints": ["애플 공급망 관련 뉴스"],
                "positivePoints": [],
                "concerns": [],
                "whyItMatters": "애플 공급망 이슈입니다.",
            }
        ])
        clickhouse = FakeClickHouseProvider([])
        provider = ClickHouseNewsProvider(
            clickhouse_provider=clickhouse,
            redis_provider=redis,
            limit=5,
            direct_fallback=False,
        )

        evidence = provider.fetch(ProviderRequest("AAPL", "애플 뉴스 보여줘"))

        self.assertEqual(redis.calls, 1)
        self.assertEqual(clickhouse.calls, 0)
        self.assertEqual(evidence[0].title, "애플 공급사, 사업 확장 추진")
        self.assertEqual(evidence[0].raw["localizedTitle"], "애플 공급사, 사업 확장 추진")
        self.assertEqual(evidence[0].raw["localizedSummary"], "애플 공급사가 사업 확장을 추진한다는 내용입니다.")
        self.assertEqual(evidence[0].raw["keyPoints"], ["애플 공급망 관련 뉴스"])
        self.assertEqual(evidence[0].raw["dataSource"], "prelocalized")

    def test_news_provider_reads_prelocalized_clickhouse_when_redis_misses(self):
        redis = FakeLocalizedRedisProvider([])
        clickhouse = FakeLocalizedClickHouseProvider(
            [],
            localized_rows=[
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
            ],
        )
        provider = ClickHouseNewsProvider(
            clickhouse_provider=clickhouse,
            redis_provider=redis,
            limit=5,
            direct_fallback=False,
        )

        evidence = provider.fetch(ProviderRequest("DDOG", "DDOG 뉴스 알려줘"))

        self.assertEqual(redis.calls, 1)
        self.assertEqual(clickhouse.localized_calls, 1)
        self.assertEqual(clickhouse.calls, 0)
        self.assertEqual(evidence[0].title, "데이터독, 관측성 기능 출시")
        self.assertEqual(evidence[0].raw["eventType"], "product-market")
        self.assertEqual(evidence[0].raw["impactDirection"], "positive")

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

    def test_orchestrator_analysis_cache_reuses_final_answer_but_rebuilds_layout(self):
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
        self.assertTrue(any(command["type"] == "layout.panel.move" for command in first.layoutProposal.commands))
        self.assertFalse(any(command["type"] == "layout.panel.move" for command in second.layoutProposal.commands))

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

    def test_hybrid_router_does_not_call_openai_only_because_api_key_exists(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            with patch("urllib.request.urlopen", side_effect=AssertionError("route LLM should be degraded-only")) as urlopen:
                route = route_intent("분석해줘", None, "hybrid")

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
        openai_role_response = {
            "output_text": json.dumps({
                "summary": "뉴스 LLM 분석입니다.",
                "rationale": "제공된 뉴스만 사용했습니다.",
                "confidence": 0.7,
                "tags": ["news", "openai"],
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
            with patch("urllib.request.urlopen", return_value=FakeOpenAIResponse(openai_role_response)) as openai:
                report = orchestrator.analyze({
                    "symbol": "NVDA",
                    "intent": "NVDA 왜 올랐어?",
                    "agentIds": ["agent-02"],
                })

        self.assertEqual(openai.call_count, 1)
        self.assertEqual(report.timing["llmCalls"], 1)
        self.assertEqual(report.timing["llmBudgetBlocked"], 1)
        self.assertIn("role:news", report.timing["llmCallLabels"])
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
            "AGENT_ALLOW_RUNTIME_NEWS_OPENAI": "true",
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
        news_command = news_panel_props_command(report)
        self.assertEqual(news_command["payload"]["props"]["symbol"], "XLV")
        self.assertEqual(news_command["payload"]["props"]["latestNews"][0]["symbol"], "XLV")

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
        news_command = news_panel_props_command(report)
        self.assertEqual(news_command["payload"]["props"]["symbol"], "ACGL")
        self.assertEqual(news_command["payload"]["props"]["latestNews"][0]["symbol"], "ACGL")

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
        news_command = news_panel_props_command(report)
        props = news_command["payload"]["props"]
        self.assertEqual(props["symbol"], "ACGL")
        self.assertEqual(props["status"], "empty")
        self.assertEqual(props["latestNews"], [])
        self.assertIn("ACGL", props["emptyMessage"])

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
        news_command = news_panel_props_command(report)
        self.assertEqual(news_command["payload"]["props"]["symbol"], "AAPL")
        self.assertEqual(news_command["payload"]["props"]["latestNews"][0]["symbol"], "AAPL")

    def test_korean_entity_resolver_handles_compact_typos_and_choseong(self):
        cases = {
            "애플뉴스알려줘": "AAPL",
            "apple뉴스알려줘": "AAPL",
            "얘플 뉴스": "AAPL",
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

        with patch("gops_agents.orchestration.request.resolve_entity", return_value=resolution):
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

        self.assertEqual(report.symbol, "AAPL")
        self.assertEqual(clickhouse.requested_symbols, ["AAPL"])
        self.assertEqual(report.agentTrace["entityResolution"]["status"], "confirmed")
        self.assertEqual(report.agentTrace["entityResolution"]["symbol"], "AAPL")
        self.assertEqual(report.resolvedEntities[0].aliases, ["애플"])

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
                }
            ],
        )
        provider = ClickHouseNewsProvider(clickhouse_provider=FakeClickHouseProvider([]), redis_provider=redis, publish_fallback=False)
        orchestrator = AgentOrchestrator()
        orchestrator.news_agent = NewsAgent(provider)

        report = orchestrator.analyze({
            "symbol": "NVDA",
            "intent": "애플 뉴스 보여줘",
            "agentIds": ["agent-02"],
        })

        self.assertEqual(report.symbol, "AAPL")
        self.assertEqual(report.dailySummaries[0]["summary"], "애플 관련 일일 뉴스 요약입니다.")
        self.assertIn("일일 뉴스 요약", report.finalAnswer.summary)
        news_command = news_panel_props_command(report)
        props = news_command["payload"]["props"]
        self.assertEqual(props["dailySummaries"][0]["date"], "2026-07-01")
        self.assertEqual(props["dailySummaries"][0]["keyPoints"], ["서비스 성장", "공급망 점검"])

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
        news_command = news_panel_props_command(report)
        props = news_command["payload"]["props"]
        self.assertEqual(props["dailySummaries"][0]["keyPoints"], ["role fallback"])

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
        news_command = news_panel_props_command(report)
        props = news_command["payload"]["props"]
        self.assertEqual(props["symbol"], "반도체")
        self.assertTrue({"AMD", "MU"}.issubset({item["symbol"] for item in props["latestNews"]}))

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

    def test_openai_synthesizer_uses_synthesis_input_payload_when_available(self):
        response = {
            "output_text": json.dumps({
                "title": "NVDA 통합 분석",
                "summary": "snapshot 근거를 바탕으로 요약했습니다.",
                "sections": [{"title": "근거", "bullets": ["뉴스 snapshot이 있습니다."]}],
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
        self.assertIn("synthesisInput", user_payload)
        self.assertNotIn("providerEvidence", user_payload)
        self.assertNotIn("findings", user_payload)

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
