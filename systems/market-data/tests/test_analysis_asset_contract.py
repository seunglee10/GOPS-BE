from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class ChartAnalysisAssetContractTest(unittest.TestCase):
    def test_canonical_schema_points_only_to_geometry_contract(self):
        canonical = json.loads((ROOT / "shared/chart-contract/chart-analysis-asset.schema.json").read_text())
        self.assertEqual(canonical["$ref"], "chart-geometry-asset.schema.json")
        geometry = json.loads((ROOT / "shared/chart-contract/chart-geometry-asset.schema.json").read_text())
        self.assertEqual(geometry["properties"]["assetVersion"]["const"], "geometry")
        self.assertEqual(geometry["$defs"]["interval"]["enum"], ["1m", "5m", "10m", "1h", "4h", "1D", "1W"])
        geometry_contract = geometry["properties"]["geometry"]
        self.assertEqual(geometry_contract["properties"]["drawings"]["maxItems"], 8)
        self.assertTrue({"patterns", "primaryPattern"}.issubset(geometry_contract["required"]))
        self.assertIn("tradePlan", geometry_contract["properties"])
        self.assertIn("tradePlan", geometry_contract["required"])
        self.assertEqual(
            set(geometry["$defs"]["pattern"]["properties"]["kind"]["enum"]),
            {
                "ascending_triangle", "descending_triangle", "symmetrical_triangle",
                "bullish_flag", "bearish_flag", "bullish_pennant", "bearish_pennant",
                "bullish_rectangle", "bearish_rectangle", "rising_wedge", "falling_wedge",
                "descending_channel_breakout", "ascending_channel_breakdown",
            },
        )
        self.assertTrue({"symbol", "interval", "sourceInterval"}.issubset(geometry["$defs"]["drawing"]["required"]))


if __name__ == "__main__":
    unittest.main()
