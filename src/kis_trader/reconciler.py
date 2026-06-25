from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from .broker_adapter import extract_kis_order_id
from .client import KisOverseasClient
from .config import KisConfig
from .order_contract import OrderStatus, build_order_key
from .redaction import redact_sensitive
from .repository import PostgresOrderRepository


FILLED_QTY_KEYS = [
    "TOT_CCLD_QTY",
    "tot_ccld_qty",
    "FT_CCLD_QTY",
    "ft_ccld_qty",
    "CCLD_QTY",
    "ccLD_qty",
    "ccld_qty",
]
CANCELED_KEYS = ["CNCL_YN", "cncl_yn", "CANCEL_YN", "cancel_yn"]


@dataclass(frozen=True)
class ReconciliationSummary:
    run_id: str
    market: str
    unknown_orders: int
    matched_unknown_orders: int
    mismatches: int
    emitted_events: int


def poll_and_reconcile_order_history(
    *,
    config: KisConfig,
    client: KisOverseasClient,
    market: str,
    start_date: date,
    end_date: date,
    repository: PostgresOrderRepository | None = None,
) -> ReconciliationSummary:
    repo = repository or PostgresOrderRepository(config.database_url)
    repo.ensure_schema()
    run_id = str(uuid4())

    if market == "overseas":
        response = client.order_history(start_date=start_date, end_date=end_date)
    elif market == "domestic":
        response = client.domestic_order_history(start_date=start_date, end_date=end_date)
    else:
        raise ValueError("market must be domestic or overseas.")

    rows = flatten_kis_order_rows(response)
    unknown_orders = repo.find_unknown_orders(
        market=market,
        env=config.env,
        account_alias=config.kafka_account_alias,
    )
    matched = 0
    mismatches = 0
    emitted = 0

    for order in unknown_orders:
        row = match_order_row(order, rows)
        if row is None:
            continue
        matched += 1
        status = status_from_order_row(row, expected_qty=Decimal(str(order["qty"])))
        if status == OrderStatus.RECONCILIATION_REQUIRED:
            mismatches += 1
        broker_order_id = extract_kis_order_id(row)
        payload = {
            "schema_version": 1,
            "event_type": "order.reconciled",
            "event_id": str(uuid4()),
            "request_id": str(order["request_id"]),
            "producer": "kis-reconciler",
            "env": config.env,
            "account_alias": config.kafka_account_alias,
            "status": status.value,
            "payload": {
                "market": market,
                "symbol": order["symbol"],
                "kis_order_id": broker_order_id,
                "kis_order_row": redact_sensitive(row),
            },
        }
        repo.update_reconciled_order(
            order_id=str(order["order_id"]),
            request_id=str(order["request_id"]),
            account_alias=config.kafka_account_alias,
            status=status,
            broker_order_id=broker_order_id,
            payload=payload,
            topic=config.kafka_reconciled_topic,
            key=build_order_key(config.kafka_account_alias, str(order["symbol"])),
        )
        emitted += 1

    broker_event_payload = {
        "schema_version": 1,
        "event_type": "broker.order_history.polled",
        "event_id": str(uuid4()),
        "run_id": run_id,
        "producer": "kis-order-history-poller",
        "env": config.env,
        "account_alias": config.kafka_account_alias,
        "payload": {
            "market": market,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "response": redact_sensitive(response),
        },
    }
    repo.record_broker_order_event(
        topic=config.kafka_order_events_topic,
        key=build_order_key(config.kafka_account_alias, market),
        payload=broker_event_payload,
    )

    summary = ReconciliationSummary(
        run_id=run_id,
        market=market,
        unknown_orders=len(unknown_orders),
        matched_unknown_orders=matched,
        mismatches=mismatches,
        emitted_events=emitted + 1,
    )
    repo.record_reconciliation_run(
        run_id=run_id,
        market=market,
        env=config.env,
        account_alias=config.kafka_account_alias,
        status="completed",
        result=summary.__dict__,
    )
    return summary


def flatten_kis_order_rows(payload: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key.lower().startswith("output") and isinstance(value, list):
                rows.extend(item for item in value if isinstance(item, dict))
            elif key.lower().startswith("output") and isinstance(value, dict):
                rows.append(value)
            else:
                rows.extend(flatten_kis_order_rows(value))
    elif isinstance(payload, list):
        for item in payload:
            rows.extend(flatten_kis_order_rows(item))
    return rows


def match_order_row(order: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    broker_order_id = order.get("broker_order_id")
    if broker_order_id:
        for row in rows:
            if extract_kis_order_id(row) == str(broker_order_id):
                return row

    symbol = str(order.get("symbol", "")).upper()
    for row in rows:
        row_symbol = _find_first(row, ["PDNO", "pdno", "SYMB", "symb", "symbol"])
        if row_symbol and row_symbol.upper() == symbol:
            return row
    return None


def status_from_order_row(row: dict[str, Any], *, expected_qty: Decimal) -> OrderStatus:
    if _is_canceled(row):
        return OrderStatus.CANCELED
    filled_qty = _decimal_from_candidates(row, FILLED_QTY_KEYS)
    if filled_qty is None:
        return OrderStatus.SUBMITTED
    if filled_qty <= 0:
        return OrderStatus.SUBMITTED
    if filled_qty < expected_qty:
        return OrderStatus.PARTIALLY_FILLED
    if filled_qty == expected_qty:
        return OrderStatus.FILLED
    return OrderStatus.RECONCILIATION_REQUIRED


def _is_canceled(row: dict[str, Any]) -> bool:
    value = _find_first(row, CANCELED_KEYS)
    return value in {"Y", "y", "1", "true", "TRUE"}


def _decimal_from_candidates(row: dict[str, Any], keys: list[str]) -> Decimal | None:
    value = _find_first(row, keys)
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except InvalidOperation:
        return None


def _find_first(payload: Any, keys: list[str]) -> str | None:
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
        for value in payload.values():
            found = _find_first(value, keys)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_first(item, keys)
            if found:
                return found
    return None
