"""D20 계좌 최근 활동."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_paper_order_memo_list import TESTCLIENT_AVAILABLE, USER, PaperApiFixture, payload  # noqa: E402

if TESTCLIENT_AVAILABLE:
    from app.services.account_activity import build_account_activity


@unittest.skipUnless(TESTCLIENT_AVAILABLE, "FastAPI TestClient is not available")
class AccountActivityRouteTest(PaperApiFixture, unittest.TestCase):
    # D20 ------------------------------------------------------------------
    def test_activity_merges_fills_rules_and_cash_entries_newest_first(self):
        created = self.submit(payload(symbol="NVDA", qty="10", price="150")).json()
        self.fill("NVDA", bid="142", ask="142.30", event_id="nvda-1", at="2026-09-22T14:00:00Z")
        execution_at = self.repository.list_executions(USER)[0]["executed_at"]

        alerts = self.app.state.alert_repository
        alerts.create_notification(
            user_sub=USER, alert_id=7, event_id="evt-rule", notification_type="alert.price_cross",
            payload={
                "alertId": 7, "symbol": "NVDA", "direction": "below", "targetPrice": 137.7,
                "triggeredAt": (execution_at + timedelta(hours=1)).isoformat(),
            },
        )
        alerts.create_notification(
            user_sub=USER, alert_id=None, event_id="evt-system", notification_type="system.market_open",
            payload={"kind": "market_open"},
        )
        account, run = self.repository._current(USER)
        self.repository._append_ledger(
            USER, account["current_generation"], run, "cash.dividend",
            cash_delta=Decimal("9.96"), payload={"symbol": "MSFT"},
        )
        self.repository.ledger[-1]["created_at"] = execution_at + timedelta(hours=2)
        self.repository._append_ledger(
            USER, account["current_generation"], run, "cash.deposit", cash_delta=Decimal("1000"),
        )
        self.repository.ledger[-1]["created_at"] = execution_at - timedelta(hours=1)

        response = self.client.get("/api/account/activity?limit=20")

        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertEqual([item["kind"] for item in items], ["dividend", "rule", "fill", "deposit"])
        dividend, rule, fill, deposit = items
        self.assertEqual(fill["text"], "엔비디아 10주 매수 체결 $142.30")
        self.assertEqual(fill["nameKo"], "엔비디아")
        self.assertEqual(fill["symbol"], "NVDA")
        self.assertEqual(fill["ref"]["orderId"], created["order_id"])
        self.assertTrue(fill["at"].endswith("Z"))
        self.assertEqual(rule["text"], "엔비디아 137.70 아래 알림이 울렸어요")
        self.assertEqual(rule["ref"]["alertId"], 7)
        self.assertEqual(dividend["text"], "마이크로소프트 배당금 입금 +$9.96")
        self.assertEqual(dividend["symbol"], "MSFT")
        self.assertEqual(deposit["text"], "현금 입금 +$1,000.00")
        self.assertIsNone(deposit["symbol"])
        self.assertEqual(set(fill), {"at", "kind", "symbol", "nameKo", "text", "ref"})

        limited = self.client.get("/api/account/activity?limit=2").json()["items"]
        self.assertEqual([item["kind"] for item in limited], ["dividend", "rule"])
        self.assertEqual(self.client.get("/api/account/activity?limit=0").status_code, 422)

    def test_activity_ignores_reservation_ledger_rows_and_is_empty_for_new_account(self):
        self.assertEqual(self.client.get("/api/account/activity").json(), {"items": []})
        self.submit()
        self.assertEqual(self.client.get("/api/account/activity").json(), {"items": []})




@unittest.skipUnless(TESTCLIENT_AVAILABLE, "FastAPI TestClient is not available")
class AccountActivityBuilderTest(unittest.TestCase):
    def test_sell_fill_and_rule_phrases(self):
        at = datetime(2026, 9, 26, 15, tzinfo=timezone.utc)
        items = build_account_activity(
            executions=[{
                "execution_id": "e1", "order_id": "o1", "symbol": "TSLA", "side": "sell",
                "quantity": Decimal("5"), "price": Decimal("262.1"), "executed_at": at,
            }],
            notifications=[
                {"id": 1, "alert_id": 3, "type": "alert.spike", "created_at": (at + timedelta(days=3)).isoformat(),
                 "payload": {"symbol": "TSLA", "direction": "below", "thresholdPct": 5, "windowMin": 60}},
                {"id": 2, "alert_id": 4, "type": "alert.rsi_threshold", "created_at": (at + timedelta(days=1)).isoformat(),
                 "payload": {"symbol": "ZZZZ", "direction": "above", "threshold": 70}},
                {"id": 3, "alert_id": None, "type": "recommendation.stock_buy", "created_at": at.isoformat(),
                 "payload": {"symbol": "TSLA"}},
            ],
            cash_entries=[],
            limit=10,
        )
        self.assertEqual([item["text"] for item in items], [
            "테슬라 60분 -5% 알림이 울렸어요",
            "ZZZZ RSI 70 위 알림이 울렸어요",
            "테슬라 5주 매도 체결 $262.10",
        ])
        self.assertIsNone(items[1]["nameKo"])



if __name__ == "__main__":
    unittest.main()
