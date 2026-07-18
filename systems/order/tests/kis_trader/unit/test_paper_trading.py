import unittest
from decimal import Decimal

from kis_trader.domain.commands import validate_order_request_payload
from kis_trader.paper.memory import InMemoryPaperTradingRepository
from kis_trader.paper.fixture import (
    DEMO_EQUITY,
    DEMO_FINAL_CASH,
    DEMO_FILLS,
    DEMO_HOLDINGS,
    DEMO_HOLDINGS_COST,
    DEMO_MARKET_VALUE,
    DEMO_REALIZED_PNL,
    DEMO_UNREALIZED_PNL,
    SEED_PROFILE,
)
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

    def test_diversified_seed_has_exact_fixture_invariants(self):
        repository = InMemoryPaperTradingRepository(seed_profile=SEED_PROFILE)

        snapshot = repository.ensure_account(self.user_id)
        positions = {position["symbol"]: position for position in snapshot["positions"]}

        self.assertEqual(snapshot["account"]["cash_balance"], DEMO_FINAL_CASH)
        self.assertEqual(snapshot["account"]["realized_pnl"], DEMO_REALIZED_PNL)
        self.assertEqual(snapshot["account"]["seed_profile"], SEED_PROFILE)
        self.assertEqual(len(positions), 7)
        self.assertEqual(len(repository.orders), len(DEMO_FILLS))
        self.assertEqual(len(repository.portfolio_history), len(DEMO_FILLS) * 2)
        self.assertEqual(repository.portfolio_history[-1]["payload"]["snapshotPhase"], "after")
        self.assertEqual(
            sum((position["qty"] * position["average_price"] for position in positions.values()), Decimal("0")),
            DEMO_HOLDINGS_COST,
        )
        self.assertEqual(
            sum((holding.quantity * holding.fallback_price for holding in DEMO_HOLDINGS), Decimal("0")),
            DEMO_MARKET_VALUE,
        )
        self.assertEqual(DEMO_MARKET_VALUE - DEMO_HOLDINGS_COST, DEMO_UNREALIZED_PNL)
        self.assertEqual(DEMO_FINAL_CASH + DEMO_MARKET_VALUE, DEMO_EQUITY)
        seeded_ledger = [row for row in repository.ledger if row["event_type"] == "order.filled"]
        self.assertEqual(len(seeded_ledger), len(DEMO_FILLS))
        self.assertEqual(seeded_ledger[-1]["cash_balance_after"], DEMO_FINAL_CASH)
        seeded_orders = sorted(repository.orders.values(), key=lambda row: row["filled_at"])
        self.assertEqual(
            [(row["symbol"], row["side"], row["qty"], row["fill_price"]) for row in seeded_orders],
            [(fill.symbol, fill.side, fill.quantity, fill.price) for fill in DEMO_FILLS],
        )
        for holding in DEMO_HOLDINGS:
            self.assertEqual(positions[holding.symbol]["qty"], holding.quantity)
            self.assertEqual(positions[holding.symbol]["average_price"], holding.average_price)

    def test_diversified_seed_is_idempotent_and_reset_suppresses_reseed(self):
        repository = InMemoryPaperTradingRepository(seed_profile=SEED_PROFILE)
        first = repository.ensure_account(self.user_id)
        second = repository.ensure_account(self.user_id)

        self.assertEqual(first, second)
        self.assertEqual(len(repository.orders), len(DEMO_FILLS))

        reset = repository.reset_account(self.user_id, Decimal("250000"))
        reopened = repository.ensure_account(self.user_id)
        self.assertEqual(reset["account"]["generation"], 2)
        self.assertEqual(reopened["account"]["cash_balance"], Decimal("250000"))
        self.assertIsNone(reopened["account"]["seed_profile"])
        self.assertIsNotNone(reopened["account"]["seed_suppressed_at"])
        self.assertEqual(reopened["positions"], [])

    def test_simulation_limit_uses_only_future_replay_sequence(self):
        created = self.repository.create_order(
            user_id=self.user_id,
            idempotency_key_hash="sim-limit",
            body_hash="sim-limit",
            request=order_request(symbol="MSFT", qty="1", price="101"),
            execution_mode="simulation",
            simulation_run_id="run-1",
            simulation_submitted_sequence=10,
        ).order

        same_sequence = self.repository.match_quote(
            symbol="MSFT", bid_price=Decimal("99"), ask_price=Decimal("100"),
            quote_timestamp="2026-07-14T15:00:00Z", quote_event_id="sim:10",
            execution_mode="simulation", simulation_run_id="run-1", quote_sequence=10,
        )
        future_sequence = self.repository.match_quote(
            symbol="MSFT", bid_price=Decimal("99"), ask_price=Decimal("100"),
            quote_timestamp="2026-07-14T15:00:01Z", quote_event_id="sim:11",
            execution_mode="simulation", simulation_run_id="run-1", quote_sequence=11,
        )

        self.assertEqual(same_sequence, [])
        self.assertEqual(future_sequence[0]["order_id"], created["order_id"])

    def test_replay_quote_never_fills_regular_paper_order_and_run_cancel_preserves_fills(self):
        regular = self.create(order_request(symbol="MSFT", qty="1", price="101"), key="paper", body="paper").order
        filled = self.repository.create_order(
            user_id=self.user_id, idempotency_key_hash="sim-market", body_hash="sim-market",
            request=order_request(symbol="GOOGL", qty="1", price="100"), execution_mode="simulation",
            simulation_run_id="run-1", simulation_submitted_sequence=10, order_type="market",
        ).order
        pending = self.repository.create_order(
            user_id=self.user_id, idempotency_key_hash="sim-resting", body_hash="sim-resting",
            request=order_request(symbol="XOM", qty="1", price="50"), execution_mode="simulation",
            simulation_run_id="run-1", simulation_submitted_sequence=10,
        ).order
        self.repository.match_quote(
            symbol="GOOGL", bid_price=Decimal("99"), ask_price=Decimal("100"),
            quote_timestamp=None, quote_event_id="sim:10", execution_mode="simulation",
            simulation_run_id="run-1", quote_sequence=10,
        )

        cancelled = self.repository.cancel_simulation_run("run-1")

        self.assertEqual(self.repository.get_order(self.user_id, regular["order_id"])["status"], "pending")
        self.assertEqual(self.repository.get_order(self.user_id, filled["order_id"])["status"], "filled")
        self.assertEqual(self.repository.get_order(self.user_id, pending["order_id"])["status"], "cancelled")
        self.assertEqual([item["order_id"] for item in cancelled], [pending["order_id"]])

    def test_simulation_sell_updates_the_seeded_shared_position(self):
        repository = InMemoryPaperTradingRepository(seed_profile=SEED_PROFILE)
        repository.ensure_account(self.user_id)
        created = repository.create_order(
            user_id=self.user_id, idempotency_key_hash="sim-sell", body_hash="sim-sell",
            request=order_request(symbol="GOOGL", side="sell", qty="1", price="180"),
            execution_mode="simulation", simulation_run_id="run-1",
            simulation_submitted_sequence=10, order_type="market",
        ).order

        repository.match_quote(
            symbol="GOOGL", bid_price=Decimal("180"), ask_price=Decimal("180.10"),
            quote_timestamp="2026-07-14T15:00:00Z", quote_event_id="sim:10",
            execution_mode="simulation", simulation_run_id="run-1", quote_sequence=10,
            virtual_timestamp="2026-07-14T15:00:00Z",
        )

        snapshot = repository.account_snapshot(self.user_id)
        googl = next(row for row in snapshot["positions"] if row["symbol"] == "GOOGL")
        self.assertEqual(repository.get_order(self.user_id, created["order_id"])["status"], "filled")
        self.assertEqual(googl["qty"], Decimal("59"))
        self.assertEqual(snapshot["account"]["cash_balance"], DEMO_FINAL_CASH + Decimal("180"))
        self.assertEqual(snapshot["account"]["realized_pnl"], DEMO_REALIZED_PNL + Decimal("7.60"))


if __name__ == "__main__":
    unittest.main()
