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
        self.assertEqual(geometry["properties"]["geometry"]["properties"]["drawings"]["maxItems"], 6)
        self.assertTrue({"symbol", "interval", "sourceInterval"}.issubset(geometry["$defs"]["drawing"]["required"]))


if __name__ == "__main__":
    unittest.main()
