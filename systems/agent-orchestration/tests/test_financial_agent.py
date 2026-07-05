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

from gops_agents.contracts import EvidenceItem, IntentRoute, utc_now_iso
from gops_agents.intent_understanding import build_query_understanding
from gops_agents.orchestration.reporting import build_agent_trace
from gops_agents.orchestration.routing import route_intent
from gops_agents.providers import ClickHouseFinancialProvider, ProviderRequest, financial_peer_latest_key, financial_summary_key
from gops_agents.query_understanding import EntityResolution
from gops_agents.retrieval.snapshots import (
    SNAPSHOT_BUNDLE_BY_INTENT,
    SnapshotExecutor,
    build_route_plan,
    role_findings_from_snapshots,
    route_plan_intent,
    runtime_policy_from_env,
)
from gops_agents.roles import AgentContext, FinancialAgent
from gops_agents.synthesis import FinalAnswerSynthesizer


class FakeRedisClient:
    def __init__(self, items):
        self.items = dict(items)
        self.get_calls = []

    def get(self, key):
        self.get_calls.append(key)
        return self.items.get(key)


class BrokenRedisClient:
    def get(self, key):
        raise RuntimeError("redis unavailable")


class CountingClickHouseClient:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = 0

    def query_json_each_row(self, query, params):
        self.calls += 1
        return list(self.rows)


class FakeFinancialProvider:
    def __init__(self):
        self.summary_calls = 0
        self.peer_calls = 0

    def fetch(self, request):
        self.summary_calls += 1
        return [
            EvidenceItem(
                provider="financial",
                status="available",
                title=f"{request.symbol} financial summary",
                summary=f"{request.symbol} SEC 재무 요약을 확인했습니다.",
                observedAt=utc_now_iso(),
                raw={
                    "symbol": request.symbol,
                    "latest_period": "2025 FY",
                    "metrics": [
                        {"metric": "revenue", "value": 100, "quality": "available", "selectedConcept": "Revenues"},
                        {"metric": "net_margin", "value": 0.2, "quality": "available"},
                    ],
                    "quality": "available",
                    "cache_hit": True,
                    "dataSource": "redis",
                },
            )
        ]

    def fetch_peer(self, request):
        self.peer_calls += 1
        return [
            EvidenceItem(
                provider="financial",
                status="available",
                title=f"{request.symbol} financial peer summary",
                summary=f"{request.symbol} SEC frames peer 비교를 확인했습니다.",
                observedAt=utc_now_iso(),
                raw={
                    "symbol": request.symbol,
                    "frame_period": "CY2025Q4",
                    "peers": [
                        {"symbol": request.symbol, "concept": "Revenues", "value": 100, "quality": "frame_as_reported"},
                        {"symbol": "AMD", "concept": "Revenues", "value": 70, "quality": "frame_as_reported"},
                    ],
                    "quality": "frame_as_reported",
                    "cache_hit": True,
                    "dataSource": "redis",
                },
            )
        ]


class FinancialAgentIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._env_backup = {
            name: os.environ.pop(name, None)
            for name in [
                "OPENAI_API_KEY",
                "AGENT_FINAL_ANSWER_PROVIDER",
                "AGENT_FINANCIAL_FINAL_ANSWER_PROVIDER",
                "AGENT_SNAPSHOT_TIMEOUT_MS",
            ]
        }

    def tearDown(self):
        for name, value in self._env_backup.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_financial_route_uses_word_boundaries_for_short_metrics(self):
        eps_route = route_intent("NVDA EPS 알려줘")
        deepseek_route = route_intent("deepseek 뉴스 보여줘")

        self.assertIn("financial", eps_route.selectedRoles)
        self.assertNotIn("financial", deepseek_route.selectedRoles)
        self.assertEqual(deepseek_route.selectedRoles, ["news"])

    def test_financial_route_plan_selects_expected_snapshot_bundles(self):
        context = AgentContext(symbol="NVDA", intent="NVDA AMD 재무 비교")
        policy = runtime_policy_from_env()

        financial_route = route_intent("NVDA 재무제표 보여줘")
        comparison_route = route_intent("NVDA AMD 재무 비교해줘")
        news_route = route_intent("NVDA 실적 뉴스 보여줘")

        self.assertEqual(route_plan_intent(financial_route), "financial_analysis")
        self.assertEqual(build_route_plan("run-fin", financial_route, context, policy).snapshot_bundle, SNAPSHOT_BUNDLE_BY_INTENT["financial_analysis"])
        self.assertEqual(route_plan_intent(comparison_route), "financial_comparison")
        self.assertIn("financial_peer_snapshot", build_route_plan("run-peer", comparison_route, context, policy).snapshot_bundle)
        self.assertEqual(route_plan_intent(news_route), "financial_news_analysis")

    def test_query_understanding_preserves_financial_comparison_intent(self):
        with patch(
            "gops_agents.intent_understanding.fanout.resolve_entity",
            return_value=EntityResolution(
                status="confirmed",
                symbol="NVDA",
                canonical_name="NVIDIA",
                confidence=0.95,
                matched_text="NVDA",
                matched_alias="NVDA",
            ),
        ):
            understanding, _ = build_query_understanding("NVDA AMD 재무 비교해줘", request_symbol="NVDA")

        self.assertEqual(understanding.routeMode, "analysis")
        self.assertEqual(understanding.intentType, "financial-comparison")
        self.assertEqual(understanding.selectedRoles, ["financial"])

    def test_redis_summary_hit_does_not_query_clickhouse(self):
        payload = {
            "symbol": "NVDA",
            "summary": "NVDA cached SEC 재무 요약",
            "latest_period": "2025 FY",
            "metrics": [{"metric": "revenue", "value": 100, "quality": "available"}],
        }
        redis_client = FakeRedisClient({financial_summary_key("NVDA"): json.dumps(payload)})
        clickhouse = CountingClickHouseClient()
        provider = ClickHouseFinancialProvider(clickhouse_provider=clickhouse, redis_client=redis_client)

        evidence = provider.fetch(ProviderRequest("NVDA", "재무제표"))

        self.assertEqual(clickhouse.calls, 0)
        self.assertEqual(evidence[0].status, "available")
        self.assertTrue(evidence[0].raw["cache_hit"])
        self.assertEqual(evidence[0].raw["dataSource"], "redis")

    def test_redis_failure_falls_back_to_clickhouse_summary(self):
        clickhouse = CountingClickHouseClient([
            {
                "symbol": "NVDA",
                "metric": "revenue",
                "value": 100,
                "fiscalYear": 2025,
                "fiscalPeriod": "FY",
                "periodEnd": "2025-12-31",
                "accession": "accn",
                "filedAt": "2026-02-01",
                "computedAt": "2026-02-02T00:00:00Z",
                "quality": "available",
                "raw": json.dumps({"selected_concept": "Revenues", "quality": "available"}),
            }
        ])
        provider = ClickHouseFinancialProvider(clickhouse_provider=clickhouse, redis_client=BrokenRedisClient())

        evidence = provider.fetch(ProviderRequest("NVDA", "재무제표"))

        self.assertEqual(clickhouse.calls, 1)
        self.assertEqual(evidence[0].status, "available")
        self.assertFalse(evidence[0].raw["cache_hit"])
        self.assertEqual(evidence[0].raw["dataSource"], "clickhouse")

    def test_redis_peer_latest_hit_does_not_query_frames(self):
        payload = {
            "symbol": "NVDA",
            "summary": "NVDA cached peer 비교",
            "frame_period": "CY2025Q4",
            "peers": [{"symbol": "AMD", "concept": "Revenues", "value": 70}],
        }
        redis_client = FakeRedisClient({financial_peer_latest_key("NVDA"): json.dumps(payload)})
        clickhouse = CountingClickHouseClient()
        provider = ClickHouseFinancialProvider(clickhouse_provider=clickhouse, redis_client=redis_client)

        evidence = provider.fetch_peer(ProviderRequest("NVDA", "재무 비교"))

        self.assertEqual(clickhouse.calls, 0)
        self.assertEqual(evidence[0].status, "available")
        self.assertTrue(evidence[0].raw["cache_hit"])
        self.assertEqual(evidence[0].raw["frame_period"], "CY2025Q4")

    def test_financial_comparison_role_finding_merges_summary_and_peer_snapshots(self):
        context = AgentContext(
            symbol="NVDA",
            intent="NVDA AMD 재무 비교",
            selectedRoles=["financial"],
            intentType="financial-comparison",
            relationshipSymbols=["AMD"],
        )
        route = IntentRoute(source="rule", intentType="financial-comparison", selectedRoles=["financial"], confidence=0.9, reason="test")
        policy = runtime_policy_from_env()
        route_plan = build_route_plan("run-financial", route, context, policy)
        fake_provider = FakeFinancialProvider()
        executor = SnapshotExecutor(news_agent=object(), ontology_agent=object())
        executor.financial.provider = fake_provider
        executor.financial_peer.provider = fake_provider

        snapshots = executor.fetch(context=context, run_id="run-financial", route_plan=route_plan, policy=policy)
        findings = role_findings_from_snapshots(["financial"], snapshots, context)
        trace = build_agent_trace(snapshots)

        self.assertEqual(fake_provider.summary_calls, 1)
        self.assertEqual(fake_provider.peer_calls, 1)
        self.assertIn("financial_snapshot", {snapshot.snapshot_type for snapshot in snapshots})
        self.assertIn("financial_peer_snapshot", {snapshot.snapshot_type for snapshot in snapshots})
        self.assertEqual(findings[0].role, "financial-analysis")
        self.assertIn("financial summary", {item.title.split("NVDA ", 1)[-1] for item in findings[0].evidence})
        self.assertIn("financial peer summary", {item.title.split("NVDA ", 1)[-1] for item in findings[0].evidence})
        self.assertIn("financial_snapshot", {item["snapshot_type"] for item in trace["visibleSnapshots"]})
        self.assertIn("financial_peer_snapshot", {item["snapshot_type"] for item in trace["visibleSnapshots"]})

    def test_financial_agent_fetches_peer_only_for_comparison_context(self):
        provider = FakeFinancialProvider()
        agent = FinancialAgent(provider)
        context = AgentContext(symbol="NVDA", intent="NVDA 재무 분석", selectedRoles=["financial"], intentType="financial-analysis")
        comparison_context = AgentContext(symbol="NVDA", intent="NVDA AMD 재무 비교", selectedRoles=["financial"], intentType="financial-comparison")

        agent.analyze(context)
        agent.analyze(comparison_context)

        self.assertEqual(provider.summary_calls, 2)
        self.assertEqual(provider.peer_calls, 1)

    def test_financial_final_answer_is_deterministic(self):
        provider = FakeFinancialProvider()
        finding = FinancialAgent(provider).analyze(
            AgentContext(symbol="NVDA", intent="NVDA AMD 재무 비교", selectedRoles=["financial"], intentType="financial-comparison")
        )

        answer = FinalAnswerSynthesizer().synthesize(
            symbol="NVDA",
            intent="NVDA AMD 재무 비교",
            route=IntentRoute(source="rule", intentType="financial-comparison", selectedRoles=["financial"], confidence=0.9, reason="test"),
            findings=[finding],
            provider_evidence=finding.evidence,
        )

        self.assertEqual(answer.title, "NVDA SEC 재무 분석")
        self.assertTrue(any(section.title == "공시 기반 재무 요약" for section in answer.sections))
        self.assertTrue(any(section.title == "Peer 비교" for section in answer.sections))
        self.assertTrue(any("SEC" in item for item in answer.limitations))


if __name__ == "__main__":
    unittest.main()
