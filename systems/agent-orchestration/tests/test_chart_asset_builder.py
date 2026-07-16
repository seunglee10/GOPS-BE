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

from alfaka.analytics.analysis_candles import AnalysisCandleBundle  # noqa: E402
from alfaka.analytics.analysis_repair import AnalysisRepairResult  # noqa: E402
from gops_agents.chart_assets.builder import ASSET_VERSION, ChartAssetBuilder  # noqa: E402
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
                "version": "geometry-analysis-trace-v1",
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
            "version": "geometry-analysis-trace-v1",
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
            "version": "geometry-analysis-trace-v1",
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
    def __init__(self, rows, coverage=None): self.rows = rows; self.coverage = coverage
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


class RaisingLoader:
    def load_symbol(self, *_args, **_kwargs):
        raise AssertionError("scheduled manual-only policy must skip before candle loading")


class RaisingRepair:
    def ensure_ready(self, *_args, **_kwargs):
        raise AssertionError("scheduled manual-only policy must skip before candle repair")


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
    def __init__(self): self.assets = {}; self.save_count = 0
    def get(self, symbol, interval): return self.assets.get((symbol, interval))
    def save(self, asset):
        self.save_count += 1
        self.assets[(asset["symbol"], asset["interval"])] = asset
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


if __name__ == "__main__": unittest.main()
