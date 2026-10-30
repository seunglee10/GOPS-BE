"""주문 전 점검 가드레일 (MOCK_API_CONTRACT D26).

`POST /api/paper/risk/pretrade` 응답의 `checks[]`를 만든다. 각 항목 모양:

    {"code", "severity", "messageKo", "detailKo", "value", "limit"}

severity: ok(통과) · warn(경고만) · confirm(한 번 더 확인) · block(주문 막음).
규칙이 해당하지 않거나(매도인데 매수 규칙 등), 설정이 꺼져 있거나, 판단에 필요한
값(오늘 등락률·손절 조건 조회)을 못 구하면 그 항목은 넣지 않는다.

- chase_buy: 매수 + 오늘 등락률 >= chaseBuyWarnPercent → warn
- avg_down_no_stop: 손실 중인 보유 종목 추가 매수 + 현재가 아래 손절 조건(매도 조건주문
  또는 하락 가격 알림)이 없음 → blockAverageDownWithoutStop이면 block, 아니면 넣지 않음
- sell_on_drop: 매도 + 오늘 등락률 <= -confirmSellOnDropPercent → confirm
- position_weight: 매수 후 (보유 평가액 + 주문 금액) / 총자산 > maxPositionWeightPercent → block
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

SEVERITY_OK = "ok"
SEVERITY_WARN = "warn"
SEVERITY_CONFIRM = "confirm"
SEVERITY_BLOCK = "block"


@dataclass(frozen=True)
class GuardrailInput:
    side: str
    symbol: str
    qty: Decimal
    price: Decimal
    settings: dict[str, Any]
    day_change_percent: float | None = None
    position_qty: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")
    current_price: Decimal | None = None
    equity: Decimal | None = None
    has_protective_stop: bool | None = None


def evaluate_guardrails(data: GuardrailInput) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if data.side == "buy":
        for builder in (_position_weight, _chase_buy, _avg_down_no_stop):
            check = builder(data)
            if check is not None:
                checks.append(check)
    elif data.side == "sell":
        check = _sell_on_drop(data)
        if check is not None:
            checks.append(check)
    return checks


def _check(code: str, severity: str, message: str, detail: str | None, value: Any, limit: Any) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "messageKo": message,
        "detailKo": detail,
        "value": value,
        "limit": limit,
    }


def _chase_buy(data: GuardrailInput) -> dict[str, Any] | None:
    threshold = data.settings.get("chaseBuyWarnPercent")
    if threshold is None or data.day_change_percent is None:
        return None
    limit = float(threshold)
    change = float(data.day_change_percent)
    if change >= limit:
        return _check(
            "chase_buy", SEVERITY_WARN,
            f"오늘 {_pct(change)} 오른 종목이에요",
            "급등일 추격 매수 경고가 켜져 있어요",
            change, limit,
        )
    return _check("chase_buy", SEVERITY_OK, "추격 매수가 아니에요", None, change, limit)


def _sell_on_drop(data: GuardrailInput) -> dict[str, Any] | None:
    threshold = data.settings.get("confirmSellOnDropPercent")
    if threshold is None or data.day_change_percent is None:
        return None
    limit = -float(threshold)
    change = float(data.day_change_percent)
    if change <= limit:
        return _check(
            "sell_on_drop", SEVERITY_CONFIRM,
            f"오늘 {_pct(change)} 내린 종목을 파는 주문이에요",
            "하루 하락 매도 전 한 번 더 확인해 주세요",
            change, limit,
        )
    return _check("sell_on_drop", SEVERITY_OK, "크게 내린 날 파는 주문이 아니에요", None, change, limit)


def _avg_down_no_stop(data: GuardrailInput) -> dict[str, Any] | None:
    if data.settings.get("blockAverageDownWithoutStop") is not True:
        return None
    if data.position_qty <= 0 or data.average_price <= 0 or data.current_price is None:
        return None
    if data.current_price >= data.average_price:
        return None  # 손실 중이 아니면 물타기가 아니다
    if data.has_protective_stop is None:
        return None  # 손절 조건을 확인하지 못했으면 막지 않는다
    loss_percent = _round((data.current_price - data.average_price) / data.average_price * 100)
    if data.has_protective_stop:
        return _check(
            "avg_down_no_stop", SEVERITY_OK,
            "손절가가 걸려 있어요",
            f"평균 단가보다 {_pct(loss_percent)} 아래에서 추가 매수해요",
            loss_percent, None,
        )
    return _check(
        "avg_down_no_stop", SEVERITY_BLOCK,
        "손절가 없이 물타기하는 주문이에요",
        f"평균 단가보다 {_pct(loss_percent)} 아래예요. 손절가 조건을 먼저 걸어 주세요",
        loss_percent, None,
    )


def _position_weight(data: GuardrailInput) -> dict[str, Any] | None:
    limit_value = data.settings.get("maxPositionWeightPercent")
    if limit_value is None or data.equity is None or data.equity <= 0:
        return None
    mark = data.current_price if data.current_price is not None else data.price
    held_value = data.position_qty * mark
    raw_weight = (held_value + data.qty * data.price) / data.equity * 100
    weight = _round(raw_weight)
    limit = float(limit_value)
    label = f"{data.symbol} 비중 {weight:.0f}%"
    if raw_weight > Decimal(str(limit_value)):
        return _check(
            "position_weight", SEVERITY_BLOCK,
            f"{label}, 한도 {limit:.0f}%를 넘어요",
            "코치 가드레일",
            weight, limit,
        )
    return _check("position_weight", SEVERITY_OK, f"{label}, 한도 {limit:.0f}% 안이에요", "코치 가드레일", weight, limit)


def has_protective_stop(
    symbol: str,
    current_price: Decimal,
    *,
    trade_conditions: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
) -> bool:
    """현재가 아래에 걸린 손절 조건이 있는가.

    - 매도·atOrBelow·watching 조건주문, 또는
    - 직접 건 하락(below) 가격 알림(조건주문이 만든 알림은 조건주문 쪽에서 판단).
    """
    symbol = symbol.upper()
    for row in trade_conditions:
        if (
            str(row.get("symbol") or "").upper() == symbol
            and row.get("side") == "sell"
            and row.get("status") == "watching"
            and _below(row.get("target_price"), current_price)
        ):
            return True
    for row in alerts:
        if (
            str(row.get("symbol") or "").upper() == symbol
            and row.get("status") == "active"
            and row.get("type") == "price_cross"
            and row.get("direction") == "below"
            and row.get("created_via") != "trade_condition"
            and _below(row.get("target_price"), current_price)
        ):
            return True
    return False


def apply_blocking_checks(risk: dict[str, Any], checks: list[dict[str, Any]]) -> dict[str, Any]:
    """block 항목이 있으면 기존 verdict·triggeredRules에도 반영한다."""
    blocking = [check for check in checks if check["severity"] == SEVERITY_BLOCK and check["code"] in GUARDRAIL_CODES]
    if not blocking:
        return risk
    risk["verdict"] = "block"
    rules = list(risk.get("triggeredRules") or [])
    for check in blocking:
        rules.append({
            "ruleId": check["code"],
            "action": "block",
            "title": check["messageKo"],
            "explanation": check.get("detailKo") or check["messageKo"],
            "guidance": "리스크 설정에서 가드레일을 확인해 주세요.",
        })
    risk["triggeredRules"] = rules
    return risk


GUARDRAIL_CODES = {"chase_buy", "avg_down_no_stop", "sell_on_drop", "position_weight"}


def _below(value: Any, current_price: Decimal) -> bool:
    try:
        return Decimal(str(value)) < current_price
    except Exception:
        return False


def _pct(value: float) -> str:
    return f"{value:+.1f}%"


def _round(value: Decimal) -> float:
    return round(float(value), 2)
