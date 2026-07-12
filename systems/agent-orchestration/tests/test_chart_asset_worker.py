from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "systems" / "agent-orchestration" / "pods" / "chart-asset-builder" / "main.py"
SPEC = importlib.util.spec_from_file_location("chart_asset_builder_main", MODULE_PATH)
assert SPEC and SPEC.loader
main = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(main)


class WorkerTest(unittest.TestCase):
    def test_process_claim_builds_one_symbol_interval(self):
        builder = Builder()
        claim = {"envelope": object(), "symbol": "NVDA", "interval": "5m"}
        result = main.process_claim(builder, claim)
        self.assertEqual(result, {"status": "saved"})
        self.assertEqual(builder.calls, [(claim["envelope"], "NVDA", "5m")])


class Builder:
    def __init__(self): self.calls = []
    def run_item(self, envelope, symbol, interval):
        self.calls.append((envelope, symbol, interval))
        return {"status": "saved"}


if __name__ == "__main__": unittest.main()
