from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "systems/market-data/shared"))

from alfaka.serving.history_window import (  # noqa: E402
    clamp_range_start,
    history_floor_iso,
    range_ends_before_history,
)


class HistoryWindowTests(unittest.TestCase):
    def test_history_floor_aligns_to_requested_interval(self) -> None:
        env = {
            "MARKET_DATA_MAX_HISTORY_YEARS": "6",
            "MARKET_DATA_HISTORY_NOW": "2026-07-01T21:49:22.780Z",
        }

        with mock.patch.dict(os.environ, env):
            self.assertEqual(history_floor_iso("1m"), "2020-07-01T21:49:22.780Z")
            self.assertEqual(history_floor_iso("5m"), "2020-07-01T21:49:22.780Z")
            self.assertEqual(history_floor_iso("1D"), "2020-07-02T00:00:00.000Z")
            self.assertEqual(history_floor_iso("1W"), "2020-07-06T00:00:00.000Z")
            self.assertEqual(history_floor_iso("1M"), "2020-08-01T00:00:00.000Z")

    def test_history_floor_keeps_already_aligned_daily_monthly_starts(self) -> None:
        env = {
            "MARKET_DATA_MAX_HISTORY_YEARS": "6",
            "MARKET_DATA_HISTORY_NOW": "2026-07-01T00:00:00.000Z",
        }

        with mock.patch.dict(os.environ, env):
            self.assertEqual(history_floor_iso("1D"), "2020-07-01T00:00:00.000Z")
            self.assertEqual(history_floor_iso("1M"), "2020-07-01T00:00:00.000Z")

    def test_history_floor_can_be_disabled_for_manual_repairs(self) -> None:
        with mock.patch.dict(os.environ, {"MARKET_DATA_MAX_HISTORY_YEARS": "0"}):
            self.assertIsNone(history_floor_iso("1D"))
            self.assertEqual(
                clamp_range_start("2016-01-04T00:00:00.000Z", "1D"),
                ("2016-01-04T00:00:00.000Z", False, None),
            )

    def test_range_start_clamps_and_old_ranges_short_circuit(self) -> None:
        env = {
            "MARKET_DATA_MAX_HISTORY_YEARS": "6",
            "MARKET_DATA_HISTORY_NOW": "2026-07-01T00:00:00.000Z",
        }

        with mock.patch.dict(os.environ, env):
            self.assertEqual(
                clamp_range_start("2015-01-01T00:00:00.000Z", "1M"),
                ("2020-07-01T00:00:00.000Z", True, "2020-07-01T00:00:00.000Z"),
            )
            self.assertTrue(range_ends_before_history("2020-06-01T00:00:00.000Z", "1M"))
            self.assertFalse(range_ends_before_history("2020-08-01T00:00:00.000Z", "1M"))


if __name__ == "__main__":
    unittest.main()
