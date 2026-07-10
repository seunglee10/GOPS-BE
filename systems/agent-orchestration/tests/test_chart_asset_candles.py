from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "systems" / "agent-orchestration" / "shared", ROOT / "systems" / "market-data" / "shared"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from gops_agents.chart_assets.candles import ChartAssetCandleLoader  # noqa: E402


class FakeProvider:
    def __init__(self, rows):
        self.rows = rows

    def daily_candles(self, *_args, **_kwargs):
        return list(self.rows)

    def aggregated_daily_candles(self, *_args, **_kwargs):
        return list(self.rows)


class ChartAssetCandleLoaderTest(unittest.TestCase):
    def test_weekly_loader_drops_current_incomplete_bucket(self):
        loader = ChartAssetCandleLoader(
            FakeProvider([candle("2026-06-29T00:00:00.000Z"), candle("2026-07-06T00:00:00.000Z")]),
            now_provider=lambda: datetime(2026, 7, 11, tzinfo=timezone.utc),
        )

        rows = loader.load("NVDA", "1W")

        self.assertEqual([row["timestamp"] for row in rows], ["2026-06-29T00:00:00.000Z"])

    def test_monthly_loader_drops_current_incomplete_bucket(self):
        loader = ChartAssetCandleLoader(
            FakeProvider([candle("2026-06-01T00:00:00.000Z"), candle("2026-07-01T00:00:00.000Z")]),
            now_provider=lambda: datetime(2026, 7, 11, tzinfo=timezone.utc),
        )

        rows = loader.load("NVDA", "1M")

        self.assertEqual([row["timestamp"] for row in rows], ["2026-06-01T00:00:00.000Z"])

    def test_completed_weekly_bucket_is_retained_at_next_boundary(self):
        loader = ChartAssetCandleLoader(
            FakeProvider([candle("2026-07-06T00:00:00.000Z")]),
            now_provider=lambda: datetime(2026, 7, 13, tzinfo=timezone.utc),
        )

        self.assertEqual(len(loader.load("NVDA", "1W")), 1)

    def test_daily_loader_preserves_provider_closed_bar_behavior(self):
        loader = ChartAssetCandleLoader(
            FakeProvider([candle("2026-07-10T00:00:00.000Z")]),
            now_provider=lambda: datetime(2026, 7, 11, tzinfo=timezone.utc),
        )

        self.assertEqual(len(loader.load("NVDA", "1D")), 1)


def candle(timestamp: str) -> dict:
    return {
        "timestamp": timestamp,
        "open": 100,
        "high": 102,
        "low": 99,
        "close": 101,
        "volume": 1000,
        "isClosed": True,
    }


if __name__ == "__main__":
    unittest.main()
