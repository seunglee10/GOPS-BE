import unittest
from decimal import Decimal

from kis_trader.domain.commands import validate_order_request_payload
from kis_trader.paper.memory import InMemoryPaperTradingRepository
from kis_trader.paper.matcher import match_quote_payload
from kis_trader.paper.models import PaperCapacityError, PaperIdempotencyConflictError, PaperOrderError, PaperOrderNotFoundError


def order_request(*, symbol="AAPL", side="buy", qty="10", price="100"):
    return validate_order_request_payload(
        {
            "market": "overseas",
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "price": price,
            "exchange": "NASD",
            "order_division": "00",
        },
        default_account_alias="paper-account",
    )


class PaperTradingRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.repository = InMemoryPaperTradingRepository()
        self.user_id = "user-1"

    def create(self, request=None, *, key="idem-1", body="body-1"):
        return self.repository.create_order(
            user_id=self.user_id,
            idempotency_key_hash=key,
            body_hash=body,
            request=request or order_request(),
        )

    def test_account_opens_with_default_cash(self):
        snapshot = self.repository.ensure_account(self.user_id)

        self.assertEqual(snapshot["account"]["cash_balance"], Decimal("100000.00"))
        self.assertEqual(snapshot["account"]["available_cash"], Decimal("100000.00"))

    def test_buy_reserves_limit_notional_and_fills_at_better_ask(self):
        created = self.create().order

        pending = self.repository.account_snapshot(self.user_id)
        self.assertEqual(pending["account"]["reserved_cash"], Decimal("1000"))
        self.assertEqual(created["status"], "pending")

        self.assertEqual(
            self.repository.match_quote(
                symbol="AAPL",
                bid_price=Decimal("98.99"),
                ask_price=Decimal("99"),
                quote_timestamp="2026-07-14T10:00:00Z",
                quote_event_id="quote-1",
            )[0]["fill_price"],
            Decimal("99"),
        )
        filled = self.repository.account_snapshot(self.user_id)
        self.assertEqual(filled["account"]["cash_balance"], Decimal("99010.00"))
        self.assertEqual(filled["account"]["reserved_cash"], Decimal("0"))
        self.assertEqual(filled["positions"][0]["qty"], Decimal("10"))
        self.assertEqual(filled["positions"][0]["average_price"], Decimal("99"))

    def test_sell_rejects_unowned_quantity_then_realizes_profit(self):
        with self.assertRaises(PaperOrderError):
            self.create(order_request(side="sell", qty="1"))

        self.create(order_request(qty="2", price="100"), key="buy", body="buy")
        self.repository.match_quote(
            symbol="AAPL",
            bid_price=Decimal("99"),
            ask_price=Decimal("99"),
            quote_timestamp=None,
            quote_event_id="quote-buy",
        )
        self.create(order_request(side="sell", qty="2", price="105"), key="sell", body="sell")
        self.repository.match_quote(
            symbol="AAPL",
            bid_price=Decimal("106"),
            ask_price=Decimal("106.01"),
            quote_timestamp=None,
            quote_event_id="quote-sell",
        )

        position = next(iter(self.repository.positions.values()))
        self.assertEqual(position["qty"], Decimal("0"))
        self.assertEqual(position["realized_pnl"], Decimal("14"))
        self.assertEqual(self.repository.account_snapshot(self.user_id)["account"]["cash_balance"], Decimal("100014.00"))

    def test_cancel_releases_reserved_cash(self):
        order = self.create().order

        cancelled = self.repository.cancel_order(self.user_id, order["order_id"])

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.repository.account_snapshot(self.user_id)["account"]["available_cash"], Decimal("100000.00"))

    def test_orders_are_isolated_by_user(self):
        order = self.create().order

        self.assertIsNone(self.repository.get_order("another-user", order["order_id"]))
        self.assertEqual(self.repository.list_orders("another-user"), [])
        with self.assertRaises(PaperOrderNotFoundError):
            self.repository.cancel_order("another-user", order["order_id"])

    def test_reset_archives_orders_and_starts_new_generation(self):
        order = self.create().order

        snapshot = self.repository.reset_account(self.user_id, Decimal("250000"))

        self.assertEqual(snapshot["account"]["generation"], 2)
        self.assertEqual(snapshot["account"]["cash_balance"], Decimal("250000"))
        self.assertEqual(self.repository.get_order(self.user_id, order["order_id"])["status"], "cancelled")
        self.assertEqual(len(self.repository.list_orders(self.user_id)), 0)
        self.assertEqual(len(self.repository.list_orders(self.user_id, include_previous=True)), 1)

    def test_idempotency_replays_and_conflicts(self):
        first = self.create().order
        replay = self.create().order

        self.assertEqual(first["order_id"], replay["order_id"])
        with self.assertRaises(PaperIdempotencyConflictError):
            self.create(body="different")

    def test_active_symbol_capacity_allows_more_orders_for_existing_symbol(self):
        repository = InMemoryPaperTradingRepository(max_active_order_symbols=1)
        repository.create_order(
            user_id=self.user_id,
            idempotency_key_hash="aapl-1",
            body_hash="aapl-1",
            request=order_request(symbol="AAPL", qty="1"),
        )
        repository.create_order(
            user_id=self.user_id,
            idempotency_key_hash="aapl-2",
            body_hash="aapl-2",
            request=order_request(symbol="AAPL", qty="1"),
        )

        with self.assertRaises(PaperCapacityError):
            repository.create_order(
                user_id=self.user_id,
                idempotency_key_hash="msft-1",
                body_hash="msft-1",
                request=order_request(symbol="MSFT", qty="1"),
            )

    def test_quote_matcher_accepts_extended_session_quote_without_filtering_it(self):
        self.create(order_request(price="100"))

        matched = match_quote_payload(self.repository, {
            "eventType": "QUOTE",
            "symbol": "AAPL",
            "bidPrice": 98.5,
            "askPrice": 99.5,
            "marketSession": "after",
            "timestamp": "2026-07-14T21:00:00Z",
            "sourceEventId": "after-quote-1",
        })

        self.assertEqual(matched[0]["status"], "filled")
        self.assertEqual(matched[0]["fill_price"], Decimal("99.5"))


if __name__ == "__main__":
    unittest.main()
