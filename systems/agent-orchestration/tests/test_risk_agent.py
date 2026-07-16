import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENT_SHARED = ROOT / "systems" / "agent-orchestration" / "shared"
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
for path in (str(AGENT_SHARED), str(MARKET_SHARED)):
    if path not in sys.path:
        sys.path.insert(0, path)

from gops_agents.contracts import EvidenceItem, IntentRoute, RuntimePolicy  # noqa: E402
from gops_agents.events.publisher import RedisNotificationPublisher, risk_log_key  # noqa: E402
from gops_agents.orchestration.routing import route_intent  # noqa: E402
from gops_agents.providers import ProviderRequest, RedisRiskEventsProvider  # noqa: E402
from gops_agents.retrieval.snapshots import (  # noqa: E402
    RiskEventsSnapshotProvider,
    SNAPSHOT_BUNDLE_BY_INTENT,
    role_findings_from_snapshots,
    route_plan_intent,
)
from gops_agents.roles import AgentContext, RiskAgent  # noqa: E402


class FakeRedis:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.lists = {}
        self.published = []

    def get(self, key):
        return self.values.get(key)

    def publish(self, channel, message):
        self.published.append((channel, message))

    def setex(self, key, ttl, value):
        self.values[key] = value

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    def ltrim(self, key, start, end):
        self.lists[key] = self.lists.get(key, [])[start:end + 1]

    def expire(self, key, ttl):
        return True

    def lrange(self, key, start, end):
        return self.lists.get(key, [])[start:end + 1]


def cached_alert(symbol="NVDA", event_type="risk_concentration_drift", severity="critical", summary="NVDA 비중 쏠림 경고"):
    return json.dumps({
        "type": "AGENT_ALERT",
        "decision": {
            "eventId": f"evt-{symbol}",
            "symbol": symbol,
            "eventType": event_type,
            "severity": severity,
            "summary": summary,
            "observedAt": "2026-07-12T14:00:00Z",
            "metrics": {"uiProposals": []},
        },
    })


class RiskRoutingTest(unittest.TestCase):
    def test_risk_keywords_route_to_risk_role(self):
        for query in ("NVDA 리스크 점검해줘", "내 포트폴리오 위험한 거 있어?", "왜 막았어?", "종목 비중 한도 알려줘"):
            route = route_intent(query)
            self.assertIn("risk", route.selectedRoles, query)
            self.assertIn("risk-check", route.intentType, query)

    def test_existing_routes_unchanged(self):
        self.assertEqual(route_intent("NVDA 차트 보여줘").selectedRoles, ["chart"])
        self.assertEqual(route_intent("NVDA 뉴스 알려줘").selectedRoles, ["news"])

    def test_risk_route_plan_selects_risk_events_bundle(self):
        route = IntentRoute(source="rule", intentType="risk-check", selectedRoles=["risk"], confidence=0.9, reason="")
        intent = route_plan_intent(route)

        self.assertEqual(intent, "risk_check")
        self.assertIn("risk_events_snapshot", SNAPSHOT_BUNDLE_BY_INTENT[intent])


class RedisRiskEventsProviderTest(unittest.TestCase):
    def test_reads_latest_symbol_and_portfolio_events(self):
        redis = FakeRedis({
            "agent.alerts:latest:NVDA": cached_alert(),
            "agent.alerts:latest:PORTFOLIO": cached_alert(symbol="PORTFOLIO", event_type="risk_daily_loss_limit", summary="일일 한도 도달"),
        })
        provider = RedisRiskEventsProvider(redis)

        evidence = provider.fetch(ProviderRequest("NVDA", "리스크 점검"))

        self.assertEqual(len(evidence), 2)
        self.assertEqual(evidence[0].title, "risk_concentration_drift")
        self.assertEqual(evidence[1].raw["symbol"], "PORTFOLIO")

    def test_ignores_non_risk_events_and_reports_no_data(self):
        redis = FakeRedis({
            "agent.alerts:latest:NVDA": json.dumps({"decision": {"eventType": "price_surge", "summary": "급등"}}),
        })
        provider = RedisRiskEventsProvider(redis)

        evidence = provider.fetch(ProviderRequest("NVDA", "리스크 점검"))

        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].status, "no-data")


