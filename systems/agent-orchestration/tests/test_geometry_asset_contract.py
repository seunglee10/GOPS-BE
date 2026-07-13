from __future__ import annotations

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
from gops_agents.chart_assets.job_store import PostgresChartAssetJobStore  # noqa: E402
from gops_agents.chart_assets.storage import POSTGRES_TABLE, build_chart_asset_storage_from_env  # noqa: E402


class GeometryAssetContractTest(unittest.TestCase):
    def test_build_contract_supports_exactly_seven_intervals_without_llm_fields(self):
        self.assertEqual(ALLOWED_INTERVALS, ("1m", "5m", "10m", "1h", "4h", "1D", "1W"))
        envelope = ChartAssetBuildEnvelope.create(requested_by="test", symbols=["nvda"])
        payload = envelope.to_dict()
        self.assertEqual(payload["intervals"], list(ALLOWED_INTERVALS))
        self.assertNotIn("llmEnabled", payload)
        self.assertNotIn("skipFreshHours", payload)

    def test_runtime_asset_store_is_postgres_geometry_table(self):
        self.assertEqual(POSTGRES_TABLE, "chart_assets.geometry_assets")
        with self.assertRaises(RuntimeError):
            build_chart_asset_storage_from_env()

    def test_postgres_queue_claim_uses_skip_locked_and_lease(self):
        connection = _Connection()
        store = PostgresChartAssetJobStore("postgresql://test", connect=lambda *_args, **_kwargs: connection)

        store.claim_next("worker-1", lease_seconds=900)

        query = connection.executions[0][0]
        self.assertIn("FOR UPDATE OF item SKIP LOCKED", query)
        self.assertIn("lease_expires_at", query)

    def test_migration_defines_geometry_asset_and_job_tables(self):
        sql = (ROOT / "systems" / "agent-orchestration" / "jobs" / "chart-asset-migrations" / "003_geometry_assets.sql").read_text(encoding="utf-8")
        self.assertIn("chart_assets.geometry_assets", sql)
        self.assertIn("chart_assets.geometry_build_jobs", sql)
        self.assertIn("chart_assets.geometry_build_items", sql)
        self.assertIn("PRIMARY KEY (symbol, \"interval\")", sql)
        self.assertNotIn("1M", sql)


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
