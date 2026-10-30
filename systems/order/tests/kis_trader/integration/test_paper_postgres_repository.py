import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
import psycopg

from kis_trader.domain.commands import validate_order_request_payload
from kis_trader.paper.models import PaperCapacityError, PaperOrderError
from kis_trader.paper.postgres import PostgresPaperTradingRepository
from kis_trader.persistence.migrations import reset_public_schema, run_migrations


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DB_INTEGRATION") != "1" or not os.getenv("DATABASE_URL"),
    reason="set RUN_DB_INTEGRATION=1 and DATABASE_URL to run Postgres integration tests",
)


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


def reset_repository(*, max_active_order_symbols=100):
    conninfo = os.environ["DATABASE_URL"]
    reset_public_schema(conninfo)
    run_migrations(conninfo)
    return PostgresPaperTradingRepository(
        conninfo,
        max_active_order_symbols=max_active_order_symbols,
    )


def test_postgres_paper_order_reserves_and_price_improves_once():
    repository = reset_repository()
    created = repository.create_order(
        user_id="paper-user",
        idempotency_key_hash="buy-1",
        body_hash="body-1",
        request=order_request(),
    ).order

    pending = PostgresPaperTradingRepository(os.environ["DATABASE_URL"]).account_snapshot("paper-user")
    assert pending["account"]["reserved_cash"] == Decimal("1000")

    first = repository.match_quote(
        symbol="AAPL",
        bid_price=Decimal("98"),
        ask_price=Decimal("99"),
        quote_timestamp="2026-07-14T10:00:00Z",
        quote_event_id="quote-1",
    )
    replay = repository.match_quote(
        symbol="AAPL",
        bid_price=Decimal("98"),
        ask_price=Decimal("99"),
        quote_timestamp="2026-07-14T10:00:00Z",
        quote_event_id="quote-1",
    )

    assert first[0]["order_id"] == created["order_id"]
    assert first[0]["fill_price"] == Decimal("99")
    assert replay == []
    snapshot = repository.account_snapshot("paper-user")
    assert snapshot["account"]["cash_balance"] == Decimal("99010")
    assert snapshot["account"]["reserved_cash"] == Decimal("0")
    assert snapshot["positions"][0]["qty"] == Decimal("10")
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        execution = conn.execute(
            "SELECT execution_sequence, quantity, price FROM paper_executions WHERE order_id = %s",
            (created["order_id"],),
        ).fetchone()
        event_execution_id = conn.execute(
            "SELECT execution_id FROM paper_order_events WHERE order_id = %s AND event_type = 'order.filled'",
            (created["order_id"],),
        ).fetchone()[0]
        ledger_execution_id = conn.execute(
            "SELECT execution_id FROM paper_cash_ledger WHERE order_id = %s AND event_type = 'order.filled'",
            (created["order_id"],),
        ).fetchone()[0]
    assert execution == (1, Decimal("10"), Decimal("99"))
    assert event_execution_id == ledger_execution_id


def test_postgres_partial_fill_totals_match_order_cash_position_and_ledger():
    repository = reset_repository()
    created = repository.create_order(
        user_id="partial-user",
        idempotency_key_hash="partial-buy",
        body_hash="partial-body",
        request=order_request(),
    ).order

    first = repository.match_quote(
        symbol="AAPL", bid_price=Decimal("98.5"), ask_price=Decimal("99"),
        ask_size=Decimal("4"), quote_timestamp="2026-07-14T10:00:00Z",
        quote_event_id="partial-quote-1",
    )
    replay = repository.match_quote(
        symbol="AAPL", bid_price=Decimal("98.5"), ask_price=Decimal("99"),
        ask_size=Decimal("4"), quote_timestamp="2026-07-14T10:00:00Z",
        quote_event_id="partial-quote-1",
    )
    second = repository.match_quote(
        symbol="AAPL", bid_price=Decimal("97.5"), ask_price=Decimal("98"),
        ask_size=Decimal("10"), quote_timestamp="2026-07-14T10:00:01Z",
        quote_event_id="partial-quote-2",
    )

    assert first[0]["status"] == "partially_filled"
    assert replay == []
    assert second[0]["status"] == "filled"
    assert second[0]["filled_qty"] == Decimal("10")
    assert second[0]["fill_price"] == Decimal("98.4")
    snapshot = repository.account_snapshot("partial-user")
    assert snapshot["account"]["cash_balance"] == Decimal("99016")
    assert snapshot["account"]["reserved_cash"] == Decimal("0")
    assert snapshot["positions"][0]["qty"] == Decimal("10")
    assert snapshot["positions"][0]["average_price"] == Decimal("98.4")

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        executions = conn.execute(
            "SELECT execution_sequence, quantity, price FROM paper_executions WHERE order_id = %s ORDER BY execution_sequence",
            (created["order_id"],),
        ).fetchall()
        cash_delta = conn.execute(
            "SELECT sum(cash_delta) FROM paper_cash_ledger WHERE order_id = %s AND execution_id IS NOT NULL",
            (created["order_id"],),
        ).fetchone()[0]
        linked_events = conn.execute(
            "SELECT count(*) FROM paper_order_events WHERE order_id = %s AND execution_id IS NOT NULL",
            (created["order_id"],),
        ).fetchone()[0]
    assert executions == [(1, Decimal("4"), Decimal("99")), (2, Decimal("6"), Decimal("98"))]
    assert cash_delta == Decimal("-984")
    assert linked_events == 2


