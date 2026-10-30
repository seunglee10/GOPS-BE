"""투자 복기 API (D27).

GET /api/review/monthly?month=YYYY-MM  월간 리포트
PUT /api/review/notes/{orderId}        복기 메모·습관 고치기 (주문 주인만)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder

from app.auth.dependencies import require_current_user
from app.auth.models import AuthenticatedUser
from app.review.market import review_market_data_from_app
from app.review.service import HABIT_LABELS_KO, InvalidMonth, build_monthly_review, parse_month

router = APIRouter(prefix="/api/review", tags=["review"])

REVIEW_MEMO_MAX_LENGTH = 200


def _now(app: Any) -> datetime:
    provider: Callable[[], datetime] | None = getattr(app.state, "review_now_provider", None)
    return provider() if callable(provider) else datetime.now(timezone.utc)


def _paper_repository(app: Any) -> Any:
    from app.routes.paper_trading import paper_repository_from_app

    return paper_repository_from_app(app)


@router.get("/monthly")
def monthly_review(
    request: Request,
    month: str | None = Query(default=None, max_length=7),
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    now = _now(request.app)
    try:
        month_start, _month_end = parse_month(month, now)
    except InvalidMonth as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_month"}) from exc
    repository = _paper_repository(request.app)
    try:
        executions = repository.list_review_executions(user.sub)
        order_ids = list(dict.fromkeys(str(row.get("order_id")) for row in executions if row.get("order_id")))
        notes = repository.list_order_notes(user.sub, order_ids)
        account = (repository.account_snapshot(user.sub) or {}).get("account") or {}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "review_unavailable"}) from exc
    report = build_monthly_review(
        month_start=month_start,
        executions=executions,
        notes=notes,
        market=review_market_data_from_app(request.app),
        starting_cash=account.get("starting_cash"),
        now=now,
    )
    return jsonable_encoder(report)


@router.put("/notes/{order_id}")
def update_review_note(
    order_id: str,
    request: Request,
    body: dict[str, Any] = Body(default_factory=dict),
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail={"code": "invalid_body"})
    repository = _paper_repository(request.app)
    if repository.get_order(user.sub, order_id) is None:
        raise HTTPException(status_code=404, detail={"code": "order_not_found"})
    existing = repository.list_order_notes(user.sub, [order_id]).get(order_id) or {}

    memo = existing.get("memo")
    if "memo" in body:
        memo = _review_memo(body.get("memo"))
    habit = existing.get("habit") if existing.get("habit_source") == "user" else None
    if "habit" in body:
        habit = _habit(body.get("habit"))
    habit_source = "user" if habit else "auto"

    saved = repository.upsert_order_note(user.sub, order_id, memo=memo, habit=habit, habit_source=habit_source)
    if saved is None:
        raise HTTPException(status_code=404, detail={"code": "order_not_found"})
    updated_at = saved.get("updated_at")
    return {
        "orderId": order_id,
        "memo": saved.get("memo"),
        "habit": saved.get("habit"),
        "habitSource": saved.get("habit_source"),
        "updatedAt": (
            updated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            if isinstance(updated_at, datetime)
            else updated_at
        ),
    }


def _review_memo(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail={"code": "invalid_memo"})
    memo = value.strip()
    if len(memo) > REVIEW_MEMO_MAX_LENGTH:
        raise HTTPException(
            status_code=422,
            detail={"code": "memo_too_long", "maxLength": REVIEW_MEMO_MAX_LENGTH},
        )
    return memo or None


def _habit(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or value not in HABIT_LABELS_KO:
        raise HTTPException(status_code=422, detail={"code": "invalid_habit"})
    return value
