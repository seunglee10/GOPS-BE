from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "systems/market-data/shared"))

from alfaka.serving.provider import merge_candles, with_coverage_metadata  # noqa: E402


class ServingProviderTests(unittest.TestCase):
    def test_merge_candles_prefers_canonical_daily_timestamp_over_legacy_offset(self) -> None:
        candles = merge_candles(
            [{
                "symbol": "AAPL",
                "interval": "1D",
                "timestamp": "2026-06-30T00:00:00.000Z",
                "close": 1,
                "createdAt": "2026-06-30T18:19:00.000Z",
            }],
            [{
                "symbol": "AAPL",
                "interval": "1D",
                "timestamp": "2026-06-30T04:00:00.000Z",
                "close": 2,
                "createdAt": "2026-06-30T18:20:00.000Z",
            }],
        )

        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0]["timestamp"], "2026-06-30T00:00:00.000Z")
        self.assertEqual(candles[0]["close"], 1)

    def test_merge_candles_prefers_newer_observed_canonical_candle(self) -> None:
        candles = merge_candles(
            [{
                "symbol": "AAPL",
                "interval": "1D",
                "timestamp": "2026-06-30T00:00:00.000Z",
                "close": 1,
                "createdAt": "2026-06-30T18:19:00.000Z",
            }],
            [{
                "symbol": "AAPL",
                "interval": "1D",
                "timestamp": "2026-06-30T00:00:00.000Z",
                "close": 2,
                "createdAt": "2026-06-30T18:20:00.000Z",
            }],
        )

        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0]["timestamp"], "2026-06-30T00:00:00.000Z")
        self.assertEqual(candles[0]["close"], 2)

    def test_empty_range_before_coverage_keeps_history_available_until_boundary(self) -> None:
        payload = with_coverage_metadata(
            {"symbol": "AAPL", "interval": "1m", "candles": []},
            {
                "rowCount": 100,
                "availableFrom": "2026-06-24T13:30:00.000Z",
                "availableTo": "2026-06-30T20:00:00.000Z",
            },
            120,
            requested_range={
                "from": "2026-06-23T13:30:00.000Z",
                "to": "2026-06-23T14:30:00.000Z",
            },
        )

        self.assertTrue(payload["hasMoreBefore"])

    def test_empty_range_at_no_data_boundary_closes_history(self) -> None:
        payload = with_coverage_metadata(
            {"symbol": "AAPL", "interval": "1m", "candles": []},
            {
                "rowCount": 100,
                "availableFrom": "2026-06-24T13:30:00.000Z",
                "availableTo": "2026-06-30T20:00:00.000Z",
            },
            120,
            requested_range={"before": "2026-06-23T13:30:00.000Z"},
            no_data_before="2026-06-23T13:30:00.000Z",
        )

        self.assertFalse(payload["hasMoreBefore"])


if __name__ == "__main__":
    unittest.main()
