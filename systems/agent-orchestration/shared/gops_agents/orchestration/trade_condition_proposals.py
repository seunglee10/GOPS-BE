from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..contracts import TradeConditionProposal, stable_id


BUY_TERMS = ("매수", "사고", "사면", "진입", "매입", "buy", "entry")
SELL_TERMS = ("매도", "팔고", "팔면", "익절", "청산", "sell", "exit")
PRICE_RECOMMENDATION_TERMS = (
    "가격",
    "조건",
    "어디서",
    "얼마에",
    "추천",
    "예약",
    "진입",
    "매수",
    "매도",
    "entry",
    "price",
    "buy",
    "sell",
)
QUANTITY_PATTERN = re.compile(r"(?<![0-9])([1-9][0-9]{0,5})\s*(?:주|개|shares?)", re.IGNORECASE)


def build_trade_condition_proposals(
    *,
    analysis_id: str,
    symbol: str,
    intent: str,
    chart_context: dict[str, Any],
) -> list[TradeConditionProposal]:
    """Build deterministic price proposals from structured chart candles.

    The builder intentionally never extracts a price from synthesized prose. It
    only uses the candle payload already attached to the authenticated analysis
    request, so a follow-up command can reference a stable proposal id.
    """

    lowered = str(intent or "").lower()
    if not any(term in lowered for term in PRICE_RECOMMENDATION_TERMS):
        return []
    side = _resolve_side(lowered)
    if side is None:
        return []
    candles = _normalized_candles(chart_context.get("candles"))
    if not candles:
        return []
    recent = candles[-20:]
    if side == "buy":
        trigger_price = min(item["low"] for item in recent)
        direction = "atOrBelow"
        rationale = "최근 20개 표시 봉의 저가 범위를 기준으로 계산한 매수 관심 가격입니다."
    else:
        trigger_price = max(item["high"] for item in recent)
        direction = "atOrAbove"
        rationale = "최근 20개 표시 봉의 고가 범위를 기준으로 계산한 매도 관심 가격입니다."
    trigger_price = round(trigger_price, 4)
    quantity = _quantity_from_intent(intent)
    created_at = datetime.now(timezone.utc)
    proposal_id = stable_id(
        "trade-condition-proposal",
        {
            "analysisId": analysis_id,
            "symbol": symbol.upper(),
            "side": side,
            "direction": direction,
            "triggerPrice": trigger_price,
        },
    )
    return [
        TradeConditionProposal(
            proposalId=proposal_id,
            analysisId=analysis_id,
            symbol=symbol.upper(),
            exchange="NASD",
            side=side,
            direction=direction,
            triggerPrice=trigger_price,
            limitPrice=trigger_price,
            quantity=quantity,
            executionEnabled=True,
            alertsEnabled=True,
            validity="DAY",
            missingFields=[] if quantity is not None else ["quantity"],
            rationale=rationale,
            createdAt=created_at.isoformat().replace("+00:00", "Z"),
            expiresAt=(created_at + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
        )
    ]


def _resolve_side(lowered: str) -> str | None:
    buy = any(term in lowered for term in BUY_TERMS)
    sell = any(term in lowered for term in SELL_TERMS)
    if buy == sell:
        return None
    return "buy" if buy else "sell"


def _quantity_from_intent(intent: str) -> int | None:
    match = QUANTITY_PATTERN.search(str(intent or ""))
    return int(match.group(1)) if match else None


def _normalized_candles(value: Any) -> list[dict[str, float]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, float]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            high = float(item.get("high"))
            low = float(item.get("low"))
        except (TypeError, ValueError):
            continue
        if high <= 0 or low <= 0 or high < low:
            continue
        normalized.append({"high": high, "low": low})
    return normalized
