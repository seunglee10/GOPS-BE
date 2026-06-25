from __future__ import annotations

import unittest
from pathlib import Path

from kis_trader.client import KisApiError
from kis_trader.config import KisConfig
from kis_trader.fake_kis import FakeKisClient
from kis_trader.models import OverseasOrderRequest


def config() -> KisConfig:
    return KisConfig(
        env="demo",
        app_key="",
        app_secret="",
        account_no="",
        product_code="01",
        hts_id="",
        base_url="https://example.invalid",
        token_cache_path=Path("/tmp/fake-token"),
        user_agent="test",
        contact_phone="",
        mgco_aptm_odno="",
        order_server_code="0",
        default_exchange="NASD",
        default_currency="USD",
        timeout_seconds=1,
        database_url="postgresql://example",
        kafka_bootstrap_servers="localhost:29092",
        kafka_order_commands_topic="orders.commands.v1",
        kafka_submit_results_topic="broker.submit-results.v1",
        kafka_order_events_topic="broker.order-events.v1",
        kafka_reconciled_topic="orders.reconciled.v1",
        kafka_dlq_topic="orders.dlq.v1",
        kafka_broker_adapter_group_id="kis-broker-adapter",
        kafka_account_alias="demo-account",
    )


class FakeKisTests(unittest.TestCase):
    def test_success_response_contains_fake_order_id(self) -> None:
        client = FakeKisClient(config(), mode="success")
        request = OverseasOrderRequest.from_strings(
            symbol="AAPL",
            side="buy",
            qty="1",
            price="145.00",
            exchange="NASD",
        )

        response = client.order(request)

        self.assertEqual(response["output"]["ODNO"], "FAKE-AAPL")  # type: ignore[index]

    def test_reject_mode_raises_kis_error(self) -> None:
        client = FakeKisClient(config(), mode="reject")
        request = OverseasOrderRequest.from_strings(
            symbol="AAPL",
            side="buy",
            qty="1",
            price="145.00",
            exchange="NASD",
        )

        with self.assertRaises(KisApiError):
            client.order(request)


if __name__ == "__main__":
    unittest.main()
