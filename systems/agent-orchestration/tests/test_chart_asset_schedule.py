from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "systems" / "agent-orchestration" / "jobs" / "chart-asset-schedule" / "main.py"
SPEC = importlib.util.spec_from_file_location("chart_asset_schedule_main", MODULE_PATH)
assert SPEC and SPEC.loader
schedule = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(schedule)


class ChartAssetScheduleParsingTest(unittest.TestCase):
    def test_intervals_preserve_the_case_sensitive_geometry_contract(self):
        self.assertEqual(
            schedule._parse_intervals("1m,5m,10m,1h,4h,1D,1W"),
            ["1m", "5m", "10m", "1h", "4h", "1D", "1W"],
        )

    def test_symbols_are_normalized_to_uppercase(self):
        self.assertEqual(schedule._parse_symbols("nvda, brk.b,NVDA"), ["NVDA", "BRK.B"])

    def test_scheduled_build_uses_low_priority_source(self):
        store = _Store()
        with (
            patch.object(schedule, "PostgresChartAssetJobStore", return_value=store),
            patch.dict(
                os.environ,
                {
                    "CHART_ASSET_SYMBOLS": "nvda",
                    "CHART_ASSET_INTERVALS": "1D",
                    "CHART_ASSET_SCHEDULED": "true",
                },
                clear=True,
            ),
        ):
            self.assertEqual(schedule.main(), 0)

        self.assertIsNotNone(store.envelope)
        self.assertEqual(store.envelope.source, "scheduled")
        self.assertEqual(store.envelope.priority, 10)

    def test_scheduled_build_defaults_to_one_minute_and_one_day(self):
        store = _Store()
        with (
            patch.object(schedule, "PostgresChartAssetJobStore", return_value=store),
            patch.dict(
                os.environ,
                {
                    "CHART_ASSET_SYMBOLS": "nvda",
                    "CHART_ASSET_SCHEDULED": "true",
                },
                clear=True,
            ),
        ):
            self.assertEqual(schedule.main(), 0)

        self.assertEqual(store.envelope.intervals, ("1m", "1D"))


class _Store:
    def __init__(self):
        self.envelope = None

    def enqueue(self, envelope):
        self.envelope = envelope
        return envelope.to_dict()


if __name__ == "__main__":
    unittest.main()