def test_postgres_paper_account_lock_prevents_overspending():
    repository = reset_repository()

    def submit(key):
        try:
            return repository.create_order(
                user_id="paper-user",
                idempotency_key_hash=key,
                body_hash=key,
                request=order_request(qty="600", price="100"),
            ).order["status"]
        except PaperOrderError:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, ["concurrent-1", "concurrent-2"]))

    assert sorted(results) == ["blocked", "pending"]
    snapshot = repository.account_snapshot("paper-user")
    assert snapshot["account"]["reserved_cash"] == Decimal("60000")


def test_postgres_global_symbol_capacity_is_serialized():
    repository = reset_repository(max_active_order_symbols=1)

    def submit(user_symbol):
        user_id, symbol = user_symbol
        try:
            repository.create_order(
                user_id=user_id,
                idempotency_key_hash=symbol,
                body_hash=symbol,
                request=order_request(symbol=symbol, qty="1"),
            )
            return "accepted"
        except PaperCapacityError:
            return "capacity"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, [("user-a", "AAPL"), ("user-b", "MSFT")]))

    assert sorted(results) == ["accepted", "capacity"]
    assert len(repository.active_order_symbols()) == 1


def test_postgres_memo_status_page_executions_and_cash_entries():
    repository = reset_repository()
    memo_order = repository.create_order(
        user_id="memo-user",
        idempotency_key_hash="memo-1",
        body_hash="memo-body-1",
        request=order_request(symbol="NVDA", qty="2", price="150"),
        memo="지지선 매수",
    ).order
    assert memo_order["memo"] == "지지선 매수"
    plain = repository.create_order(
        user_id="memo-user",
        idempotency_key_hash="memo-2",
        body_hash="memo-body-2",
        request=order_request(symbol="AAPL", qty="1", price="50"),
    ).order
    assert plain["memo"] is None
    repository.cancel_order("memo-user", plain["order_id"])
    repository.match_quote(
        symbol="NVDA",
        bid_price=Decimal("140"),
        ask_price=Decimal("142.3"),
        quote_timestamp="2026-09-22T14:00:00Z",
        quote_event_id="nvda-q1",
    )

    assert repository.get_order("memo-user", memo_order["order_id"])["memo"] == "지지선 매수"
    items, total = repository.page_orders("memo-user", statuses=frozenset({"filled"}))
    assert total == 1 and items[0]["memo"] == "지지선 매수"
    items, total = repository.page_orders("memo-user", statuses=frozenset({"cancelled", "rejected"}))
    assert total == 1 and items[0]["order_id"] == plain["order_id"]
    items, total = repository.page_orders("memo-user", offset=1, limit=1)
    assert total == 2 and len(items) == 1

    executions = repository.list_executions("memo-user")
    assert len(executions) == 1
    assert executions[0]["symbol"] == "NVDA"
    assert executions[0]["memo"] == "지지선 매수"
    assert executions[0]["price"] == Decimal("142.3")
    assert repository.list_cash_entries("memo-user", event_types=frozenset({"cash.dividend"})) == []


def test_postgres_review_executions_and_order_notes():
    repository = reset_repository()
    buy = repository.create_order(
        user_id="review-user",
        idempotency_key_hash="review-1",
        body_hash="review-body-1",
        request=order_request(symbol="NVDA", qty="2", price="150"),
        memo="주문 메모",
    ).order
    other = repository.create_order(
        user_id="other-user",
        idempotency_key_hash="review-2",
        body_hash="review-body-2",
        request=order_request(symbol="AAPL", qty="1", price="50"),
    ).order
    repository.match_quote(
        symbol="NVDA",
        bid_price=Decimal("140"),
        ask_price=Decimal("142"),
        quote_timestamp="2026-09-22T14:00:00Z",
        quote_event_id="review-q1",
    )

    [execution] = repository.list_review_executions("review-user")
    assert execution["order_id"] == buy["order_id"]
    assert execution["order_type"] == "limit"
    assert execution["memo"] == "주문 메모"
    assert execution["order_created_at"] is not None

    assert repository.upsert_order_note("review-user", other["order_id"], memo="x", habit=None, habit_source="auto") is None
    saved = repository.upsert_order_note("review-user", buy["order_id"], memo="복기", habit="chase", habit_source="user")
    assert saved["habit"] == "chase" and saved["habit_source"] == "user" and saved["memo"] == "복기"
    assert saved["app_user_id"] is not None
    saved = repository.upsert_order_note("review-user", buy["order_id"], memo=None, habit=None, habit_source="auto")
    assert saved["memo"] is None and saved["habit"] is None
    notes = repository.list_order_notes("review-user", [buy["order_id"], other["order_id"]])
    assert list(notes) == [buy["order_id"]]
    assert repository.list_order_notes("other-user", [buy["order_id"]]) == {}

    with pytest.raises(psycopg.errors.CheckViolation):
        repository.upsert_order_note("review-user", buy["order_id"], memo=None, habit="yolo", habit_source="user")