class RiskAgentTest(unittest.TestCase):
    def test_narrates_latest_risk_event_with_severity_label(self):
        redis = FakeRedis({"agent.alerts:latest:NVDA": cached_alert()})
        agent = RiskAgent(RedisRiskEventsProvider(redis))
        context = AgentContext(symbol="NVDA", intent="NVDA 리스크 점검", selectedRoles=["risk"], intentType="risk-check")

        finding = agent.analyze(context)

        self.assertEqual(finding.role, "risk-analysis")
        self.assertIn("[긴급]", finding.summary)
        self.assertIn("비중 쏠림", finding.summary)
        self.assertIn("read-only", finding.tags)
        # 조회 전용 가드 문구
        self.assertIn("우회할 수 없고", finding.rationale)

    def test_reports_clean_state_when_no_events(self):
        agent = RiskAgent(RedisRiskEventsProvider(FakeRedis()))
        context = AgentContext(symbol="NVDA", intent="리스크 점검", selectedRoles=["risk"], intentType="risk-check")

        finding = agent.analyze(context)

        self.assertIn("리스크 이벤트가 없습니다", finding.summary)
        # 매수 권유 표현 금지
        for banned in ("매수하세요", "사세요", "팔아라"):
            self.assertNotIn(banned, finding.summary + finding.rationale)


class RiskSnapshotPathTest(unittest.TestCase):
    def test_role_findings_built_from_risk_events_snapshot(self):
        redis = FakeRedis({"agent.alerts:latest:NVDA": cached_alert()})
        snapshot_provider = RiskEventsSnapshotProvider(RedisRiskEventsProvider(redis))
        context = AgentContext(symbol="NVDA", intent="리스크 점검", selectedRoles=["risk"], intentType="risk-check")
        snapshot = snapshot_provider.fetch(context, "run-1", 5)

        findings = role_findings_from_snapshots(["risk"], [snapshot], context)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].role, "risk-analysis")
        self.assertEqual(findings[0].agentId, "risk-agent")
        self.assertIn("리스크 이벤트", findings[0].summary)


class RiskLogTest(unittest.TestCase):
    def test_publisher_appends_risk_events_to_daily_log(self):
        redis = FakeRedis()
        publisher = RedisNotificationPublisher(redis)
        decision = {
            "eventId": "evt-1",
            "symbol": "NVDA",
            "eventType": "risk_concentration_drift",
            "severity": "alert",
            "summary": "비중 쏠림 경고",
            "observedAt": "2026-07-12T14:00:00Z",
            "level": "alert",
            "showToast": True,
        }

        publisher.publish(decision)

        log = redis.lists[risk_log_key("2026-07-12")]
        self.assertEqual(len(log), 1)
        self.assertEqual(json.loads(log[0])["eventType"], "risk_concentration_drift")

    def test_non_risk_decisions_are_not_logged(self):
        redis = FakeRedis()
        publisher = RedisNotificationPublisher(redis)

        publisher.publish({"symbol": "NVDA", "eventType": "price_surge", "level": "watch"})

        self.assertEqual(redis.lists, {})

    def test_market_events_are_internal_only_and_duplicate_ids_publish_once(self):
        redis = FakeRedis()
        publisher = RedisNotificationPublisher(redis)
        decision = {
            "eventId": "market-event-1",
            "symbol": "NVDA",
            "eventType": "volatility_expansion",
            "severity": "critical",
            "showToast": True,
        }

        first = publisher.publish(decision, source_topic="agents.market-events.v1")
        duplicate = publisher.publish(decision, source_topic="agents.market-events.v1")

        self.assertFalse(first["showToast"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(len(redis.published), 2)


if __name__ == "__main__":
    unittest.main()
