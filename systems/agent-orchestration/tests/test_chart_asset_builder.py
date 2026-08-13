from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "systems" / "market-data" / "shared", ROOT / "systems" / "agent-orchestration" / "shared"):
    if str(path) not in sys.path: sys.path.insert(0, str(path))

from market_data.analytics.analysis_candles import AnalysisCandleBundle  # noqa: E402
from market_data.analytics.analysis_repair import AnalysisRepairResult  # noqa: E402
from gops_agents.chart_assets.builder import ASSET_VERSION, ChartAssetBuilder  # noqa: E402
from gops_agents.chart_assets.commentary import ChartCommentaryGenerationError  # noqa: E402
from gops_agents.chart_assets.envelope import ChartAssetBuildEnvelope  # noqa: E402
from gops_agents.chart_assets.progress import InMemoryChartAssetProgressStore  # noqa: E402
from gops_agents.chart_assets.storage import (  # noqa: E402
    MAX_ASSET_BYTES, _validate_asset_identity, _validate_asset_schema,
)


class ChartAssetBuilderTest(unittest.TestCase):
    def test_builds_geometry_asset_without_llm_or_legacy_layers(self):
        rows = _rows(120, "1m")
        storage = MemoryStorage()
        progress = InMemoryChartAssetProgressStore()
        builder = ChartAssetBuilder(candle_loader=Loader(rows), storage=storage, progress=progress, concurrency=1)
        envelope = ChartAssetBuildEnvelope.create(requested_by="test", symbols=["NVDA"], intervals=["1m"])

        state = builder.run(envelope)

        self.assertEqual(state["status"], "completed_with_warnings")
        asset = storage.assets[("NVDA", "1m")]
        self.assertEqual(asset["assetVersion"], ASSET_VERSION)
        self.assertEqual(asset["coverage"]["state"], "partial")
        self.assertIn("geometry", asset)
        self.assertIn("patterns", asset["geometry"])
        self.assertIn("primaryPattern", asset["geometry"])
        self.assertIn("tradePlan", asset["geometry"])
        self.assertIn("indicators", asset)
        self.assertNotIn("layers", asset)
        self.assertNotIn("commentary", asset)
        _validate_asset_schema(asset)
        _validate_asset_identity(asset)

    def test_same_input_and_algorithm_is_noop(self):
        rows = _rows(120, "1D")
        storage = MemoryStorage()
        progress = InMemoryChartAssetProgressStore()
        builder = ChartAssetBuilder(candle_loader=Loader(rows), storage=storage, progress=progress, concurrency=1)
        first = ChartAssetBuildEnvelope.create(requested_by="test", symbols=["NVDA"], intervals=["1D"])
        second = ChartAssetBuildEnvelope.create(requested_by="test", symbols=["NVDA"], intervals=["1D"])

        builder.run(first)
        state = builder.run(second)

        self.assertEqual(state["recentItems"][-1]["status"], "unchanged")
        self.assertEqual(storage.save_count, 1)

    def test_insufficient_data_preserves_existing_asset(self):
        rows = _rows(119, "1D")
        storage = MemoryStorage()
        storage.assets[("NVDA", "1D")] = {"assetVersion": "geometry", "symbol": "NVDA", "interval": "1D"}
        progress = InMemoryChartAssetProgressStore()
        builder = ChartAssetBuilder(candle_loader=Loader(rows), storage=storage, progress=progress, concurrency=1)
        envelope = ChartAssetBuildEnvelope.create(requested_by="test", symbols=["NVDA"], intervals=["1D"])

        state = builder.run(envelope)

        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["recentItems"][-1]["reason"], "existing_asset_preserved")
        self.assertEqual(storage.save_count, 0)

    def test_scheduled_build_skips_before_loading_or_repairing(self):
        storage = MemoryStorage()
        progress = InMemoryChartAssetProgressStore()
        builder = ChartAssetBuilder(
            candle_loader=RaisingLoader(), storage=storage, progress=progress,
            repair_service=RaisingRepair(), concurrency=1,
        )
        envelope = ChartAssetBuildEnvelope.create(
            requested_by="test", symbols=["AMD"], intervals=["1D"], source="scheduled",
        )

        state = builder.run(envelope)

        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["recentItems"][-1]["status"], "skipped")
        self.assertEqual(state["recentItems"][-1]["reason"], "manual_refresh_only")
        self.assertEqual(storage.save_count, 0)

    def test_simulation_build_uses_cutoff_loader_and_snapshot_storage_only(self):
        rows = _rows(140, "1D")
        cutoff = rows[129]["timestamp"]
        storage = MemoryStorage()
        loader = Loader(rows)
        builder = ChartAssetBuilder(
            candle_loader=loader, storage=storage, progress=InMemoryChartAssetProgressStore(),
            repair_service=RaisingRepair(), concurrency=1,
        )
        envelope = ChartAssetBuildEnvelope.create(
            requested_by="test", symbols=["NVDA"], intervals=["1D"], force=True,
            target="simulation", dataset_id="dataset-1", snapshot_cutoff=cutoff,
        )

        state = builder.run(envelope)

        self.assertEqual(state["recentItems"][-1]["status"], "saved")
        self.assertEqual(storage.save_count, 0, "simulation builds must not mutate the LIVE row")
        self.assertEqual(storage.snapshot_save_count, 1)
        snapshot = storage.snapshots[("dataset-1", "NVDA", "1D")]
        self.assertEqual(snapshot["asOf"], cutoff)
        self.assertEqual(loader.cutoff_calls, [("NVDA", ("1D",), cutoff)])
        saved_log = json.loads(state["logs"][-1])
        self.assertEqual(saved_log["target"], "simulation")
        self.assertEqual(saved_log["datasetId"], "dataset-1")
        self.assertEqual(saved_log["snapshotCutoff"], cutoff)

    def test_nvda_demo_projection_runs_before_v5_commentary_and_saves_one_snapshot(self):
        rows = _rows(140, "1D")
        cutoff = rows[-1]["timestamp"]
        live_source = {"assetVersion": "geometry", "marker": "live-demo-source"}
        storage = MemoryStorage()
        storage.assets[("NVDA", "1D")] = live_source
        progress = InMemoryChartAssetProgressStore()
        commentary = _ready_commentary(rows[-1]["timestamp"])
        builder = ChartAssetBuilder(
            candle_loader=Loader(rows),
            storage=storage,
            progress=progress,
            commentary_writer=DemoWriter(),
            commentary_context_loader=DemoContextLoader(),
            concurrency=1,
        )
        envelope = ChartAssetBuildEnvelope.create(
            requested_by="test", symbols=["NVDA"], intervals=["1D"], force=True,
            target="simulation", dataset_id="sp500-full-20260715-kst-v3", snapshot_cutoff=cutoff,
        )

        def project_demo(**kwargs):
            projected = kwargs["base_asset"]
            projected["geometry"]["demoProjection"] = True
            return projected

        with (
            patch("gops_agents.chart_assets.builder.project_nvda_simulation_demo_snapshot", side_effect=project_demo) as project,
            patch("gops_agents.chart_assets.builder.is_complete_nvda_simulation_demo_snapshot", return_value=True),
            patch("gops_agents.chart_assets.builder.build_chart_commentary_fact_pack", return_value={"contextDigest": "sha256:facts"}) as fact_pack,
            patch("gops_agents.chart_assets.builder.generate_chart_commentary", return_value=(commentary, 7)),
        ):
            state = builder.run(envelope)

        self.assertEqual(state["recentItems"][-1]["status"], "saved")
        self.assertEqual(storage.save_count, 0)
        self.assertEqual(storage.snapshot_save_count, 1)
        self.assertIs(storage.assets[("NVDA", "1D")], live_source)
        snapshot = storage.snapshots[("sp500-full-20260715-kst-v3", "NVDA", "1D")]
        self.assertTrue(snapshot["geometry"]["demoProjection"])
        self.assertEqual(snapshot["commentary"]["promptVersion"], "chart-commentary.ko.v5")
        self.assertIs(project.call_args.kwargs["source_asset"], live_source)
        self.assertTrue(fact_pack.call_args.kwargs["geometry"]["demoProjection"])
        asset_log = next(json.loads(item) for item in state["logs"] if "chart_asset_saved" in item)
        self.assertTrue(asset_log["simulationDemoProjection"])

    def test_nvda_demo_commentary_failure_preserves_the_existing_snapshot(self):
        rows = _rows(140, "1D")
        cutoff = rows[-1]["timestamp"]
        existing = {"assetVersion": "geometry", "symbol": "NVDA", "interval": "1D", "marker": "old"}
        storage = MemoryStorage()
        storage.assets[("NVDA", "1D")] = {"assetVersion": "geometry", "marker": "live-demo-source"}
        storage.snapshots[("sp500-full-20260715-kst-v3", "NVDA", "1D")] = existing
        builder = ChartAssetBuilder(
            candle_loader=Loader(rows),
            storage=storage,
            progress=InMemoryChartAssetProgressStore(),
            commentary_writer=DemoWriter(),
            commentary_context_loader=DemoContextLoader(),
            concurrency=1,
        )
        envelope = ChartAssetBuildEnvelope.create(
            requested_by="test", symbols=["NVDA"], intervals=["1D"], force=True,
            target="simulation", dataset_id="sp500-full-20260715-kst-v3", snapshot_cutoff=cutoff,
        )

        with (
            patch(
                "gops_agents.chart_assets.builder.project_nvda_simulation_demo_snapshot",
                side_effect=lambda **kwargs: kwargs["base_asset"],
            ),
            patch("gops_agents.chart_assets.builder.build_chart_commentary_fact_pack", return_value={"contextDigest": "sha256:facts"}),
            patch(
                "gops_agents.chart_assets.builder.generate_chart_commentary",
                side_effect=ChartCommentaryGenerationError("provider failed", code="provider_server"),
            ),
        ):
            state = builder.run(envelope)

        self.assertEqual(state["status"], "completed_with_errors")
        self.assertEqual(state["recentItems"][-1]["reason"], "commentary_generation_failed")
        self.assertIs(storage.snapshots[("sp500-full-20260715-kst-v3", "NVDA", "1D")], existing)
        self.assertEqual(storage.snapshot_save_count, 0)

    def test_manual_force_is_the_only_path_that_replaces_existing_asset(self):
        rows = _rows(120, "1D")
        storage = MemoryStorage()
        storage.assets[("AMD", "1D")] = {"assetVersion": "geometry", "symbol": "AMD", "interval": "1D"}
        progress = InMemoryChartAssetProgressStore()
        builder = ChartAssetBuilder(candle_loader=Loader(rows), storage=storage, progress=progress, concurrency=1)
        envelope = ChartAssetBuildEnvelope.create(
            requested_by="test", symbols=["AMD"], intervals=["1D"], source="manual", force=True,
        )

        state = builder.run(envelope)

        self.assertEqual(state["recentItems"][-1]["status"], "saved")
        self.assertEqual(storage.save_count, 1)
        saved_log = json.loads(state["logs"][-1])
        self.assertEqual(saved_log["traceMode"], "geometry-analysis-trace-v2")
        self.assertTrue(saved_log["writeVerified"])
        self.assertEqual(saved_log["asOf"], storage.assets[("AMD", "1D")]["asOf"])

    def test_force_build_preserves_a_newer_as_of_asset(self):
        rows = _rows(120, "1D")
        existing = {
            "assetVersion": "geometry", "symbol": "AMD", "interval": "1D",
            "asOf": "2026-12-31T00:00:00.000Z", "marker": "newer",
        }
        storage = MemoryStorage()
        storage.assets[("AMD", "1D")] = existing
        builder = ChartAssetBuilder(
            candle_loader=Loader(rows), storage=storage,
            progress=InMemoryChartAssetProgressStore(), concurrency=1,
        )

        state = builder.run(ChartAssetBuildEnvelope.create(
            requested_by="test", symbols=["AMD"], intervals=["1D"], force=True,
        ))

        self.assertEqual(state["status"], "completed_with_errors")
        self.assertIn("older than the stored asset", state["recentItems"][-1]["error"])
        self.assertIs(storage.assets[("AMD", "1D")], existing)
        self.assertEqual(storage.save_count, 0)

    def test_provider_confirmed_empty_gaps_do_not_block_asset_save(self):
        rows = _rows(380, "1m")
        storage = MemoryStorage()
        progress = InMemoryChartAssetProgressStore()
        loader = Loader(rows, coverage={
            "coverageState": "data_insufficient",
            "recentContiguousBars": 10,
            "missingBars": 35,
            "qualityFlags": ["interior_gap"],
        })
        builder = ChartAssetBuilder(
            candle_loader=loader,
            storage=storage,
            progress=progress,
            repair_service=ConfirmedEmptyRepair(35),
            concurrency=1,
        )
        envelope = ChartAssetBuildEnvelope.create(requested_by="test", symbols=["A"], intervals=["1m"])

        state = builder.run(envelope)

        self.assertEqual(state["status"], "completed")
        asset = storage.assets[("A", "1m")]
        self.assertEqual(asset["coverage"]["state"], "full")
        self.assertEqual(asset["coverage"]["confirmedEmptyBars"], 35)
        self.assertEqual(asset["coverage"]["contiguousBars"], 10)
        self.assertIn("provider_confirmed_empty", asset["coverage"]["qualityFlags"])
        _validate_asset_schema(asset)

    def test_v6_optional_geometry_fields_are_preserved(self):
        rows = _rows(120, "1D")
        storage = MemoryStorage()
        progress = InMemoryChartAssetProgressStore()
        builder = ChartAssetBuilder(candle_loader=Loader(rows), storage=storage, progress=progress, concurrency=1)
        result = _analysis_result()
        result.update({
            "trends": [{"id": "trend-1", "kind": "uptrend"}],
            "primaryTrend": {"id": "trend-1", "kind": "uptrend"},
            "drawingGroups": {"levels": [], "trend": ["chart-asset:trend-1"], "pattern": []},
            "analysisTrace": {
                "version": "geometry-analysis-trace-v2",
                "pivots": [],
                "levelCandidates": [],
                "trendCandidates": [{"id": "trend-1"}],
                "patternCandidates": [],
                "selections": {
                    "levelCandidateIds": [],
                    "trendCandidateIds": ["trend-1"],
                    "patternCandidateIds": [],
                },
                "omittedCounts": {"trendCandidates": 2},
                "completeness": {
                    "complete": True,
                    "detected": {"levels": 0, "trends": 1, "patterns": 0},
                    "stored": {"levels": 0, "trends": 1, "patterns": 0},
                },
            },
        })

        with patch("gops_agents.chart_assets.builder.analyze_geometry", return_value=result):
            builder.run(ChartAssetBuildEnvelope.create(
                requested_by="test", symbols=["NVDA"], intervals=["1D"], force=True,
            ))

        geometry = storage.assets[("NVDA", "1D")]["geometry"]
        self.assertEqual(geometry["primaryTrend"]["id"], "trend-1")
        self.assertEqual(geometry["drawingGroups"]["trend"], ["chart-asset:trend-1"])
        self.assertEqual(geometry["analysisTrace"]["omittedCounts"], {"trendCandidates": 2})

    def test_complete_recorded_trace_fits_the_asset_without_candidate_pruning(self):
        fixture = ROOT / "systems" / "market-data" / "tests" / "fixtures" / "chart_assets_v2" / "tsla-1d.json"
        rows = json.loads(fixture.read_text(encoding="utf-8"))[-380:]
        storage = MemoryStorage()
        progress = InMemoryChartAssetProgressStore()
        builder = ChartAssetBuilder(
            candle_loader=Loader(rows), storage=storage, progress=progress, concurrency=1,
        )

        state = builder.run(ChartAssetBuildEnvelope.create(
            requested_by="test", symbols=["TSLA"], intervals=["1D"], force=True,
        ))

        self.assertEqual(state["status"], "completed")
        asset = storage.assets[("TSLA", "1D")]
        trace = asset["geometry"]["analysisTrace"]
        self.assertEqual(trace["version"], "geometry-analysis-trace-v2")
        self.assertEqual(trace["completeness"]["detected"], trace["completeness"]["stored"])
        self.assertEqual(
            [len(trace[field]) for field in ("levelCandidates", "trendCandidates", "patternCandidates")],
            [15, 50, 16],
        )
        payload_bytes = len(json.dumps(
            asset, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8"))
        self.assertLessEqual(payload_bytes, MAX_ASSET_BYTES)
        _validate_asset_schema(asset)
        _validate_asset_identity(asset)

    def test_recorded_aapl_asset_passes_schema_and_storage_identity_validation(self):
        fixture = ROOT / "systems" / "market-data" / "tests" / "fixtures" / "chart_assets_v2" / "aapl-1d.json"
        rows = json.loads(fixture.read_text(encoding="utf-8"))[-380:]
        storage = MemoryStorage()
        builder = ChartAssetBuilder(
            candle_loader=Loader(rows), storage=storage,
            progress=InMemoryChartAssetProgressStore(), concurrency=1,
        )

        state = builder.run(ChartAssetBuildEnvelope.create(
            requested_by="test", symbols=["AAPL"], intervals=["1D"], force=True,
        ))

        self.assertEqual(state["recentItems"][-1]["status"], "saved")
        asset = storage.assets[("AAPL", "1D")]
        _validate_asset_schema(asset)
        _validate_asset_identity(asset)

    def test_oversized_v6_trace_preserves_existing_asset(self):
        rows = _rows(120, "1D")
        existing = {"assetVersion": "geometry", "symbol": "NVDA", "interval": "1D", "marker": "old"}
        storage = MemoryStorage()
        storage.assets[("NVDA", "1D")] = existing
        progress = InMemoryChartAssetProgressStore()
        builder = ChartAssetBuilder(candle_loader=Loader(rows), storage=storage, progress=progress, concurrency=1)
        result = _analysis_result()
        result["analysisTrace"] = {
            "version": "geometry-analysis-trace-v2",
            "pivots": [],
            "levelCandidates": [{
                "id": "oversized", "selected": True, "score": 1.0,
                "metrics": {"note": "x" * (256 * 1024)},
            }],
            "trendCandidates": [],
            "patternCandidates": [],
            "selections": {
                "levelCandidateIds": ["oversized"], "trendCandidateIds": [], "patternCandidateIds": [],
            },
            "omittedCounts": {},
            "completeness": {
                "complete": True,
                "detected": {"levels": 1, "trends": 0, "patterns": 0},
                "stored": {"levels": 1, "trends": 0, "patterns": 0},
            },
        }

        with patch("gops_agents.chart_assets.builder.analyze_geometry", return_value=result):
            state = builder.run(ChartAssetBuildEnvelope.create(
                requested_by="test", symbols=["NVDA"], intervals=["1D"], force=True,
            ))

        self.assertEqual(state["status"], "completed_with_errors")
        self.assertIn("payload exceeds", state["recentItems"][-1]["error"])
        self.assertIs(storage.assets[("NVDA", "1D")], existing)
        self.assertEqual(storage.save_count, 0)

    def test_oversized_trace_does_not_prune_a_retained_competitor(self):
        rows = _rows(120, "1D")
        existing = {"assetVersion": "geometry", "symbol": "NVDA", "interval": "1D", "marker": "old"}
        storage = MemoryStorage()
        storage.assets[("NVDA", "1D")] = existing
        progress = InMemoryChartAssetProgressStore()
        builder = ChartAssetBuilder(candle_loader=Loader(rows), storage=storage, progress=progress, concurrency=1)
        result = _analysis_result()
        result["analysisTrace"] = {
            "version": "geometry-analysis-trace-v2",
            "pivots": [
                {"id": "pivot-selected", "timestamp": rows[-3]["timestamp"], "confirmedAt": rows[-2]["timestamp"], "price": 100, "kind": "L"},
                {"id": "pivot-rejected", "timestamp": rows[-5]["timestamp"], "confirmedAt": rows[-4]["timestamp"], "price": 105, "kind": "H"},
            ],
            "levelCandidates": [
                {
                    "id": "selected", "score": .9, "selected": True,
                    "evidenceRefs": ["pivot-selected"], "touches": [], "metrics": {},
                },
                {
                    "id": "rejected", "score": .1, "selected": False,
                    "evidenceRefs": ["pivot-rejected"],
                    "touches": [{"id": "touch-rejected", "note": "x" * (256 * 1024)}],
                    "metrics": {},
                },
            ],
            "trendCandidates": [],
            "patternCandidates": [],
            "selections": {
                "levelCandidateIds": ["selected"], "trendCandidateIds": [], "patternCandidateIds": [],
            },
            "omittedCounts": {
                "levelCandidates": 0, "trendCandidates": 0, "patternCandidates": 0, "touchEpisodes": 0,
            },
            "completeness": {
                "complete": True,
                "detected": {"levels": 2, "trends": 0, "patterns": 0},
                "stored": {"levels": 2, "trends": 0, "patterns": 0},
            },
        }

        with patch("gops_agents.chart_assets.builder.analyze_geometry", return_value=result):
            state = builder.run(ChartAssetBuildEnvelope.create(
                requested_by="test", symbols=["NVDA"], intervals=["1D"], force=True,
            ))

        self.assertEqual(state["status"], "completed_with_errors")
        self.assertIn("payload exceeds", state["recentItems"][-1]["error"])
        self.assertIs(storage.assets[("NVDA", "1D")], existing)
        self.assertEqual(storage.save_count, 0)


class Loader:
    def __init__(self, rows, coverage=None): self.rows = rows; self.coverage = coverage; self.cutoff_calls = []
    def load_symbol(self, symbol, intervals):
        interval = intervals[0]
        actual = len(self.rows)
        return AnalysisCandleBundle(
            rows={interval: self.rows},
            coverage={interval: self.coverage or {
                "coverageState": "full" if actual >= (312 if interval == "1W" else 380) else "partial" if actual >= 120 else "data_insufficient",
                "recentContiguousBars": actual, "missingBars": max(0, (312 if interval == "1W" else 380) - actual),
            }},
            digests={interval: f"sha256:{interval}:{actual}"},
        )
    def load_symbol_at(self, symbol, intervals, cutoff):
        normalized = cutoff.astimezone(timezone.utc)
        rows = [row for row in self.rows if datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")) <= normalized]
        self.cutoff_calls.append((symbol, tuple(intervals), normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z")))
        return Loader(rows, self.coverage).load_symbol(symbol, intervals)


class RaisingLoader:
    def load_symbol(self, *_args, **_kwargs):
        raise AssertionError("scheduled manual-only policy must skip before candle loading")


class RaisingRepair:
    def ensure_ready(self, *_args, **_kwargs):
        raise AssertionError("scheduled manual-only policy must skip before candle repair")


class DemoWriter:
    model = "fixture-model"

    def generate(self, _fact_pack):
        raise AssertionError("generate_chart_commentary is patched by this fixture")


class DemoContextLoader:
    def load(self, **_kwargs):
        return {"news": [], "earnings": [], "missingData": []}


class ConfirmedEmptyRepair:
    def __init__(self, count): self.count = count
    def ensure_ready(self, *_args, **_kwargs):
        return AnalysisRepairResult(
            checked=True,
            attempted=True,
            repaired=False,
            unavailable=False,
            missing_before=self.count,
            missing_after=self.count,
            materialized_rows=0,
            reason="provider_confirmed_empty",
            confirmed_empty_bars=self.count,
        )


class MemoryStorage:
    def __init__(self): self.assets = {}; self.snapshots = {}; self.save_count = 0; self.snapshot_save_count = 0
    def get(self, symbol, interval): return self.assets.get((symbol, interval))
    def save(self, asset):
        self.save_count += 1
        self.assets[(asset["symbol"], asset["interval"])] = asset
        return True
    def get_snapshot(self, dataset_id, symbol, interval, _cutoff=None):
        return self.snapshots.get((dataset_id, symbol, interval))
    def save_snapshot(self, dataset_id, _snapshot_cutoff, asset):
        self.snapshot_save_count += 1
        self.snapshots[(dataset_id, asset["symbol"], asset["interval"])] = asset
        return True


def _rows(count: int, interval: str):
    started = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)
    step = timedelta(days=1) if interval == "1D" else timedelta(minutes={"5m": 5, "1h": 60}.get(interval, 60))
    return [{
        "candleKey": (started + index * step).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "timestamp": (started + index * step).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "barIndex": index, "open": 100 + index * .1, "high": 101 + index * .1,
        "low": 99 + index * .1, "close": 100.2 + index * .1, "volume": 1_000_000,
        "isClosed": True, "interval": interval,
    } for index in range(count)]


def _analysis_result():
    return {
        "drawings": [],
        "supports": [],
        "resistances": [],
        "patterns": [],
        "primaryPattern": None,
        "tradePlan": None,
        "primaryTriangle": None,
        "historicalTriangle": None,
        "evidence": [],
        "indicators": {},
    }


def _ready_commentary(as_of: str):
    return {
        "version": "chart-commentary.v2",
        "status": "ready",
        "generatedAt": as_of,
        "model": "fixture-model",
        "promptVersion": "chart-commentary.ko.v5",
        "sourceIdentity": {
            "geometryInputDigest": "sha256:1D:140",
            "candlesAsOf": as_of,
            "indicatorsAsOf": as_of,
            "contextDigest": "sha256:facts",
        },
        "paragraphs": [],
        "indicatorRecommendations": [],
        "references": [],
        "limitations": [],
    }


if __name__ == "__main__": unittest.main()
