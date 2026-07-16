from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
for path in (
    ROOT / "systems" / "market-data" / "shared",
    ROOT / "systems" / "agent-orchestration" / "shared",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from gops_agents.chart_assets.envelope import ALLOWED_INTERVALS, ChartAssetBuildEnvelope  # noqa: E402
from gops_agents.chart_assets import envelope as envelope_contract  # noqa: E402
from gops_agents.chart_assets.job_store import PostgresChartAssetJobStore  # noqa: E402
from gops_agents.chart_assets.storage import POSTGRES_TABLE, build_chart_asset_storage_from_env  # noqa: E402


class GeometryAssetContractTest(unittest.TestCase):
    def test_build_contract_supports_exactly_seven_intervals_without_llm_fields(self):
        self.assertEqual(ALLOWED_INTERVALS, ("1m", "5m", "10m", "1h", "4h", "1D", "1W"))
        self.assertEqual(envelope_contract.BUILD_INTERVALS, ("1m", "1D"))
        envelope = ChartAssetBuildEnvelope.create(requested_by="test", symbols=["nvda"])
        payload = envelope.to_dict()
        self.assertEqual(payload["intervals"], list(envelope_contract.BUILD_INTERVALS))
        self.assertNotIn("llmEnabled", payload)
        self.assertNotIn("skipFreshHours", payload)

    def test_build_envelope_rejects_read_compatible_non_build_interval(self):
        with self.assertRaisesRegex(ValueError, "only 1m and 1D"):
            ChartAssetBuildEnvelope.create(
                requested_by="test",
                symbols=["NVDA"],
                intervals=["5m"],
            )

    def test_v6_schema_keeps_legacy_fields_and_adds_optional_geometry_contract(self):
        path = ROOT / "shared" / "chart-contract" / "chart-geometry-asset.schema.json"
        schema = json.loads(path.read_text(encoding="utf-8"))
        geometry = schema["properties"]["geometry"]

        self.assertTrue({
            "drawings", "supports", "resistances", "patterns", "primaryPattern",
            "tradePlan", "primaryTriangle", "historicalTriangle",
        }.issubset(geometry["required"]))
        self.assertTrue({"trends", "primaryTrend", "drawingGroups", "analysisTrace"}.issubset(geometry["properties"]))
        self.assertIn("reference", schema["$defs"]["level"]["properties"]["selectionTier"]["enum"])
        self.assertIn("trendParallelLines", schema["$defs"]["drawing"]["properties"]["type"]["enum"])
        self.assertEqual(
            schema["$defs"]["analysisTrace"]["properties"]["version"]["enum"],
            ["geometry-analysis-trace-v1", "geometry-analysis-trace-v2"],
        )
        self.assertIn("disposition", schema["$defs"]["traceCandidate"]["properties"])
        self.assertIn("render", schema["$defs"]["traceCandidate"]["properties"])
        trade_plan = schema["$defs"]["tradePlan"]["properties"]
        self.assertEqual(trade_plan["action"]["enum"], ["watch", "buy_candidate", "sell_candidate", "no_trade"])
        self.assertEqual(trade_plan["direction"]["enum"], ["long", "exit_long", None])

    def test_manual_and_scheduled_requests_have_server_owned_priorities(self):
        manual = ChartAssetBuildEnvelope.create(requested_by="user", symbols=["NVDA"])
        scheduled = ChartAssetBuildEnvelope.create(
            requested_by="scheduler",
            symbols=["NVDA"],
            source="scheduled",
        )

        self.assertEqual((manual.source, manual.priority), ("manual", 100))
        self.assertEqual((scheduled.source, scheduled.priority), ("scheduled", 10))
        self.assertEqual(manual.to_dict()["source"], "manual")
        self.assertEqual(scheduled.to_dict()["priority"], 10)

    def test_runtime_asset_store_is_postgres_geometry_table(self):
        self.assertEqual(POSTGRES_TABLE, "chart_assets.geometry_assets")
        with self.assertRaises(RuntimeError):
            build_chart_asset_storage_from_env()

    def test_postgres_queue_claim_uses_skip_locked_and_lease(self):
        connection = _Connection()
        store = PostgresChartAssetJobStore("postgresql://test", connect=lambda *_args, **_kwargs: connection)

        store.claim_next("worker-1", lease_seconds=900)

        queries = "\n".join(query for query, _parameters in connection.executions)
        self.assertIn("FOR UPDATE OF item SKIP LOCKED", queries)
        self.assertIn("lease_expires_at", queries)
        self.assertIn("job.priority DESC", queries)
        self.assertIn("lease_expired_after_max_attempts", queries)

    def test_migration_defines_geometry_asset_and_job_tables(self):
        sql = (ROOT / "systems" / "agent-orchestration" / "jobs" / "chart-asset-migrations" / "003_geometry_assets.sql").read_text(encoding="utf-8")
        self.assertIn("chart_assets.geometry_assets", sql)
        self.assertIn("chart_assets.geometry_build_jobs", sql)
        self.assertIn("chart_assets.geometry_build_items", sql)
        self.assertIn("PRIMARY KEY (symbol, \"interval\")", sql)
        self.assertIn("drawing_count BETWEEN 0 AND 8", sql)
        self.assertNotIn("1M", sql)
        self.assertNotIn("geometry_build_jobs_active_request_idx", sql)
        self.assertNotIn("geometry_build_jobs_priority_idx", sql)

        queue_sql = (
            ROOT
            / "systems"
            / "agent-orchestration"
            / "jobs"
            / "chart-asset-migrations"
            / "004_chart_asset_queue_priority.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("source", queue_sql)
        self.assertIn("priority", queue_sql)
        self.assertIn("request_fingerprint", queue_sql)
        self.assertIn("geometry_build_jobs_active_request_idx", queue_sql)
        self.assertIn("geometry_build_jobs_priority_idx", queue_sql)
        self.assertIn("WHERE status IN ('queued', 'running')", queue_sql)


class _Connection:
    def __init__(self):
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, parameters=()):
        self.executions.append((query, parameters))
        return self

    def fetchone(self):
        return None

    def commit(self):
        return None


if __name__ == "__main__":
    unittest.main()
