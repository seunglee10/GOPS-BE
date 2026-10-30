"""D27 투자 복기 월간 리포트와 복기 메모·습관."""

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_paper_order_memo_list import TESTCLIENT_AVAILABLE, USER, PaperApiFixture, payload  # noqa: E402

if TESTCLIENT_AVAILABLE:
    from app.review.service import build_monthly_review, classify_habit, replay_orders
    from kis_trader.domain.commands import validate_order_request_payload


def d(text):
    return date.fromisoformat(text)


def utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class FakeReviewMarket:
    def __init__(self, closes=None, news=None):
        self.closes = closes or {}
        self.news = news or {}
        self.news_calls = []

    def daily_closes(self, symbol, start, end):
        return sorted((day, close) for day, close in self.closes.get(symbol, {}).items() if start <= day <= end)

    def news_published_times(self, symbol, until, window):
        self.news_calls.append(symbol)
        return [at for at in self.news.get(symbol, []) if at <= until]


CLOSES = {
    "NVDA": {
        d("2026-09-01"): 100, d("2026-09-02"): 101, d("2026-09-04"): 100, d("2026-09-08"): 95,
        d("2026-09-15"): 118, d("2026-09-16"): 125, d("2026-09-17"): 130, d("2026-09-18"): 122,
        d("2026-09-21"): 121, d("2026-09-22"): 119, d("2026-10-29"): 110,
    },
    "AVGO": {d("2026-09-02"): 180, d("2026-09-03"): 190, d("2026-10-29"): 200},
    "MSFT": {
        d("2026-08-20"): 400, d("2026-09-09"): 392, d("2026-09-10"): 395, d("2026-09-11"): 405,
        d("2026-09-14"): 400, d("2026-09-15"): 398, d("2026-09-16"): 401, d("2026-09-17"): 420,
        d("2026-10-29"): 410,
    },
    "TSLA": {d("2026-09-21"): 248, d("2026-09-22"): 251, d("2026-10-29"): 260},
    "^GSPC": {d("2026-07-31"): 4900, d("2026-08-31"): 5000, d("2026-09-30"): 5100},
}


