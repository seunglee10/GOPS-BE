from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


REGISTER_TERMS = ("걸어줘", "등록해줘", "추가해줘", "예약해줘", "설정해줘", "걸어 줘", "등록해 줘")
REFERENCE_TERMS = ("이 가격", "그 가격", "이 조건", "그 조건", "방금", "추천")
BUY_TERMS = ("매수", "사줘", "살", "buy")
SELL_TERMS = ("매도", "팔아", "팔", "sell")
QUANTITY_PATTERN = re.compile(r"(?<![0-9])([1-9][0-9]{0,5})\s*(?:주|개|shares?)", re.IGNORECASE)


@dataclass(frozen=True)
class CommandResolution:
    status: str
    proposal: dict[str, Any] | None = None
    clarification: str | None = None
    reason: str | None = None


def resolve_trade_condition_command(
    text: str,
    proposals: list[dict[str, Any]],
    *,
    proposal_id: str | None = None,
) -> CommandResolution:
    normalized = str(text or "").strip()
    lowered = normalized.lower()
    quantity = quantity_from_text(normalized)
    explicit_register = any(term in lowered for term in REGISTER_TERMS)
    references_proposal = any(term in lowered for term in REFERENCE_TERMS)
    continuation = bool(proposal_id and quantity is not None)
    if not explicit_register and not continuation:
        return CommandResolution(status="not_matched")
    if not proposal_id and not references_proposal:
        return CommandResolution(status="not_matched")

    candidates = [proposal for proposal in proposals if isinstance(proposal, dict)]
    if proposal_id:
        candidates = [item for item in candidates if str(item.get("proposalId")) == proposal_id]
    candidates = _filter_side(candidates, lowered)
    if not candidates:
        return CommandResolution(
            status="clarify",
            clarification="등록할 가격 조건 제안을 찾지 못했습니다. 먼저 정확한 매수 또는 매도 가격을 추천받아주세요.",
            reason="proposal_not_found",
        )
    if len(candidates) > 1:
        return CommandResolution(
            status="clarify",
            clarification="추천된 조건이 여러 개입니다. 매수 조건인지 매도 조건인지 알려주세요.",
            reason="ambiguous_proposal",
        )

    proposal = dict(candidates[0])
    if _is_expired(proposal.get("expiresAt")):
        return CommandResolution(
            status="rejected",
            clarification="추천 조건이 만료되었습니다. 현재 차트 기준으로 다시 추천받아주세요.",
            reason="proposal_expired",
        )
    if quantity is not None:
        proposal["quantity"] = quantity
    if "알림만" in lowered:
        proposal["executionEnabled"] = False
        proposal["alertsEnabled"] = True
    elif "예약매매만" in lowered or "주문만" in lowered:
        proposal["executionEnabled"] = True
        proposal["alertsEnabled"] = False
    elif "알림" in lowered:
        proposal["alertsEnabled"] = True

    missing = []
    if not _positive_number(proposal.get("limitPrice")):
        missing.append("limitPrice")
    if not _positive_integer(proposal.get("quantity")):
        missing.append("quantity")
    if missing:
        field_label = "수량" if missing == ["quantity"] else "지정 가격과 수량"
        return CommandResolution(
            status="clarify",
            proposal=proposal,
            clarification=f"{field_label}을 알려주세요. 예: 5주로 등록해줘",
            reason="missing_fields",
        )
    proposal["missingFields"] = []
    return CommandResolution(status="ready", proposal=proposal)


def quantity_from_text(text: str) -> int | None:
    match = QUANTITY_PATTERN.search(str(text or ""))
    return int(match.group(1)) if match else None


def _filter_side(proposals: list[dict[str, Any]], lowered: str) -> list[dict[str, Any]]:
    buy = any(term in lowered for term in BUY_TERMS)
    sell = any(term in lowered for term in SELL_TERMS)
    if buy == sell:
        return proposals
    side = "buy" if buy else "sell"
    return [item for item in proposals if item.get("side") == side]


def _is_expired(value: Any) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed <= datetime.now(timezone.utc)


def _positive_number(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _positive_integer(value: Any) -> bool:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return False
    return parsed > 0 and parsed.is_integer()
