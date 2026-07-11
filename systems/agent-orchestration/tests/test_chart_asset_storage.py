from __future__ import annotations

import sys
import unittest
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "systems" / "agent-orchestration" / "shared", ROOT / "systems" / "market-data" / "shared"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from gops_agents.chart_assets.storage import (  # noqa: E402
    ChartAssetStorage,
    ClickHouseChartAssetStorage,
    DualChartAssetStorage,
    MaintenanceChartAssetStorage,
    PostgresChartAssetStorage,
    _asset_projection,
    _canonical_payload,
    _payload_digest,
    build_chart_asset_storage_from_env,
)

MIGRATION_SPEC = importlib.util.spec_from_file_location(
    "chart_asset_migration_job",
    ROOT / "systems" / "agent-orchestration" / "jobs" / "chart-asset-migrations" / "main.py",
)
assert MIGRATION_SPEC and MIGRATION_SPEC.loader
MIGRATION = importlib.util.module_from_spec(MIGRATION_SPEC)
MIGRATION_SPEC.loader.exec_module(MIGRATION)


class FakeClient:
    def __init__(self, *, query_rows=None):
        self.queries = []
        self.executions = []
        self.inserts = []
        self.query_rows = query_rows

    def query_json_each_row(self, query, parameters=None):
        self.queries.append((query, parameters))
        return list(self.query_rows) if self.query_rows is not None else [{"assetCount": 2}]

    def execute(self, query, parameters=None):
        self.executions.append((query, parameters))

    def insert_json_each_row(self, table, rows):
        self.inserts.append((table, rows))


