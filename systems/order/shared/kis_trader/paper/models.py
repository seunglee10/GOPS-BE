from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol

from kis_trader.domain.commands import OrderRequest


DEFAULT_STARTING_CASH = Decimal("100000.00")
MAX_ACTIVE_ORDER_SYMBOLS = 100
PENDING_STATUS = "pending"
PARTIALLY_FILLED_STATUS = "partially_filled"
FILLED_STATUS = "filled"
CANCELLED_STATUS = "cancelled"
ACTIVE_STATUSES = frozenset({PENDING_STATUS, PARTIALLY_FILLED_STATUS})
TERMINAL_STATUSES = frozenset({FILLED_STATUS, CANCELLED_STATUS, "rejected"})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def public_order(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload.pop("app_user_id", None)
    payload.pop("instrument_id", None)
    payload["price"] = payload.get("limit_price", payload.get("price"))
    payload["execution_mode"] = payload.get("execution_mode") or "paper"
    if payload["execution_mode"] == "simulation":
        payload["simulation"] = True
        payload["runId"] = payload.get("simulation_run_id")
        payload["virtualSubmittedAt"] = payload.get("virtual_submitted_at")
        payload["virtualFilledAt"] = payload.get("virtual_filled_at")
    return payload


class PaperOrderError(RuntimeError):
    pass


class PaperOrderNotFoundError(PaperOrderError):
    pass


class PaperIdempotencyConflictError(PaperOrderError):
    pass


class PaperCapacityError(PaperOrderError):
    pass


@dataclass(frozen=True)
class PaperOrderCreationResult:
    order: dict[str, Any]
    idempotent_replay: bool


class PaperTradingRepository(Protocol):
    def ensure_account(self, user_id: str) -> dict[str, Any]: ...

    def account_snapshot(self, user_id: str) -> dict[str, Any]: ...

    def create_order(
        self,
        *,
        user_id: str,
        idempotency_key_hash: str,
        body_hash: str,
        request: OrderRequest,
        execution_mode: str = "paper",
        simulation_run_id: str | None = None,
        simulation_submitted_sequence: int | None = None,
        virtual_submitted_at: str | None = None,
        order_type: str = "limit",
    ) -> PaperOrderCreationResult: ...

    def pretrade(self, user_id: str, request: OrderRequest) -> dict[str, Any]: ...

    def get_order(self, user_id: str, order_id: str) -> dict[str, Any] | None: ...

    def list_orders(
        self,
        user_id: str,
        *,
        status: str | None = None,
        include_previous: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    def list_order_events(self, user_id: str, order_id: str) -> list[dict[str, Any]]: ...

    def cancel_order(self, user_id: str, order_id: str) -> dict[str, Any]: ...

    def reset_account(self, user_id: str, starting_cash: Decimal) -> dict[str, Any]: ...

    def match_quote(
        self,
        *,
        symbol: str,
        bid_price: Decimal | None,
        ask_price: Decimal | None,
        quote_timestamp: str | None,
        quote_event_id: str | None,
        bid_size: Decimal | None = None,
        ask_size: Decimal | None = None,
        execution_mode: str = "paper",
        simulation_run_id: str | None = None,
        quote_sequence: int | None = None,
        virtual_timestamp: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def cancel_simulation_run(self, run_id: str, *, reason: str = "simulation_run_ended") -> list[dict[str, Any]]: ...

    def active_order_symbols(self) -> list[str]: ...

    def active_position_symbols(self) -> list[str]: ...
