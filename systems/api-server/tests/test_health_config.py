import os
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend"
for path in (str(MARKET_SHARED), str(BACKEND), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from app.routes import health  # noqa: E402


class HealthConfigTest(unittest.TestCase):
    def test_single_sip_feed_profile_is_default_runtime_contract(self) -> None:
        with mock.patch.dict(os.environ, {
            "ALPACA_FEED_PROFILE": "sip",
            "ALPACA_FEED_PROFILES": "sip",
        }, clear=True):
            self.assertEqual(health.configured_feed_profiles(), ["sip"])
            warnings = health.runtime_config_warnings()

        self.assertEqual([warning for warning in warnings if warning.startswith("alpaca_feed")], [])

    def test_warns_when_active_feed_is_not_reported_in_profile_list(self) -> None:
        with mock.patch.dict(os.environ, {
            "ALPACA_FEED_PROFILE": "boats",
            "ALPACA_FEED_PROFILES": "sip",
        }, clear=True):
            warnings = health.runtime_config_warnings()

        self.assertIn("alpaca_feed_profile_not_listed", warnings)

    def test_pipeline_required_components_default_to_configured_feeds_and_processor(self) -> None:
        with mock.patch.dict(os.environ, {
            "ALPACA_FEED_PROFILES": "sip,iex",
        }, clear=True):
            components = health.pipeline_required_component_names()

        self.assertEqual(components, ["market-ingestor-sip", "market-ingestor-iex", "market-processor"])

    def test_pipeline_required_components_can_be_overridden_for_local_core_runtime(self) -> None:
        with mock.patch.dict(os.environ, {
            "ALPACA_FEED_PROFILES": "sip",
            "PIPELINE_REQUIRED_COMPONENTS": "market-processor",
        }, clear=True):
            components = health.pipeline_required_component_names()

        self.assertEqual(components, ["market-processor"])

    def test_pipeline_component_summary_reports_missing_and_unhealthy(self) -> None:
        summary = health.pipeline_component_summary({
            "market-ingestor-sip": None,
            "market-processor": {"status": "error"},
            "clickhouse-loader": {"status": "ok"},
        })

        self.assertFalse(summary["healthy"])
        self.assertEqual(summary["missing"], ["market-ingestor-sip"])
        self.assertEqual(summary["unhealthy"], ["market-processor"])

    def test_runtime_warnings_include_pipeline_component_state(self) -> None:
        warnings = health.runtime_config_warnings({
            "available": True,
            "missing": ["market-ingestor-sip"],
            "unhealthy": ["market-processor"],
        })

        self.assertIn("pipeline_component_missing", warnings)
        self.assertIn("pipeline_component_unhealthy", warnings)

    def test_component_health_redaction_keeps_processor_diagnostics(self) -> None:
        redacted = health.redact_component_health({
            "component": "market-processor",
            "status": "ok",
            "updatedAt": "2026-06-30T22:58:54.170Z",
            "lastResult": "trades",
            "lastEventAt": "2026-06-30T22:55:00.000Z",
            "heartbeatResult": "idle",
            "lastError": "x" * 400,
            "secret": "do-not-include",
        })

        self.assertEqual(redacted["lastEventAt"], "2026-06-30T22:55:00.000Z")
        self.assertEqual(redacted["heartbeatResult"], "idle")
        self.assertEqual(len(redacted["lastError"]), 300)
        self.assertNotIn("secret", redacted)


if __name__ == "__main__":
    unittest.main()
