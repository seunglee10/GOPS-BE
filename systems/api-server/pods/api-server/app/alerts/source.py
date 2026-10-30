"""규칙 출처(`source`) — 알림·조건주문 공통 (MOCK_API_CONTRACT D23).

허용값: agent(에이전트 제안) · chart(차트) · user(직접 만듦) · default(기본 설정).

기존 컬럼과의 관계 (0027_rule_source.sql 백필과 같은 규칙):
- `alerts.proposal_source`(daily_trade 등)는 "코치의 어느 분석에서 나온 제안인가"를
  뜻하는 세부 분류라 그대로 둔다. 값이 있으면 source=agent.
- `alerts.created_via`는 생성 경로(manual/chart/ai_coach/agent_chat/trade_condition)다.
  chart→chart, ai_coach→agent, 나머지(manual·agent_chat)→user.
  agent_chat은 사용자가 직접 쓴 문장을 해석한 것이라 user로 본다.
- `trade_conditions.source`의 옛 값 manual은 user로 바꾼다. 조건주문이 만든
  알림(created_via=trade_condition)은 그 조건주문의 source를 따른다.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

RULE_SOURCES = ("agent", "chart", "user", "default")
DEFAULT_RULE_SOURCE = "user"
LEGACY_SOURCE_ALIASES = {"manual": "user"}


def validate_rule_source(value: Any) -> str | None:
    """요청 body 값 검사. 없으면 None, 모르는 값이면 422 invalid_source."""
    if value is None:
        return None
    normalized = str(value).strip().lower() if isinstance(value, str) else None
    if normalized not in RULE_SOURCES:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_source", "allowed": list(RULE_SOURCES)},
        )
    return normalized


def source_from_legacy(*, created_via: str | None = None, proposal_source: str | None = None) -> str:
    """source가 없는 요청·행을 옛 필드로 판정 (마이그레이션 백필과 같은 규칙)."""
    if proposal_source or created_via == "ai_coach":
        return "agent"
    if created_via == "chart":
        return "chart"
    return DEFAULT_RULE_SOURCE


def normalize_stored_source(value: Any) -> str:
    """저장된 값 읽기 — 롤아웃 중 남은 manual 같은 옛 값도 계약 값으로 맞춘다."""
    text = str(value or "").strip().lower()
    text = LEGACY_SOURCE_ALIASES.get(text, text)
    return text if text in RULE_SOURCES else DEFAULT_RULE_SOURCE