@unittest.skipUnless(TESTCLIENT_AVAILABLE, "FastAPI TestClient is not available")
class ReviewRouteTest(PaperApiFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.market = FakeReviewMarket(
            closes=CLOSES,
            news={"TSLA": [utc("2026-09-22T13:30:00Z")], "NVDA": [utc("2026-09-15T10:00:00Z")]},
        )
        self.app.state.review_market_data = self.market
        self.app.state.review_now_provider = lambda: utc("2026-10-30T15:00:00Z")

    def trade(self, symbol, side, qty, price, at, *, market_order=False, ordered_at=None, memo=None):
        body = payload(symbol=symbol, side=side, qty=str(qty), price=str(price))
        if memo:
            body["memo"] = memo
        created = self.submit(body)
        self.assertEqual(created.status_code, 202, created.text)
        order_id = created.json()["order_id"]
        row = self.repository.orders[order_id]
        row["created_at"] = utc(ordered_at or at) - (timedelta(0) if ordered_at else timedelta(minutes=1))
        if market_order:
            row["order_type"] = "market"
        price = Decimal(str(price))
        self.fill(symbol, bid=str(price), ask=str(price), event_id=f"{order_id}-fill", at=at)
        self.assertEqual(self.repository.orders[order_id]["status"], "filled")
        return order_id

    def seed_september(self):
        ids = {}
        ids["msft_buy"] = self.trade("MSFT", "buy", 4, 400, "2026-08-20T15:00:00Z")
        ids["nvda_plan"] = self.trade("NVDA", "buy", 10, 100, "2026-09-02T14:00:00Z", memo="20일선 지지")
        ids["avgo_chase"] = self.trade("AVGO", "buy", 5, 190, "2026-09-03T15:00:00Z", memo="급등해서 따라 샀다")
        ids["nvda_avg"] = self.trade("NVDA", "buy", 10, 90, "2026-09-08T15:00:00Z")
        ids["msft_rush"] = self.trade(
            "MSFT", "sell", 2, 390, "2026-09-09T13:35:00Z", market_order=True, ordered_at="2026-09-09T13:34:00Z",
        )
        ids["nvda_exit"] = self.trade("NVDA", "sell", 10, 120, "2026-09-15T15:00:00Z")
        ids["tsla_news"] = self.trade("TSLA", "buy", 3, 250, "2026-09-22T14:00:00Z", ordered_at="2026-09-22T14:00:00Z")
        return ids

    def test_monthly_report_kpis_habits_and_trades(self):
        ids = self.seed_september()

        response = self.client.get("/api/review/monthly?month=2026-09")

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["month"], "2026-09")
        trades = {row["orderId"]: row for row in body["trades"]}
        self.assertEqual(len(trades), 6)
        self.assertNotIn(ids["msft_buy"], trades)
        habits = {key: trades[ids[key]]["habit"] for key in ids if key != "msft_buy"}
        self.assertEqual(habits, {
            "nvda_plan": "plan", "avgo_chase": "chase", "nvda_avg": "avg",
            "msft_rush": "rush", "nvda_exit": "exit", "tsla_news": "news",
        })

        exit_trade = trades[ids["nvda_exit"]]
        self.assertEqual(exit_trade["side"], "sell")
        self.assertEqual(exit_trade["avgPriceAtSell"], 95.0)
        self.assertEqual(exit_trade["pnl"], 250.0)
        self.assertEqual(exit_trade["returnPercent"], 26.32)
        self.assertEqual(exit_trade["priceAfter"], {"d1": 125.0, "d5": 119.0})
        self.assertEqual(exit_trade["missedProfit"], 100.0)

        rush = trades[ids["msft_rush"]]
        self.assertEqual((rush["avgPriceAtSell"], rush["pnl"], rush["returnPercent"]), (400.0, -20.0, -2.5))
        self.assertEqual(rush["missedProfit"], 30.0)

        plan = trades[ids["nvda_plan"]]
        self.assertIsNone(plan["avgPriceAtSell"])
        self.assertIsNone(plan["pnl"])
        self.assertIsNone(plan["returnPercent"])
        self.assertIsNone(plan["missedProfit"])
        self.assertEqual(plan["markPrice"], 110.0)
        self.assertEqual(plan["evalPnl"], 100.0)
        self.assertEqual(plan["memo"], "20일선 지지")
        self.assertEqual(plan["priceAfter"]["d1"], 100.0)
        self.assertEqual(plan["nameKo"], "엔비디아")
        self.assertTrue(plan["at"].endswith("Z"))

        kpi = body["kpi"]
        self.assertEqual(kpi["tradeCount"], 6)
        self.assertEqual(kpi["realizedPnl"], 230.0)
        self.assertEqual(kpi["evalPnl"], 610.0)
        self.assertEqual(kpi["returnPercent"], 0.61)
        self.assertEqual(kpi["winRate"], 83.33)
        self.assertEqual(kpi["missedProfit"], 130.0)
        self.assertEqual(kpi["avgWin"], 126.0)
        self.assertEqual(kpi["avgLoss"], 20.0)
        self.assertEqual(kpi["payoffRatio"], 6.3)
        self.assertEqual((kpi["mistakePnl"], kpi["mistakeCount"]), (250.0, 2))
        self.assertNotIn("checklist", str(body).lower())

        prev = body["prevMonth"]
        self.assertEqual(prev["tradeCount"], 1)
        self.assertEqual(prev["realizedPnl"], 0.0)
        self.assertEqual(prev["evalPnl"], 40.0)
        self.assertEqual(prev["winRate"], 100.0)

        self.assertEqual(body["benchmark"], {"spxReturnPercent": 2.0, "prevSpxReturnPercent": 2.04})

        by_habit = {row["habit"]: row for row in body["byHabit"]}
        self.assertEqual([row["habit"] for row in body["byHabit"]], ["plan", "exit", "news", "rush", "chase", "avg"])
        self.assertEqual(by_habit["exit"]["pnl"], 250.0)
        self.assertEqual(by_habit["rush"]["missedProfit"], 30.0)
        self.assertEqual(by_habit["avg"]["count"], 1)
        self.assertTrue(all(row["labelKo"] for row in body["byHabit"]))

        by_symbol = body["bySymbol"]
        self.assertEqual(by_symbol[0], {"symbol": "NVDA", "nameKo": "엔비디아", "pnl": 550.0, "count": 3})
        self.assertEqual(by_symbol[-1]["symbol"], "MSFT")

    def test_user_override_wins_and_review_memo_replaces_order_memo(self):
        ids = self.seed_september()

        saved = self.client.put(
            f"/api/review/notes/{ids['avgo_chase']}", json={"memo": "  다시 보니 계획 매수  ", "habit": "plan"},
        )

        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["memo"], "다시 보니 계획 매수")
        self.assertEqual(saved.json()["habit"], "plan")
        self.assertEqual(saved.json()["habitSource"], "user")
        trade = {row["orderId"]: row for row in self.client.get("/api/review/monthly?month=2026-09").json()["trades"]}[ids["avgo_chase"]]
        self.assertEqual((trade["habit"], trade["habitAuto"], trade["habitSource"]), ("plan", "chase", "user"))
        self.assertEqual(trade["memo"], "다시 보니 계획 매수")
        self.assertEqual(trade["orderMemo"], "급등해서 따라 샀다")

        # memo 만 고치면 습관 값은 그대로, habit=null 이면 서버 분류로 돌아간다.
        self.assertEqual(self.client.put(f"/api/review/notes/{ids['avgo_chase']}", json={"memo": "메모만"}).json()["habit"], "plan")
        cleared = self.client.put(f"/api/review/notes/{ids['avgo_chase']}", json={"habit": None, "memo": None}).json()
        self.assertEqual((cleared["habit"], cleared["habitSource"], cleared["memo"]), (None, "auto", None))
        trade = {row["orderId"]: row for row in self.client.get("/api/review/monthly?month=2026-09").json()["trades"]}[ids["avgo_chase"]]
        self.assertEqual((trade["habit"], trade["habitSource"], trade["memo"]), ("chase", "auto", "급등해서 따라 샀다"))

    def test_note_validation_and_ownership(self):
        order_id = self.trade("NVDA", "buy", 1, 100, "2026-09-02T14:00:00Z")
        self.assertEqual(self.client.put(f"/api/review/notes/{order_id}", json={"habit": "yolo"}).json()["detail"]["code"], "invalid_habit")
        too_long = self.client.put(f"/api/review/notes/{order_id}", json={"memo": "가" * 201})
        self.assertEqual((too_long.status_code, too_long.json()["detail"]["code"]), (422, "memo_too_long"))
        self.assertEqual(self.client.put(f"/api/review/notes/{order_id}", json={"memo": 3}).json()["detail"]["code"], "invalid_memo")

        request = validate_order_request_payload(payload(symbol="AAPL", qty="1"), default_account_alias="paper-account")
        other = self.repository.create_order(
            user_id="someone-else", idempotency_key_hash="other-1", body_hash="b", request=request,
        ).order
        forbidden = self.client.put(f"/api/review/notes/{other['order_id']}", json={"memo": "남의 주문"})
        self.assertEqual((forbidden.status_code, forbidden.json()["detail"]["code"]), (404, "order_not_found"))
        self.assertEqual(self.client.put("/api/review/notes/paper_ord_missing", json={}).status_code, 404)
        self.assertEqual(self.repository.list_order_notes("someone-else", [other["order_id"]]), {})

    def test_month_validation_default_and_empty_account(self):
        self.assertEqual(self.client.get("/api/review/monthly?month=2026-13").json()["detail"]["code"], "invalid_month")
        self.assertEqual(self.client.get("/api/review/monthly?month=09-2026").status_code, 422)
        body = self.client.get("/api/review/monthly").json()
        self.assertEqual(body["month"], "2026-10")
        self.assertEqual(body["trades"], [])
        self.assertEqual(body["kpi"]["tradeCount"], 0)
        self.assertIsNone(body["kpi"]["winRate"])
        self.assertEqual(body["kpi"]["missedProfit"], 0.0)
        self.assertEqual(len(body["byHabit"]), 6)


