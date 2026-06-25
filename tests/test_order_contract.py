from __future__ import annotations

import unittest
from uuid import uuid4

from kis_trader.order_contract import (
    OrderContractError,
    build_order_key,
    validate_order_command_message,
)


def sample_message(*, market: str = "overseas", symbol: str = "AAPL") -> dict[str, object]:
    payload: dict[str, object] = {
        "market": market,
        "symbol": symbol,
        "side": "buy",
        "qty": "1",
        "price": "145.00",
        "exchange": "NASD" if market == "overseas" else "KRX",
        "order_division": "00",
    }
    if market == "domestic":
        payload.update({"sell_type": "", "condition_price": ""})
    return {
        "schema_version": 1,
        "event_type": "order.submit.requested",
        "event_id": str(uuid4()),
        "request_id": str(uuid4()),
        "occurred_at": "2026-06-25T00:00:00.000Z",
        "producer": "test",
        "env": "demo",
        "account_alias": "demo-account",
        "payload": payload,
    }


class OrderContractTests(unittest.TestCase):
    def test_accepts_overseas_command(self) -> None:
        message = sample_message()
        envelope = validate_order_command_message(
            message,
            key=build_order_key("demo-account", "AAPL"),
        )

        self.assertEqual(envelope.command.market, "overseas")
        self.assertEqual(envelope.kafka_key, "demo-account:AAPL")

    def test_accepts_domestic_command(self) -> None:
        message = sample_message(market="domestic", symbol="005930")
        envelope = validate_order_command_message(
            message,
            key=build_order_key("demo-account", "005930"),
        )

        self.assertEqual(envelope.command.market, "domestic")
        self.assertEqual(envelope.command.exchange, "KRX")

    def test_rejects_bad_market(self) -> None:
        message = sample_message()
        message["payload"]["market"] = "crypto"  # type: ignore[index]

        with self.assertRaises(OrderContractError):
            validate_order_command_message(message)

    def test_rejects_non_numeric_quantity(self) -> None:
        message = sample_message()
        message["payload"]["qty"] = "one"  # type: ignore[index]

        with self.assertRaises(OrderContractError):
            validate_order_command_message(message)

    def test_rejects_sensitive_fields(self) -> None:
        message = sample_message()
        message["payload"]["CANO"] = "12345678"  # type: ignore[index]

        with self.assertRaises(OrderContractError):
            validate_order_command_message(message)

    def test_rejects_wrong_kafka_key(self) -> None:
        message = sample_message()

        with self.assertRaises(OrderContractError):
            validate_order_command_message(message, key="demo-account:MSFT")


if __name__ == "__main__":
    unittest.main()
