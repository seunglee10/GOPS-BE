from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "systems" / "agent-orchestration" / "shared", ROOT / "systems" / "market-data" / "shared"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from gops_agents.chart_assets.storage import ChartAssetStorage  # noqa: E402


class FakeClient:
    def __init__(self):
        self.queries = []
        self.executions = []

    def query_json_each_row(self, query, parameters=None):
        self.queries.append((query, parameters))
        return [{"assetCount": 2}]

    def execute(self, query, parameters=None):
        self.executions.append((query, parameters))


class ChartAssetStorageTest(unittest.TestCase):
    def test_delete_removes_all_history_for_selected_pairs_synchronously(self):
        client = FakeClient()
        deleted = ChartAssetStorage(client=client).delete(["nvda", "NVDA"], ["1D", "1W", "1D"])
        self.assertEqual(deleted, 2)
        expected = {"symbols": ["NVDA"], "intervals": ["1D", "1W"]}
        self.assertEqual(client.queries[0][1], expected)
        self.assertEqual(client.executions[0][1], expected)
        self.assertIn("DELETE WHERE", client.executions[0][0])
        self.assertIn("mutations_sync = 1", client.executions[0][0])

    def test_delete_with_empty_selection_is_noop(self):
        client = FakeClient()
        self.assertEqual(ChartAssetStorage(client=client).delete([], ["1D"]), 0)
        self.assertEqual(client.queries, [])
        self.assertEqual(client.executions, [])


if __name__ == "__main__":
    unittest.main()
