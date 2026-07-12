from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
