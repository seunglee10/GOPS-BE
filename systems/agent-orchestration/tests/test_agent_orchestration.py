import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))

from gops_agents.event_detector import MarketEventDetector, MarketEventThresholds
from gops_agents.orchestrator import AgentOrchestrator
from gops_agents.publisher import notification_payload


class AgentOrchestrationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
