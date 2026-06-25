from __future__ import annotations

import unittest
from types import SimpleNamespace
from uuid import uuid4

import requests

from kis_trader.broker_adapter import KisBrokerAdapter
from kis_trader.client import KisApiError
from kis_trader.order_contract import OrderStatus, build_order_key


def message() -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_type": "order.submit.requested",
        "event_id": str(uuid4()),
        "request_id": str(uuid4()),
        "occurred_at": "2026-06-25T00:00:00.000Z",
        "producer": "test",
        "env": "demo",
        "account_alias": "demo-account",
        "payload": {
            "market": "overseas",
            "symbol": "AAPL",
            "side": "buy",
            "qty": "1",
            "price": "145.00",
            "exchange": "NASD",
            "order_division": "00",
        },
    }


class FakeRepo:
    def __init__(self, *, has_submission: bool = False) -> None:
        self.has_submission_value = has_submission
        self.began = 0
        self.records = []
        self.dlq = []

    def has_submission(self, request_id: str) -> bool:
        return self.has_submission_value

    def begin_submission(self, envelope) -> str:  # type: ignore[no-untyped-def]
        self.began += 1
        return "00000000-0000-0000-0000-000000000001"

    def record_submission_result(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.records.append(kwargs["record"])
        self.has_submission_value = True

    def record_dlq(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.dlq.append(kwargs)


class FakeAuth:
    def invalidate_access_token(self) -> None:
        pass


class FakeClient:
    def __init__(self, *, error: KisApiError | None = None) -> None:
        self.error = error
        self.auth = FakeAuth()
        self.order_calls = 0

    def preview_order(self, request) -> dict[str, object]:  # type: ignore[no-untyped-def]
        return {"body": {"CANO": "12345678", "PDNO": request.symbol}}

    def order(self, request) -> dict[str, object]:  # type: ignore[no-untyped-def]
        self.order_calls += 1
        if self.error is not None:
            raise self.error
        return {"rt_cd": "0", "output": {"ODNO": "KIS123"}}


def make_timeout_kis_error() -> KisApiError:
    try:
        raise requests.Timeout("timed out")
    except requests.Timeout as exc:
        try:
            raise KisApiError("KIS POST request failed: timed out") from exc
        except KisApiError as kis_exc:
            return kis_exc


class BrokerAdapterTests(unittest.TestCase):
    def test_duplicate_request_does_not_call_kis(self) -> None:
        repo = FakeRepo(has_submission=True)
        client = FakeClient()
        config = SimpleNamespace(
            kafka_dlq_topic="orders.dlq.v1",
            kafka_submit_results_topic="broker.submit-results.v1",
            database_url="unused",
        )
        adapter = KisBrokerAdapter(config=config, client=client, repository=repo)
        payload = message()

        result = adapter.process_message(
            payload,
            key=build_order_key("demo-account", "AAPL"),
            original_topic="orders.commands.v1",
        )

        self.assertTrue(result.skipped_external_submit)
        self.assertEqual(client.order_calls, 0)
        self.assertEqual(repo.began, 0)

    def test_timeout_records_unknown_and_does_not_retry_post(self) -> None:
        repo = FakeRepo()
        client = FakeClient(error=make_timeout_kis_error())
        config = SimpleNamespace(
            kafka_dlq_topic="orders.dlq.v1",
            kafka_submit_results_topic="broker.submit-results.v1",
            database_url="unused",
        )
        adapter = KisBrokerAdapter(config=config, client=client, repository=repo)
        payload = message()

        result = adapter.process_message(
            payload,
            key=build_order_key("demo-account", "AAPL"),
            original_topic="orders.commands.v1",
        )

        self.assertEqual(result.status, OrderStatus.SUBMIT_FAILED_UNKNOWN.value)
        self.assertEqual(client.order_calls, 1)
        self.assertEqual(repo.records[0].status, OrderStatus.SUBMIT_FAILED_UNKNOWN)
        self.assertEqual(repo.records[0].redacted_request["body"]["CANO"], "[REDACTED]")

    def test_invalid_message_goes_to_dlq(self) -> None:
        repo = FakeRepo()
        client = FakeClient()
        config = SimpleNamespace(
            kafka_dlq_topic="orders.dlq.v1",
            kafka_submit_results_topic="broker.submit-results.v1",
            database_url="unused",
        )
        adapter = KisBrokerAdapter(config=config, client=client, repository=repo)
        payload = message()
        payload["payload"]["market"] = "bad"  # type: ignore[index]

        result = adapter.process_message(
            payload,
            key=build_order_key("demo-account", "AAPL"),
            original_topic="orders.commands.v1",
        )

        self.assertEqual(result.status, "DLQ")
        self.assertEqual(client.order_calls, 0)
        self.assertEqual(len(repo.dlq), 1)

    def test_reprocessing_same_request_does_not_submit_more_than_once(self) -> None:
        repo = FakeRepo()
        client = FakeClient()
        config = SimpleNamespace(
            kafka_dlq_topic="orders.dlq.v1",
            kafka_submit_results_topic="broker.submit-results.v1",
            database_url="unused",
        )
        adapter = KisBrokerAdapter(config=config, client=client, repository=repo)
        payload = message()

        for _ in range(100):
            adapter.process_message(
                payload,
                key=build_order_key("demo-account", "AAPL"),
                original_topic="orders.commands.v1",
            )

        self.assertEqual(client.order_calls, 1)
        self.assertEqual(len(repo.records), 1)


if __name__ == "__main__":
    unittest.main()
