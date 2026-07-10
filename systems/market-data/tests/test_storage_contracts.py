import importlib.util
import unittest
from pathlib import Path

from alfaka.common.redis_keys import RedisKeyBuilder
from alfaka.serving.redis_provider import RedisMarketDataProvider


ROOT = Path(__file__).resolve().parents[3]


def load_contract_checker():
    path = ROOT / "scripts/local/check-chart-data-contracts.py"
    spec = importlib.util.spec_from_file_location("check_chart_data_contracts", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class StorageContractsTest(unittest.TestCase):
    def test_repository_contracts_are_reconciled(self):
        checker = load_contract_checker()

        self.assertEqual(checker.collect_errors(), [])

    def test_retired_redis_key_builders_and_readers_are_absent(self):
        self.assertFalse(hasattr(RedisKeyBuilder, "volume_profile_live"))
        self.assertFalse(hasattr(RedisKeyBuilder, "order_flow_live"))
        self.assertFalse(hasattr(RedisMarketDataProvider, "volume_profile_bins"))
        self.assertFalse(hasattr(RedisMarketDataProvider, "_legacy_order_flow_live_bins"))

    def test_s3_lifecycle_expires_only_raw_prefixes(self):
        terraform = (ROOT / "infra/aws/terraform/main.tf").read_text(encoding="utf-8")
        self.assertIn('id     = "expire-chart-raw-v1"', terraform)
        self.assertIn('id     = "expire-chart-raw-v2"', terraform)
        self.assertEqual(terraform.count("days = var.s3_raw_retention_days"), 4)
        self.assertNotIn("expire-chart-final", terraform)


if __name__ == "__main__":
    unittest.main()