@unittest.skipUnless(TESTCLIENT_AVAILABLE, "FastAPI TestClient is not available")
class ReviewBuilderTest(unittest.TestCase):
    def execution(self, order_id, symbol, side, qty, price, at, **extra):
        return {
            "execution_id": f"{order_id}:{at}", "order_id": order_id, "symbol": symbol, "side": side,
            "quantity": Decimal(str(qty)), "price": Decimal(str(price)), "executed_at": utc(at), **extra,
        }

    def test_average_cost_replay_matches_paper_positions_with_partial_fills(self):
        orders = replay_orders([
            self.execution("b1", "AAPL", "buy", 10, 100, "2026-09-01T15:00:00Z"),
            self.execution("b2", "AAPL", "buy", 5, 130, "2026-09-02T15:00:00Z"),
            self.execution("b2", "AAPL", "buy", 5, 130, "2026-09-02T15:05:00Z"),
            self.execution("s1", "AAPL", "sell", 8, 120, "2026-09-03T15:00:00Z"),
            self.execution("s1", "AAPL", "sell", 2, 125, "2026-09-03T15:10:00Z"),
            self.execution("s2", "AAPL", "sell", 10, 110, "2026-09-04T15:00:00Z"),
        ])
        by_id = {order.order_id: order for order in orders}
        self.assertEqual(by_id["b2"].qty, Decimal("10"))
        self.assertEqual(by_id["b2"].held_qty_before, Decimal("10"))
        # 평균 115 에서 8주 120, 2주 125 판매
        self.assertEqual(by_id["s1"].avg_cost, Decimal("115"))
        self.assertEqual(by_id["s1"].realized, Decimal("60"))
        self.assertEqual(by_id["s1"].price, Decimal("121"))
        self.assertEqual(by_id["s2"].avg_cost, Decimal("115"))
        self.assertEqual(by_id["s2"].realized, Decimal("-50"))

    def test_sell_without_known_cost_has_no_pnl(self):
        [order] = replay_orders([self.execution("s", "XOM", "sell", 5, 100, "2026-09-03T15:00:00Z")])
        self.assertIsNone(order.realized)
        report = build_monthly_review(
            month_start=d("2026-09-01"), executions=[self.execution("s", "XOM", "sell", 5, 100, "2026-09-03T15:00:00Z")],
            notes={}, market=FakeReviewMarket(), starting_cash=None, now=utc("2026-10-01T00:00:00Z"),
        )
        trade = report["trades"][0]
        self.assertEqual((trade["pnl"], trade["avgPriceAtSell"], trade["habit"]), (None, None, "plan"))
        self.assertIsNone(report["kpi"]["returnPercent"])
        self.assertIsNone(report["benchmark"]["spxReturnPercent"])

    def test_habit_rules_edges(self):
        [buy] = replay_orders([self.execution("b", "AAPL", "buy", 1, 100, "2026-09-02T13:45:00Z", order_type="market")])
        # 장 시작 15분 뒤 시장가는 rush 아님, +3% 정확히는 chase
        self.assertEqual(classify_habit(buy, pnl=None, day_change_percent=3.0, news_times=[]), "chase")
        self.assertEqual(classify_habit(buy, pnl=None, day_change_percent=2.99, news_times=[]), "plan")
        self.assertEqual(
            classify_habit(buy, pnl=None, day_change_percent=None, news_times=[utc("2026-09-02T12:44:00Z")]), "plan",
        )
        self.assertEqual(
            classify_habit(buy, pnl=None, day_change_percent=None, news_times=[utc("2026-09-02T12:46:00Z")]), "news",
        )
        [weekend] = replay_orders([self.execution("w", "AAPL", "buy", 1, 100, "2026-09-05T13:35:00Z", order_type="market")])
        self.assertEqual(classify_habit(weekend, pnl=None, day_change_percent=None, news_times=[]), "plan")
        orders = replay_orders([
            self.execution("b", "AAPL", "buy", 1, 100, "2026-09-02T15:00:00Z"),
            self.execution("s", "AAPL", "sell", 1, 99, "2026-09-03T15:00:00Z"),
        ])
        self.assertEqual(classify_habit(orders[1], pnl=-1.0, day_change_percent=None, news_times=[]), "plan")
        self.assertEqual(classify_habit(orders[1], pnl=0.5, day_change_percent=9.0, news_times=[]), "exit")

    def test_benchmark_falls_back_to_spy(self):
        market = FakeReviewMarket(closes={"SPY": {d("2026-08-31"): 500, d("2026-09-15"): 510}})
        report = build_monthly_review(
            month_start=d("2026-09-01"), executions=[], notes={}, market=market,
            starting_cash=Decimal("100000"), now=utc("2026-09-16T00:00:00Z"),
        )
        self.assertEqual(report["benchmark"]["spxReturnPercent"], 2.0)


if __name__ == "__main__":
    unittest.main()
