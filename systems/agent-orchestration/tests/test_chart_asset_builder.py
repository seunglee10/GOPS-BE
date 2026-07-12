from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "systems" / "market-data" / "shared", ROOT / "systems" / "agent-orchestration" / "shared"):
    if str(path) not in sys.path: sys.path.insert(0, str(path))

from alfaka.analytics.analysis_candles import AnalysisCandleBundle  # noqa: E402
from gops_agents.chart_assets.builder import ASSET_VERSION, ChartAssetBuilder  # noqa: E402
from gops_agents.chart_assets.envelope import ChartAssetBuildEnvelope  # noqa: E402
from gops_agents.chart_assets.progress import InMemoryChartAssetProgressStore  # noqa: E402


class ChartAssetBuilderTest(unittest.TestCase):
    def test_builds_geometry_asset_without_llm_or_legacy_layers(self):
        rows = _rows(120, "5m")
        storage = MemoryStorage()
        progress = InMemoryChartAssetProgressStore()
        builder = ChartAssetBuilder(candle_loader=Loader(rows), storage=storage, progress=progress, concurrency=1)
        envelope = ChartAssetBuildEnvelope.create(requested_by="test", symbols=["NVDA"], intervals=["5m"])

        state = builder.run(envelope)

        self.assertEqual(state["status"], "completed_with_warnings")
        asset = storage.assets[("NVDA", "5m")]
        self.assertEqual(asset["assetVersion"], ASSET_VERSION)
        self.assertEqual(asset["coverage"]["state"], "partial")
        self.assertIn("geometry", asset)
        self.assertIn("indicators", asset)
        self.assertNotIn("layers", asset)
        self.assertNotIn("commentary", asset)

    def test_same_input_and_algorithm_is_noop(self):
        rows = _rows(120, "1h")
        storage = MemoryStorage()
        progress = InMemoryChartAssetProgressStore()
        builder = ChartAssetBuilder(candle_loader=Loader(rows), storage=storage, progress=progress, concurrency=1)
        first = ChartAssetBuildEnvelope.create(requested_by="test", symbols=["NVDA"], intervals=["1h"])
        second = ChartAssetBuildEnvelope.create(requested_by="test", symbols=["NVDA"], intervals=["1h"])

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

        self.assertEqual(state["status"], "completed_with_errors")
        self.assertEqual(storage.save_count, 0)


class Loader:
    def __init__(self, rows): self.rows = rows
    def load_symbol(self, symbol, intervals):
        interval = intervals[0]
        actual = len(self.rows)
        return AnalysisCandleBundle(
            rows={interval: self.rows},
            coverage={interval: {
                "coverageState": "full" if actual >= (312 if interval == "1W" else 380) else "partial" if actual >= 120 else "data_insufficient",
                "recentContiguousBars": actual, "missingBars": max(0, (312 if interval == "1W" else 380) - actual),
            }},
            digests={interval: f"sha256:{interval}:{actual}"},
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


if __name__ == "__main__": unittest.main()
