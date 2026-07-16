from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "systems" / "market-data" / "shared", ROOT / "systems" / "agent-orchestration" / "shared"):
    if str(path) not in sys.path: sys.path.insert(0, str(path))

from gops_agents.chart_assets.storage import (  # noqa: E402
    MAX_ASSET_BYTES, POSTGRES_TABLE, PostgresChartAssetStorage, _asset_projection,
    _validate_asset_schema, build_chart_asset_storage_from_env,
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
        self.assertIn("EXCLUDED.as_of >=", query)
        self.assertIn("EXCLUDED.payload_digest IS DISTINCT FROM", query)
        self.assertEqual(parameters[:2], ("NVDA", "1D"))

    def test_json_schema_accepts_legacy_and_v6_payload_fixtures(self):
        _validate_asset_schema(_asset())
        _validate_asset_schema(_channel_asset())
        _validate_asset_schema(_trace_asset_v2())

    def test_schema_failure_preserves_existing_row_before_postgres_write(self):
        connection = Connection()
        storage = PostgresChartAssetStorage("postgresql://test", connect=lambda *_args, **_kwargs: connection)
        asset = _asset()
        del asset["geometry"]["patterns"]

        with self.assertRaisesRegex(ValueError, "schema validation failed"):
            storage.save(asset)

        self.assertEqual(connection.executions, [])

    def test_coverage_projects_primary_pattern_for_each_symbol_interval(self):
        primary_pattern = {
            "kind": "bullish_flag",
            "state": "confirmed",
            "score": 0.88,
        }
        connection = Connection(rows=[{
            "symbol": "NVDA",
            "interval": "1D",
            "as_of": "2026-07-10T00:00:00.000Z",
            "generated_at": "2026-07-11T00:00:00.000Z",
            "status": "ready",
            "asset_version": "geometry",
            "algorithm_version": "ohlcv-consensus-pattern-families-v6",
            "coverage_state": "full",
            "payload_bytes": 512,
            "drawing_count": 3,
            "primary_pattern": primary_pattern,
            "trace_mode": "geometry-analysis-trace-v2",
            "level_candidates": 15,
            "trend_candidates": 50,
            "pattern_candidates": 16,
        }])
        storage = PostgresChartAssetStorage("postgresql://test", connect=lambda *_args, **_kwargs: connection)

        item = storage.coverage()[0]

        self.assertEqual(item["primaryPattern"], primary_pattern)
        self.assertEqual(item["traceCandidateCounts"], {"levels": 15, "trends": 50, "patterns": 16})
        self.assertEqual(item["algorithmVersion"], "ohlcv-consensus-pattern-families-v6")
        self.assertIn("primaryPattern", connection.executions[0][0])
        self.assertIn("primaryTriangle", connection.executions[0][0])

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

    def test_storage_rejects_oversized_payload_before_postgres_write(self):
        connection = Connection()
        storage = PostgresChartAssetStorage("postgresql://test", connect=lambda *_args, **_kwargs: connection)
        asset = _asset()
        asset["indicators"]["oversized"] = "x" * MAX_ASSET_BYTES

        with self.assertRaisesRegex(ValueError, "payload exceeds"):
            storage.save(asset)

        self.assertEqual(connection.executions, [])

    def test_storage_rejects_invalid_v6_drawing_group_before_postgres_write(self):
        connection = Connection()
        storage = PostgresChartAssetStorage("postgresql://test", connect=lambda *_args, **_kwargs: connection)
        asset = _asset()
        asset["geometry"]["drawingGroups"] = {
            "levels": ["missing-drawing"], "trend": [], "pattern": [],
        }

        with self.assertRaisesRegex(ValueError, "do not match"):
            storage.save(asset)

        self.assertEqual(connection.executions, [])

    def test_storage_rejects_every_dangling_trace_pivot_reference_before_postgres_write(self):
        for field in ("evidenceRefs", "anchorPivotIds", "touchPivotIds", "reactionPivotIds"):
            with self.subTest(field=field):
                connection = Connection()
                storage = PostgresChartAssetStorage(
                    "postgresql://test", connect=lambda *_args, **_kwargs: connection,
                )
                asset = _trace_asset()
                candidate = asset["geometry"]["analysisTrace"]["levelCandidates"][0]
                candidate[field] = ["missing-pivot"]
                if field == "touchPivotIds":
                    candidate["reactionPivotIds"] = []
                elif field == "reactionPivotIds":
                    candidate["touchPivotIds"] = ["pivot-1", "missing-pivot"]

                with self.assertRaisesRegex(ValueError, "pivot references"):
                    storage.save(asset)

                self.assertEqual(connection.executions, [])

    def test_storage_rejects_reaction_pivot_that_is_not_a_touch(self):
        connection = Connection()
        storage = PostgresChartAssetStorage("postgresql://test", connect=lambda *_args, **_kwargs: connection)
        asset = _trace_asset()
        candidate = asset["geometry"]["analysisTrace"]["levelCandidates"][0]
        candidate["touchPivotIds"] = []

        with self.assertRaisesRegex(ValueError, "evidence is invalid"):
            storage.save(asset)

        self.assertEqual(connection.executions, [])

    def test_storage_rejects_malformed_parallel_channel_before_postgres_write(self):
        for mutation in ("two_anchors", "missing_parallel_count"):
            with self.subTest(mutation=mutation):
                connection = Connection()
                storage = PostgresChartAssetStorage(
                    "postgresql://test", connect=lambda *_args, **_kwargs: connection,
                )
                asset = _channel_asset()
                drawing = asset["geometry"]["drawings"][0]
                if mutation == "two_anchors":
                    drawing["anchors"] = drawing["anchors"][:2]
                else:
                    drawing.pop("parallelLineCount")

                with self.assertRaisesRegex(ValueError, "drawing|trendParallelLines"):
                    storage.save(asset)

                self.assertEqual(connection.executions, [])

    def test_storage_rejects_incomplete_v6_trend_before_postgres_write(self):
        connection = Connection()
        storage = PostgresChartAssetStorage("postgresql://test", connect=lambda *_args, **_kwargs: connection)
        asset = _channel_asset()
        asset["geometry"]["trends"] = [{"id": "trend-1", "kind": "channel"}]
        asset["geometry"]["primaryTrend"] = {"id": "trend-1", "kind": "channel"}

        with self.assertRaisesRegex(ValueError, "trend contract"):
            storage.save(asset)

        self.assertEqual(connection.executions, [])

    def test_postgres_round_trip_preserves_optional_v6_geometry_fields(self):
        asset = _asset()
        asset["geometry"].update({
            "trends": [{"id": "trend-1", "kind": "uptrend"}],
            "primaryTrend": {"id": "trend-1", "kind": "uptrend"},
            "drawingGroups": {"levels": ["one"], "trend": ["trend-1"], "pattern": []},
            "analysisTrace": {
                "version": "geometry-analysis-trace-v1",
                "pivots": [],
                "levelCandidates": [],
                "trendCandidates": [],
                "patternCandidates": [],
                "selections": {
                    "levelCandidateIds": [], "trendCandidateIds": ["trend-1"], "patternCandidateIds": [],
                },
                "omittedCounts": {},
            },
        })
        connection = Connection(rows=[{"payload": asset}])
        storage = PostgresChartAssetStorage("postgresql://test", connect=lambda *_args, **_kwargs: connection)

        loaded = storage.get("nvda", "1D")

        self.assertEqual(loaded, asset)
        self.assertEqual(connection.executions[0][1], ("NVDA", "1D"))

    def test_postgres_save_accepts_complete_v2_trace_without_schema_migration(self):
        connection = Connection()
        storage = PostgresChartAssetStorage("postgresql://test", connect=lambda *_args, **_kwargs: connection)

        self.assertTrue(storage.save(_trace_asset_v2()))

        self.assertIn(f"INSERT INTO {POSTGRES_TABLE}", connection.executions[0][0])

    def test_schema_has_seven_interval_primary_key_and_eight_drawing_limit(self):
        sql = (ROOT / "systems" / "agent-orchestration" / "jobs" / "chart-asset-migrations" / "003_geometry_assets.sql").read_text(encoding="utf-8")
        self.assertIn('PRIMARY KEY (symbol, "interval")', sql)
        self.assertIn("drawing_count BETWEEN 0 AND 8", sql)
        self.assertIn("DROP CONSTRAINT IF EXISTS geometry_assets_drawing_count_check", sql)
        self.assertNotIn("'1M'", sql)


class Connection:
    def __init__(self, rows=None): self.executions = []; self.rowcount = 1; self.rows = rows or []
    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def execute(self, query, parameters=()): self.executions.append((query, parameters)); return self
    def commit(self): return None
    def fetchall(self): return self.rows
    def fetchone(self): return self.rows[0] if self.rows else None


def _asset():
    def drawing(drawing_id: str, price: float):
        return {
            "id": drawing_id,
            "type": "horizontalLine",
            "symbol": "NVDA",
            "interval": "1D",
            "sourceInterval": "1D",
            "anchors": [
                {"timestamp": "2026-07-09T04:00:00.000Z", "price": price},
                {"timestamp": "2026-07-10T04:00:00.000Z", "price": price},
            ],
            "style": {},
            "visible": True,
            "createdBy": "system",
            "sourceProposalId": "chart-asset:NVDA:1D:geometry",
            "createdAt": "2026-07-10T04:00:00.000Z",
            "updatedAt": "2026-07-10T04:00:00.000Z",
        }
    return {
        "assetVersion": "geometry", "algorithmVersion": "ohlcv-consensus-1", "symbol": "NVDA", "interval": "1D",
        "sourceInterval": "1D", "asOf": "2026-07-10T04:00:00.000Z", "generatedAt": "2026-07-11T00:00:00.000Z",
        "status": "ready", "inputDigest": "sha256:input", "coverage": {
            "state": "full", "targetBars": 380, "actualBars": 380,
            "contiguousBars": 380, "missingBars": 0,
        },
        "geometry": {
            "drawings": [drawing("one", 100.0), drawing("two", 110.0)],
            "supports": [], "resistances": [], "patterns": [],
            "primaryPattern": None, "tradePlan": None,
            "primaryTriangle": None, "historicalTriangle": None,
        },
        "indicators": {},
    }


def _trace_asset():
    asset = _asset()
    pivot_id = "pivot-1"
    candidate_id = "level-1"
    asset["geometry"]["analysisTrace"] = {
        "version": "geometry-analysis-trace-v1",
        "pivots": [{
            "id": pivot_id,
            "timestamp": "2026-07-10T03:00:00.000Z",
            "confirmedAt": "2026-07-10T03:30:00.000Z",
            "price": 100.0,
            "kind": "L",
        }],
        "levelCandidates": [{
            "id": candidate_id,
            "category": "level",
            "role": "support",
            "score": 0.9,
            "selected": True,
            "hardPass": True,
            "evidencePass": True,
            "activePass": True,
            "rejectReasons": [],
            "selectionTier": "confirmed",
            "importanceTier": "major",
            "importanceRank": 1,
            "anchors": [
                {"timestamp": "2026-07-09T04:00:00.000Z", "price": 100.0},
                {"timestamp": "2026-07-10T04:00:00.000Z", "price": 100.0},
            ],
            "evidenceRefs": [pivot_id],
            "anchorPivotIds": [pivot_id],
            "touchPivotIds": [pivot_id],
            "reactionPivotIds": [pivot_id],
            "touches": [],
            "touchRefs": [],
            "reactionRefs": [],
            "metrics": {},
        }],
        "trendCandidates": [],
        "patternCandidates": [],
        "selections": {
            "levelCandidateIds": [candidate_id],
            "trendCandidateIds": [],
            "patternCandidateIds": [],
        },
        "omittedCounts": {},
    }
    return asset


def _trace_asset_v2():
    asset = _trace_asset()
    trace = asset["geometry"]["analysisTrace"]
    trace["version"] = "geometry-analysis-trace-v2"
    candidate = trace["levelCandidates"][0]
    candidate.update({
        "categoryRank": 1,
        "disposition": "selected",
        "selectionReasons": ["confirmed"],
        "render": {"drawingType": "horizontalLine", "extension": "plot"},
    })
    trace["completeness"] = {
        "complete": True,
        "detected": {"levels": 1, "trends": 0, "patterns": 0},
        "stored": {"levels": 1, "trends": 0, "patterns": 0},
    }
    return asset


def _channel_asset():
    asset = _asset()
    asset["algorithmVersion"] = "ohlcv-consensus-pattern-families-v6"
    anchors = [
        {"timestamp": "2026-07-08T04:00:00.000Z", "price": 95.0},
        {"timestamp": "2026-07-10T04:00:00.000Z", "price": 100.0},
        {"timestamp": "2026-07-09T04:00:00.000Z", "price": 110.0},
    ]
    drawing = {
        **asset["geometry"]["drawings"][0],
        "id": "trend-drawing-1",
        "type": "trendParallelLines",
        "anchors": anchors,
        "parallelLineCount": 2,
    }
    trend = {
        "id": "trend-1",
        "kind": "channel",
        "direction": "up",
        "score": 0.9,
        "drawingId": drawing["id"],
        "anchors": anchors,
        "anchorPivotIds": ["pivot-1", "pivot-2", "pivot-3"],
        "touchPivotIds": ["pivot-1", "pivot-2", "pivot-3"],
        "reactionPivotIds": ["pivot-1"],
        "touchCount": 3,
        "reactionCount": 2,
        "slopeAtrPerBar": 0.1,
        "medianResidualAtr": 0.2,
        "currentDistanceAtr": 0.3,
        "lastTouchAgeBars": 2,
        "channelWidthAtr": 2.5,
        "parallelSlopeError": 0.05,
        "containment": 0.9,
    }
    asset["geometry"].update({
        "drawings": [drawing],
        "trends": [trend],
        "primaryTrend": dict(trend),
        "drawingGroups": {"levels": [], "trend": [drawing["id"]], "pattern": []},
    })
    return asset


if __name__ == "__main__": unittest.main()
