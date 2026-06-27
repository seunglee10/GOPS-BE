"""Bounded order/fill reconciliation logic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from kis_trader.domain.status import OrderStatus, RECONCILIATION_TARGET_STATUSES
from kis_trader.persistence.repository import OrderRepository


@dataclass(frozen=True)
class ReconciliationResult:
    order_id: str
    status: str
    reason: str | None = None


@dataclass(frozen=True)
class ReconciliationAlert:
    alert_type: str
    reason: str
    order_id: str | None = None
    broker_order_id: str | None = None
    row: dict[str, Any] | None = None


@dataclass(frozen=True)
class ReconciliationReport:
    results: list[ReconciliationResult]
    alerts: list[ReconciliationAlert]


def reconciliation_targets(repository: OrderRepository) -> list[dict[str, Any]]:
    return repository.find_orders_by_status(set(RECONCILIATION_TARGET_STATUSES))


def reconcile_orders(
    repository: OrderRepository,
    kis_rows: list[dict[str, Any]],
    *,
    time_window_minutes: int = 10,
) -> list[ReconciliationResult]:
    results: list[ReconciliationResult] = []
    for order in reconciliation_targets(repository):
        row = match_order_row(order, kis_rows, time_window_minutes=time_window_minutes)
        if row is None:
            continue
        status = status_from_order_row(row, expected_qty=Decimal(str(order["qty"])), current_status=OrderStatus(order["status"]))
        reason = row.get("status_reason")
        execution_id = row.get("execution_id") or row.get("broker_order_id")
        repository.update_reconciled_order(order["order_id"], status, reason, execution_id=execution_id, payload=row)
        results.append(ReconciliationResult(order["order_id"], status.value, reason))
    return results


def build_reconciliation_report(
    repository: OrderRepository,
    kis_rows: list[dict[str, Any]],
    *,
    time_window_minutes: int = 10,
) -> ReconciliationReport:
    results = reconcile_orders(repository, kis_rows, time_window_minutes=time_window_minutes)
    alerts = find_reconciliation_alerts(repository, kis_rows, time_window_minutes=time_window_minutes)
    return ReconciliationReport(results=results, alerts=alerts)


def find_reconciliation_alerts(
    repository: OrderRepository,
    kis_rows: list[dict[str, Any]],
    *,
    time_window_minutes: int = 10,
) -> list[ReconciliationAlert]:
    open_orders = repository.find_open_orders()
    alerts: list[ReconciliationAlert] = []
    for order in open_orders:
        if order["status"] == OrderStatus.SUBMIT_FAILED_UNKNOWN.value:
            row = match_order_row(order, kis_rows, time_window_minutes=time_window_minutes)
            if row is None:
                alerts.append(
                    ReconciliationAlert(
                        alert_type="internal_order_missing_in_kis",
                        order_id=order["order_id"],
                        reason="internal uncertain order was not found in KIS rows",
                    )
                )
    for row in kis_rows:
        if match_kis_row_to_any_order(row, open_orders, time_window_minutes=time_window_minutes) is None:
            alerts.append(
                ReconciliationAlert(
                    alert_type="external_order_missing_in_internal",
                    broker_order_id=row.get("broker_order_id"),
                    reason="KIS row has no matching internal order",
                    row=row,
                )
            )
    return alerts


def match_kis_row_to_any_order(row: dict[str, Any], orders: list[dict[str, Any]], *, time_window_minutes: int = 10) -> dict[str, Any] | None:
    for order in orders:
        if match_order_row(order, [row], time_window_minutes=time_window_minutes) is not None:
            return order
    return None


def match_order_row(
    order: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    time_window_minutes: int = 10,
) -> dict[str, Any] | None:
    broker_order_id = order.get("broker_order_id")
    if broker_order_id:
        for row in rows:
            if row.get("broker_order_id") == broker_order_id:
                return row

    client_order_id = order.get("client_order_id")
    if client_order_id:
        for row in rows:
            if row.get("client_order_id") == client_order_id:
                return row

    candidates: list[dict[str, Any]] = []
    for row in rows:
        if row.get("account_alias") != order.get("account_alias"):
            continue
        if str(row.get("symbol", "")).upper() != str(order.get("symbol", "")).upper():
            continue
        if row.get("side") != order.get("side"):
            continue
        if _decimal_or_none(row.get("qty")) != _decimal_or_none(order.get("qty")):
            continue
        if _decimal_or_none(row.get("price")) != _decimal_or_none(order.get("price")):
            continue
        if not _within_time_window(order.get("occurred_at"), row.get("occurred_at"), time_window_minutes):
            continue
        candidates.append(row)
    if len(candidates) == 1:
        return candidates[0]
    return None


def status_from_order_row(row: dict[str, Any], *, expected_qty: Decimal, current_status: OrderStatus) -> OrderStatus:
    broker_status = str(row.get("status", "")).lower()
    if broker_status in {"rejected", "reject"}:
        return OrderStatus.REJECTED
    if broker_status in {"canceled", "cancelled"}:
        if current_status == OrderStatus.SUBMIT_FAILED_UNKNOWN:
            return OrderStatus.RECONCILIATION_REQUIRED
        return OrderStatus.CANCELED

    filled_qty = _decimal_or_none(row.get("filled_qty", "0"))
    if filled_qty is None or filled_qty < 0:
        return OrderStatus.RECONCILIATION_REQUIRED
    if filled_qty == 0:
        return OrderStatus.SUBMITTED
    if filled_qty < expected_qty:
        return OrderStatus.PARTIALLY_FILLED
    if filled_qty == expected_qty:
        return OrderStatus.FILLED
    return OrderStatus.RECONCILIATION_REQUIRED


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _within_time_window(left: Any, right: Any, minutes: int) -> bool:
    left_dt = _parse_dt(left)
    right_dt = _parse_dt(right)
    if left_dt is None or right_dt is None:
        return False
    return abs(left_dt - right_dt) <= timedelta(minutes=minutes)


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
