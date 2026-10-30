"""D21 주문 메모, D22 주문 내역 필터·페이지."""

import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
ORDER_SHARED = ROOT / "systems" / "order" / "shared"
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server"
for path in (str(MARKET_SHARED), str(ORDER_SHARED), str(BACKEND), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

try:
    from fastapi.testclient import TestClient

    from app.alerts.repository import InMemoryAlertRepository
    from app.main import create_app
    from kis_trader.paper.memory import InMemoryPaperTradingRepository

    TESTCLIENT_AVAILABLE = True
except Exception:
    TestClient = None
    TESTCLIENT_AVAILABLE = False


USER = "dev-auth-disabled"


def payload(**overrides):
    value = {
        "market": "overseas",
        "symbol": "NVDA",
        "side": "buy",
        "qty": "10",
        "price": "100",
        "exchange": "NASD",
        "order_division": "00",
    }
    value.update(overrides)
    return value


class PaperApiFixture:
    """메모 레인 테스트와 계좌 활동 테스트가 함께 쓰는 앱·저장소 준비."""

    def setUp(self):
        os.environ["AUTH_ENABLED"] = "false"
        os.environ["IDEMPOTENCY_HASH_SECRET"] = "paper-test-secret"
        self.repository = InMemoryPaperTradingRepository()
        self.app = create_app()
        self.app.state.paper_trading_repository = self.repository
        self.app.state.alert_repository = InMemoryAlertRepository()
        self.app.state.paper_symbol_validator = lambda symbol: {
            "symbol": symbol, "assetClass": "us_equity", "tradable": True, "status": "active",
        }
        self.app.state.paper_subscription_sync = lambda orders, positions: None
        self.app.state.paper_price_resolver = lambda _symbol: {"price": "105", "source": "test"}
        self.client = TestClient(self.app)
        self._keys = 0

    def submit(self, body=None, key=None):
        self._keys += 1
        return self.client.post(
            "/api/paper/orders",
            json=body or payload(),
            headers={"Idempotency-Key": key or f"key-{self._keys}"},
        )

    def fill(self, symbol, *, bid, ask, event_id, at="2026-09-22T14:00:00Z"):
        return self.repository.match_quote(
            symbol=symbol,
            bid_price=Decimal(bid),
            ask_price=Decimal(ask),
            quote_timestamp=at,
            quote_event_id=event_id,
        )



@unittest.skipUnless(TESTCLIENT_AVAILABLE, "FastAPI TestClient is not available")
class PaperOrderMemoAndListTest(PaperApiFixture, unittest.TestCase):
    # D21 ------------------------------------------------------------------
    def test_memo_round_trips_through_create_get_and_list(self):
        created = self.submit(payload(memo="  지지선 근처 분할 매수  "))

        self.assertEqual(created.status_code, 202)
        self.assertEqual(created.json()["memo"], "지지선 근처 분할 매수")
        order_id = created.json()["order_id"]
        self.assertEqual(self.client.get(f"/api/paper/orders/{order_id}").json()["memo"], "지지선 근처 분할 매수")
        listed = self.client.get("/api/paper/orders").json()["items"]
        self.assertEqual(listed[0]["memo"], "지지선 근처 분할 매수")

        self.fill("NVDA", bid="98", ask="99", event_id="q-1")
        filled = self.client.get(f"/api/paper/orders/{order_id}").json()
        self.assertEqual(filled["status"], "filled")
        self.assertEqual(filled["memo"], "지지선 근처 분할 매수")
        self.assertEqual(self.repository.list_executions(USER)[0]["memo"], "지지선 근처 분할 매수")

    def test_memo_is_optional_and_blank_becomes_null(self):
        self.assertIsNone(self.submit().json()["memo"])
        self.assertIsNone(self.submit(payload(memo="   ")).json()["memo"])
        self.assertIsNone(self.submit(payload(memo=None)).json()["memo"])

    def test_memo_length_is_limited_to_200_characters(self):
        exact = self.submit(payload(memo="가" * 200))
        too_long = self.submit(payload(memo="가" * 201))
        wrong_type = self.submit(payload(memo=123))

        self.assertEqual(exact.status_code, 202)
        self.assertEqual(len(exact.json()["memo"]), 200)
        self.assertEqual(too_long.status_code, 422)
        self.assertEqual(too_long.json()["detail"]["code"], "memo_too_long")
        self.assertEqual(too_long.json()["detail"]["maxLength"], 200)
        self.assertEqual(wrong_type.status_code, 422)
        self.assertEqual(wrong_type.json()["detail"]["code"], "invalid_memo")
        self.assertEqual(len(self.client.get("/api/paper/orders").json()["items"]), 1)

    # D22 ------------------------------------------------------------------
    def _seed_mixed_orders(self):
        # 대기 3건(1건은 부분 체결), 체결 2건, 취소 1건
        filled_ids = [self.submit(payload(symbol="AAPL", qty="1")).json()["order_id"] for _ in range(2)]
        self.fill("AAPL", bid="98", ask="99", event_id="aapl-fill")
        partial = self.submit(payload(symbol="MSFT", qty="10")).json()["order_id"]
        self.repository.match_quote(
            symbol="MSFT", bid_price=Decimal("98"), ask_price=Decimal("99"),
            quote_timestamp="2026-09-23T14:00:00Z", quote_event_id="msft-partial", ask_size=Decimal("4"),
        )
        pending = [self.submit(payload(symbol="NVDA", qty="1", price="50")).json()["order_id"] for _ in range(2)]
        cancelled = self.submit(payload(symbol="TSLA", qty="1", price="50")).json()["order_id"]
        self.client.post(f"/api/paper/orders/{cancelled}/cancel")
        return {"filled": filled_ids, "partial": partial, "pending": pending, "cancelled": cancelled}

    def test_status_filters_map_internal_statuses_to_four_buckets(self):
        ids = self._seed_mixed_orders()
        self.assertEqual(self.repository.get_order(USER, ids["partial"])["status"], "partially_filled")

        def bucket(name):
            body = self.client.get(f"/api/paper/orders?status={name}").json()
            return body["total"], {item["order_id"] for item in body["items"]}

        self.assertEqual(bucket("all")[0], 6)
        self.assertEqual(bucket("pending"), (3, {ids["partial"], *ids["pending"]}))
        self.assertEqual(bucket("filled"), (2, set(ids["filled"])))
        self.assertEqual(bucket("cancelled"), (1, {ids["cancelled"]}))

        invalid = self.client.get("/api/paper/orders?status=open")
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["detail"]["code"], "invalid_order_status")

    def test_pagination_returns_total_page_and_size_newest_first(self):
        created = [self.submit(payload(qty="1", price="10")).json()["order_id"] for _ in range(5)]

        first = self.client.get("/api/paper/orders?status=all&page=1&size=2").json()
        third = self.client.get("/api/paper/orders?status=all&page=3&size=2").json()
        beyond = self.client.get("/api/paper/orders?page=4&size=2").json()

        self.assertEqual((first["total"], first["page"], first["size"]), (5, 1, 2))
        self.assertEqual([item["order_id"] for item in first["items"]], created[::-1][:2])
        self.assertEqual([item["order_id"] for item in third["items"]], [created[0]])
        self.assertEqual(beyond["items"], [])
        self.assertEqual(beyond["total"], 5)
        self.assertEqual(self.client.get("/api/paper/orders").json()["size"], 20)
        self.assertEqual(self.client.get("/api/paper/orders?page=0").status_code, 422)

    def test_legacy_query_params_and_orders_key_still_work(self):
        ids = self._seed_mixed_orders()

        legacy = self.client.get("/api/paper/orders?limit=500").json()
        by_order_status = self.client.get("/api/paper/orders?order_status=cancelled").json()
        combined = self.client.get("/api/paper/orders?status=filled&order_status=cancelled").json()

        self.assertEqual(legacy["size"], 500)
        self.assertEqual(legacy["orders"], legacy["items"])
        self.assertEqual(len(legacy["orders"]), 6)
        self.assertEqual([item["order_id"] for item in by_order_status["orders"]], [ids["cancelled"]])
        self.assertEqual(combined["total"], 0)
        self.assertEqual(self.client.get("/api/paper/orders?order_status=open").status_code, 422)


if __name__ == "__main__":
    unittest.main()
