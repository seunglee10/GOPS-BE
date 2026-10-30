"""계좌 최근 활동 목록 (D20).

페이퍼 체결, 알림 규칙 발동, 현금 원장의 입금·배당을 한 목록으로 합쳐
최신순으로 돌려준다. 각 항목은 `{at, kind, symbol, nameKo, text, ref}` 이다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from app.services.instrument_names import name_ko


KIND_FILL = "fill"
KIND_RULE = "rule"
KIND_DIVIDEND = "dividend"
KIND_DEPOSIT = "deposit"

# paper_cash_ledger.event_type 중 활동 목록에 싣는 값. 지금 원장은 주문 예약·체결만
# 쓰지만 입금·배당이 원장에 들어오면 이 이름으로 잡힌다.
DEPOSIT_EVENT_TYPES = frozenset({"cash.deposit", "account.deposit", "deposit"})
DIVIDEND_EVENT_TYPES = frozenset({"cash.dividend", "dividend", "dividend.paid", "position.dividend"})
CASH_ACTIVITY_EVENT_TYPES = DEPOSIT_EVENT_TYPES | DIVIDEND_EVENT_TYPES

_SIDE_KO = {"buy": "매수", "sell": "매도"}
_DIRECTION_KO = {"above": "위", "below": "아래"}


def build_account_activity(
    *,
    executions: Iterable[dict[str, Any]],
    notifications: Iterable[dict[str, Any]],
    cash_entries: Iterable[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    items: list[tuple[datetime, str, dict[str, Any]]] = []
    for execution in executions:
        item = _fill_item(execution)
        if item is not None:
            items.append(item)
    for notification in notifications:
        item = _rule_item(notification)
        if item is not None:
            items.append(item)
    for entry in cash_entries:
        item = _cash_item(entry)
        if item is not None:
            items.append(item)
    items.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [item for _at, _tiebreak, item in items[:limit]]


def _fill_item(row: dict[str, Any]) -> tuple[datetime, str, dict[str, Any]] | None:
    at = _as_datetime(row.get("executed_at"))
    symbol = _symbol(row.get("symbol"))
    quantity = _decimal(row.get("quantity"))
    price = _decimal(row.get("price"))
    side = _SIDE_KO.get(str(row.get("side") or "").lower())
    if at is None or not symbol or quantity is None or price is None or side is None:
        return None
    display = _display_name(symbol)
    text = f"{display} {_quantity(quantity)}주 {side} 체결 {_usd(price)}"
    ref = {"orderId": row.get("order_id"), "executionId": row.get("execution_id")}
    return at, f"fill:{row.get('execution_id')}", _item(at, KIND_FILL, symbol, text, ref)


def _rule_item(row: dict[str, Any]) -> tuple[datetime, str, dict[str, Any]] | None:
    notification_type = str(row.get("type") or "")
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    # 사용자가 만든 알림 규칙(조건 주문 포함)이 울린 것만 싣는다. 장 시작·추천 같은 시스템 알림은 뺀다.
    if not notification_type.startswith("alert.") or (row.get("alert_id") is None and payload.get("alertId") is None):
        return None
    at = _as_datetime(payload.get("triggeredAt")) or _as_datetime(row.get("created_at"))
    if at is None:
        return None
    symbol = _symbol(payload.get("symbol"))
    display = _display_name(symbol) if symbol else "종목"
    text = f"{display} {_rule_phrase(notification_type, payload)} 알림이 울렸어요".replace("  ", " ")
    alert_id = row.get("alert_id") if row.get("alert_id") is not None else payload.get("alertId")
    ref = {"notificationId": row.get("id"), "alertId": alert_id}
    return at, f"rule:{row.get('id')}", _item(at, KIND_RULE, symbol or None, text, ref)


def _rule_phrase(notification_type: str, payload: dict[str, Any]) -> str:
    direction = _DIRECTION_KO.get(str(payload.get("direction") or "").lower(), "")
    if notification_type == "alert.price_cross":
        target = _decimal(payload.get("targetPrice"))
        return f"{_plain_price(target)} {direction}" if target is not None else direction
    if notification_type == "alert.spike":
        threshold = _decimal(payload.get("thresholdPct") or payload.get("alertThresholdPct"))
        window = payload.get("windowMin")
        if threshold is None:
            return ""
        sign = "-" if str(payload.get("direction") or "").lower() == "below" else "+"
        prefix = f"{window}분 " if window else ""
        return f"{prefix}{sign}{_plain_number(threshold)}%"
    if notification_type == "alert.rsi_threshold":
        threshold = _decimal(payload.get("threshold"))
        return f"RSI {_plain_number(threshold)} {direction}" if threshold is not None else "RSI"
    if notification_type in {"alert.volume_absolute", "alert.volume_relative"}:
        return "거래량"
    return ""


def _cash_item(row: dict[str, Any]) -> tuple[datetime, str, dict[str, Any]] | None:
    event_type = str(row.get("event_type") or "")
    at = _as_datetime(row.get("created_at"))
    amount = _decimal(row.get("cash_delta"))
    if at is None or amount is None:
        return None
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    symbol = _symbol(payload.get("symbol"))
    ref = {"entryId": row.get("entry_id")}
    if event_type in DIVIDEND_EVENT_TYPES:
        display = _display_name(symbol) if symbol else "보유 종목"
        text = f"{display} 배당금 입금 {_signed_usd(amount)}"
        return at, f"dividend:{row.get('entry_id')}", _item(at, KIND_DIVIDEND, symbol or None, text, ref)
    if event_type in DEPOSIT_EVENT_TYPES:
        text = f"현금 입금 {_signed_usd(amount)}"
        return at, f"deposit:{row.get('entry_id')}", _item(at, KIND_DEPOSIT, symbol or None, text, ref)
    return None


def _item(at: datetime, kind: str, symbol: str | None, text: str, ref: dict[str, Any]) -> dict[str, Any]:
    return {
        "at": at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "kind": kind,
        "symbol": symbol,
        "nameKo": name_ko(symbol) if symbol else None,
        "text": text,
        "ref": {key: value for key, value in ref.items() if value is not None},
    }


def _display_name(symbol: str) -> str:
    return name_ko(symbol) or symbol


def _symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    return "" if symbol in {"MARKET", "PORTFOLIO", "UNKNOWN", "ALERT"} else symbol


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _quantity(value: Decimal) -> str:
    return f"{value:,.0f}" if value == value.to_integral_value() else f"{value.normalize():,f}"


def _usd(value: Decimal) -> str:
    return f"${value:,.2f}"


def _signed_usd(value: Decimal) -> str:
    return f"+${value:,.2f}" if value >= 0 else f"-${abs(value):,.2f}"


def _plain_price(value: Decimal) -> str:
    return f"{value:,.2f}"


def _plain_number(value: Decimal) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text or "0"
