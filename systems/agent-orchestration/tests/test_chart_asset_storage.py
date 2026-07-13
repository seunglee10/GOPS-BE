from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "systems" / "market-data" / "shared", ROOT / "systems" / "agent-orchestration" / "shared"):
    if str(path) not in sys.path: sys.path.insert(0, str(path))

from gops_agents.chart_assets.storage import (  # noqa: E402
    POSTGRES_TABLE, PostgresChartAssetStorage, _asset_projection, build_chart_asset_storage_from_env,
)


class ChartAssetStorageTest(unittest.TestCase):
    def test_geometry_projection_counts_single_layer_drawings(self):
        projection = _asset_projection(_asset())
        self.assertEqual(projection["drawing_count"], 2)
        self.assertEqual(projection["algorithm_version"], "ohlcv-consensus-1")
        self.assertEqual(projection["coverage_state"], "full")
        self.assertEqual(projection["input_digest"], "sha256:input")

    def test_postgres_save_targets_geometry_table_with_monotonic_upsert(self):
        connection = Connection()
        storage = PostgresChartAssetStorage("postgresql://test", connect=lambda *_args, **_kwargs: connection)

        self.assertTrue(storage.save(_asset()))

        query, parameters = connection.executions[0]
        self.assertIn(f"INSERT INTO {POSTGRES_TABLE}", query)
        self.assertIn('ON CONFLICT (symbol, "interval")', query)
        self.assertIn("EXCLUDED.payload_digest IS DISTINCT FROM", query)
        self.assertEqual(parameters[:2], ("NVDA", "1D"))

    def test_factory_is_postgres_only(self):
        with self.assertRaises(RuntimeError):
            build_chart_asset_storage_from_env()

    def test_save_rejects_drawing_from_another_interval(self):
        asset = _asset()
        asset["geometry"]["drawings"][0]["interval"] = "1W"
        storage = PostgresChartAssetStorage("postgresql://test", connect=lambda *_args, **_kwargs: Connection())

        with self.assertRaisesRegex(ValueError, "does not match"):
            storage.save(asset)

    def test_storage_accepts_eight_drawings_and_rejects_nine(self):
        storage = PostgresChartAssetStorage("postgresql://test", connect=lambda *_args, **_kwargs: Connection())
        asset = _asset()
        template = asset["geometry"]["drawings"][0]
        asset["geometry"]["drawings"] = [{**template, "id": f"drawing-{index}"} for index in range(8)]

        self.assertTrue(storage.save(asset))

        asset["geometry"]["drawings"].append({**template, "id": "drawing-8"})
        with self.assertRaisesRegex(ValueError, "drawing limit"):
            storage.save(asset)

    def test_schema_has_seven_interval_primary_key_and_eight_drawing_limit(self):
        sql = (ROOT / "systems" / "agent-orchestration" / "jobs" / "chart-asset-migrations" / "003_geometry_assets.sql").read_text(encoding="utf-8")
        self.assertIn('PRIMARY KEY (symbol, "interval")', sql)
        self.assertIn("drawing_count BETWEEN 0 AND 8", sql)
        self.assertIn("DROP CONSTRAINT IF EXISTS geometry_assets_drawing_count_check", sql)
        self.assertNotIn("'1M'", sql)


class Connection:
    def __init__(self): self.executions = []; self.rowcount = 1
    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def execute(self, query, parameters=()): self.executions.append((query, parameters)); return self
    def commit(self): return None


def _asset():
    return {
        "assetVersion": "geometry", "algorithmVersion": "ohlcv-consensus-1", "symbol": "NVDA", "interval": "1D",
        "sourceInterval": "1D", "asOf": "2026-07-10T04:00:00.000Z", "generatedAt": "2026-07-11T00:00:00.000Z",
        "status": "ready", "inputDigest": "sha256:input", "coverage": {"state": "full"},
        "geometry": {"drawings": [
            {"id": "one", "symbol": "NVDA", "interval": "1D", "sourceInterval": "1D"},
            {"id": "two", "symbol": "NVDA", "interval": "1D", "sourceInterval": "1D"},
        ]}, "indicators": {},
    }


if __name__ == "__main__": unittest.main()
