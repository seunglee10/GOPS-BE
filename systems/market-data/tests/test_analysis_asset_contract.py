from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class ChartAnalysisAssetContractTest(unittest.TestCase):
    def test_canonical_schema_discriminates_preserved_v1_and_v2(self):
        canonical = json.loads((ROOT / "shared/chart-contract/chart-analysis-asset.schema.json").read_text())
        self.assertEqual(canonical["oneOf"], [
            {"$ref": "chart-analysis-asset-v1.schema.json"},
            {"$ref": "chart-analysis-asset-v2.schema.json"},
        ])
        v1 = json.loads((ROOT / "shared/chart-contract/chart-analysis-asset-v1.schema.json").read_text())
        v2 = json.loads((ROOT / "shared/chart-contract/chart-analysis-asset-v2.schema.json").read_text())
        self.assertEqual(v1["properties"]["assetVersion"]["const"], "v1")
        self.assertEqual(v2["properties"]["assetVersion"]["const"], "v2")
        self.assertEqual(v2["properties"]["kernelVersion"]["enum"], ["kernel-v2", "kernel-v3"])
        self.assertEqual(v2["properties"]["qualityPolicyVersion"]["enum"], ["chart-quality-v1", "chart-quality-v2"])
        self.assertEqual(v2["$defs"]["input"]["properties"]["candleContractVersion"]["enum"], ["analysis-candles-v1", "v2"])


if __name__ == "__main__":
    unittest.main()