class ChartAssetStorageTest(unittest.TestCase):
    def test_delete_removes_all_history_for_selected_pairs_synchronously(self):
        client = FakeClient()
        deleted = ChartAssetStorage(client=client).delete(["nvda", "NVDA"], ["1D", "1W", "1D"])
        self.assertEqual(deleted, 2)
        expected = {"symbols": ["NVDA"], "intervals": ["1D", "1W"]}
        self.assertEqual(client.queries[0][1], expected)
        self.assertEqual(client.executions[0][1], expected)
        self.assertIn("DELETE WHERE", client.executions[0][0])
        self.assertIn("mutations_sync = 1", client.executions[0][0])

    def test_delete_with_empty_selection_is_noop(self):
        client = FakeClient()
        self.assertEqual(ChartAssetStorage(client=client).delete([], ["1D"]), 0)
        self.assertEqual(client.queries, [])
        self.assertEqual(client.executions, [])

    def test_migration_source_rejects_invalid_latest_payload_instead_of_hiding_pair(self):
        client = FakeClient(query_rows=[{"symbol": "NVDA", "interval": "1D", "payload": "not-json"}])
        with self.assertRaisesRegex(ValueError, "NVDA:1D"):
            ClickHouseChartAssetStorage(client=client).latest_assets()

    def test_migration_source_rejects_key_payload_pair_mismatch(self):
        client = FakeClient(query_rows=[{
            "symbol": "AAPL", "interval": "1D", "payload": _canonical_payload(_asset()),
        }])
        with self.assertRaisesRegex(ValueError, "key/payload mismatch"):
            ClickHouseChartAssetStorage(client=client).latest_assets()

    def test_projection_counts_compact_payload_and_drawings(self):
        asset = _asset()
        projection = _asset_projection(asset)
        self.assertEqual(projection["symbol"], "NVDA")
        self.assertEqual(projection["drawing_count"], 2)
        self.assertGreater(projection["payload_bytes"], 0)
        self.assertTrue(projection["payload_digest"].startswith("sha256:"))
        self.assertEqual(projection["asset_content_digest"], "sha256:content")

    def test_clickhouse_save_rejects_an_older_generated_asset_before_insert(self):
        newer = {**_asset(), "generatedAt": "2026-07-12T00:00:00.000Z"}
        client = FakeClient(query_rows=[{"payload": _canonical_payload(newer)}])

        saved = ClickHouseChartAssetStorage(client=client).save(_asset())

        self.assertFalse(saved)
        self.assertEqual(client.inserts, [])

    def test_clickhouse_save_inserts_a_newer_generated_asset(self):
        older = {**_asset(), "generatedAt": "2026-07-10T00:00:00.000Z"}
        client = FakeClient(query_rows=[{"payload": _canonical_payload(older)}])

        saved = ClickHouseChartAssetStorage(client=client).save(_asset())

        self.assertTrue(saved)
        self.assertEqual(len(client.inserts), 1)

    def test_dual_write_keeps_primary_available_and_records_shadow_warning(self):
        primary = MemoryStore()
        shadow = MemoryStore(fail_save=True)
        storage = DualChartAssetStorage(primary, shadow, mode="dual_clickhouse_read")
        with self.assertLogs("gops_agents.chart_assets.storage", level="WARNING"):
            storage.save(_asset())
        self.assertEqual(primary.saved, ["NVDA:1D"])
        self.assertEqual(len(storage.pop_warnings()), 1)
        self.assertEqual(storage.pop_warnings(), [])

    def test_shadow_warnings_stay_ephemeral_and_bound_to_the_build_thread(self):
        storage = DualChartAssetStorage(MemoryStore(), MemoryStore(fail_save=True), mode="dual_clickhouse_read")
        barrier = Barrier(2)

        def save_and_pop():
            storage.save(_asset())
            barrier.wait()
            return storage.pop_warnings()

        with patch("gops_agents.chart_assets.storage.LOGGER.warning"), ThreadPoolExecutor(max_workers=2) as executor:
            warnings = list(executor.map(lambda _index: save_and_pop(), range(2)))

        self.assertEqual([len(items) for items in warnings], [1, 1])

    def test_dual_write_surfaces_a_shadow_monotonic_noop_that_diverges(self):
        primary = MemoryStore()
        shadow = MonotonicNoopStore({**_asset(), "generatedAt": "2026-07-12T00:00:00.000Z"})
        storage = DualChartAssetStorage(primary, shadow, mode="dual_clickhouse_read")

        with self.assertLogs("gops_agents.chart_assets.storage", level="WARNING"):
            storage.save(_asset())

        self.assertEqual(len(storage.pop_warnings()), 1)

    def test_dual_delete_attempts_both_and_fails_if_either_store_fails(self):
        primary = MemoryStore()
        shadow = MemoryStore(fail_delete=True)
        storage = DualChartAssetStorage(primary, shadow, mode="dual_clickhouse_read")
        with self.assertRaises(RuntimeError):
            storage.delete(["NVDA"], ["1D"])
        self.assertEqual(primary.deleted, [(["NVDA"], ["1D"])])
        self.assertEqual(shadow.deleted, [(["NVDA"], ["1D"])])

    def test_postgres_save_uses_one_row_upsert(self):
        connection = FakePostgresConnection()
        storage = PostgresChartAssetStorage("postgresql://test", connect=lambda *_args, **_kwargs: connection)
        storage.save(_asset())
        self.assertEqual(len(connection.executions), 1)
        query, parameters = connection.executions[0]
        self.assertIn("ON CONFLICT (symbol, \"interval\")", query)
        self.assertIn("EXCLUDED.payload_digest IS DISTINCT FROM", query)
        self.assertEqual(parameters[0:2], ("NVDA", "1D"))
        self.assertTrue(connection.committed)

    def test_postgres_save_reports_a_monotonic_noop(self):
        connection = FakePostgresConnection(rowcount=0)
        storage = PostgresChartAssetStorage("postgresql://test", connect=lambda *_args, **_kwargs: connection)

        self.assertFalse(storage.save(_asset()))

    def test_postgres_parity_recomputes_digest_from_actual_jsonb(self):
        asset = _asset()
        connection = FakePostgresConnection(rows=[{
            "symbol": "NVDA", "interval": "1D", "payload": asset,
            "payload_digest": "sha256:stale-projection-must-not-be-trusted",
        }])
        storage = PostgresChartAssetStorage("postgresql://test", connect=lambda *_args, **_kwargs: connection)

        digests = storage.payload_digests()

        self.assertEqual(digests, {("NVDA", "1D"): _payload_digest(_canonical_payload(asset))})

    def test_maintenance_storage_keeps_reads_live_and_blocks_all_writes(self):
        delegate = MemoryStore()
        storage = MaintenanceChartAssetStorage(delegate)

        self.assertEqual(storage.get_symbol_assets("NVDA"), {"1D": None, "1W": None, "1M": None})
        with self.assertRaises(RuntimeError):
            storage.save(_asset())
        with self.assertRaises(RuntimeError):
            storage.delete(["NVDA"], ["1D"])
        self.assertEqual(delegate.saved, [])
        self.assertEqual(delegate.deleted, [])

    def test_invalid_storage_mode_fails_closed(self):
        with patch.dict("os.environ", {"CHART_ASSET_STORAGE_MODE": "unknown"}):
            with self.assertRaises(ValueError):
                build_chart_asset_storage_from_env()

    def test_factory_enforces_read_only_maintenance_for_workers_and_api(self):
        with patch.dict("os.environ", {
            "CHART_ASSET_STORAGE_MODE": "clickhouse",
            "CHART_ASSET_STORAGE_MAINTENANCE": "true",
        }):
            storage = build_chart_asset_storage_from_env()
        self.assertIsInstance(storage, MaintenanceChartAssetStorage)

    def test_migration_parity_requires_exact_pairs_and_payload_hashes(self):
        source = {("AAPL", "1D"): "sha256:a", ("NVDA", "1W"): "sha256:b"}
        exact = MIGRATION.verify_parity(source, dict(source))
        mismatch = MIGRATION.verify_parity(source, {("AAPL", "1D"): "sha256:x", ("MU", "1D"): "sha256:y"})
        self.assertTrue(exact["parity"])
        self.assertFalse(mismatch["parity"])
        self.assertEqual(mismatch["missingRows"], 1)
        self.assertEqual(mismatch["extraRows"], 1)
        self.assertEqual(mismatch["digestMismatches"], 1)

    def test_sync_and_verify_fail_closed_without_write_maintenance(self):
        for action in ("sync", "verify"):
            with self.subTest(action=action), patch.dict("os.environ", {
                "CHART_ASSET_MIGRATION_ACTION": action,
                "CHART_ASSET_STORAGE_MAINTENANCE": "false",
            }), patch.object(MIGRATION, "apply_schema") as apply_schema:
                self.assertEqual(MIGRATION.main(), 3)
                apply_schema.assert_not_called()

    def test_postgres_schema_is_latest_row_only_and_chart_owned(self):
        sql = (
            ROOT / "systems" / "agent-orchestration" / "jobs" / "chart-asset-migrations"
            / "001_create_chart_assets.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("CREATE SCHEMA IF NOT EXISTS chart_assets", sql)
        self.assertIn('PRIMARY KEY (symbol, "interval")', sql)
        self.assertIn("payload JSONB NOT NULL", sql)
        self.assertNotIn("CREATE INDEX", sql)
        self.assertNotIn("TTL", sql.upper())


class MemoryStore:
    def __init__(self, *, fail_save=False, fail_delete=False):
        self.fail_save = fail_save
        self.fail_delete = fail_delete
        self.saved = []
        self.deleted = []
        self.assets = {}

    def save(self, asset):
        if self.fail_save:
            raise RuntimeError("shadow unavailable")
        self.saved.append(f"{asset['symbol']}:{asset['interval']}")
        self.assets[(asset["symbol"], asset["interval"])] = asset
        return True

    def get(self, symbol, interval): return self.assets.get((symbol, interval))
    def get_symbol_assets(self, _symbol): return {"1D": None, "1W": None, "1M": None}
    def coverage(self, _symbols=None): return []
    def delete(self, symbols, intervals):
        self.deleted.append((symbols, intervals))
        if self.fail_delete:
            raise RuntimeError("delete unavailable")
        return 1


class MonotonicNoopStore(MemoryStore):
    def __init__(self, existing):
        super().__init__()
        self.assets[(existing["symbol"], existing["interval"])] = existing

    def save(self, _asset):
        return False


class FakePostgresConnection:
    def __init__(self, *, rows=None, rowcount=1):
        self.executions = []
        self.committed = False
        self.rows = list(rows or [])
        self.rowcount = rowcount

    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def execute(self, query, parameters=()):
        self.executions.append((query, parameters))
        return self
    def fetchone(self): return None
    def fetchall(self): return list(self.rows)
    def commit(self): self.committed = True


def _asset():
    return {
        "symbol": "NVDA", "interval": "1D", "asOf": "2026-07-10T04:00:00.000Z",
        "generatedAt": "2026-07-11T00:00:00.000Z", "assetVersion": "v2",
        "kernelVersion": "kernel-v3", "promptVersion": "prompt-v2", "status": "ready",
        "quality": {"state": "eligible"}, "build": {"assetContentDigest": "sha256:content"},
        "layers": {
            "structure": {"drawings": [{"id": "s1"}]},
            "trend": {"drawings": [{"id": "t1"}]},
            "agent": {"drawings": []},
        },
    }


if __name__ == "__main__":
    unittest.main()
