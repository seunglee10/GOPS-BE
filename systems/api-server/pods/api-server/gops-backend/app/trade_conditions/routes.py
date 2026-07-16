from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from app.alerts.routes import _repository_from_app as alert_repository_from_app
from app.alerts.routes import _resolve_current_price, _sync_projection
from app.alerts.repository import ACTIVE_ALERT_LIMIT
from app.auth.dependencies import auth_is_enabled, require_current_user
from app.auth.models import AuthenticatedUser
from app.services.agent_gateway import get_agent_report
from app.routes.simulator import simulator_gateway_from_app, simulator_mode_active
from .command_parser import resolve_trade_condition_command
from .repository import (
    DuplicateProposalError,
    InMemoryTradeConditionRepository,
    PostgresTradeConditionRepository,
    TradeConditionCreate,
)


router = APIRouter(tags=["trade-conditions"])
USER_MUTABLE_STATUSES = {"watching", "paused"}


class TradeConditionCreateBody(BaseModel):
    symbol: str = Field(min_length=1, max_length=12)
    side: str
    direction: str
    triggerPrice: Decimal
    limitPrice: Decimal
    quantity: int = Field(ge=1, le=1_000_000)
    exchange: str = Field(default="NASD", min_length=2, max_length=8)
    executionEnabled: bool = True
    alertsEnabled: bool = True
    validity: str = Field(default="DAY", max_length=32)


class TradeConditionPatchBody(BaseModel):
    status: str | None = None
    alertsEnabled: bool | None = None


class TradeConditionCommandBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    analysisId: str | None = Field(default=None, max_length=128)
    proposalId: str | None = Field(default=None, max_length=128)


@router.get("/api/trade-conditions")
def list_trade_conditions(
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    if simulator_mode_active(request.app):
        try:
            return simulator_gateway_from_app(request.app).conditions(user.sub)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    repository = _repository_from_app(request.app)
    return {"conditions": jsonable_encoder(repository.list_conditions(user.sub))}


@router.post("/api/trade-conditions", status_code=status.HTTP_201_CREATED)
def create_trade_condition(
    body: TradeConditionCreateBody,
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    if simulator_mode_active(request.app):
        try:
            return simulator_gateway_from_app(request.app).create_condition(
                user.sub,
                {
                    "symbol": body.symbol,
                    "side": body.side,
                    "direction": body.direction,
                    "triggerPrice": str(body.triggerPrice),
                    "limitPrice": str(body.limitPrice),
                    "quantity": body.quantity,
                    "executionEnabled": body.executionEnabled,
                    "alertsEnabled": body.alertsEnabled,
                    "validity": body.validity,
                },
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    created = _create_from_values(
        request,
        user_sub=user.sub,
        source="manual",
        symbol=body.symbol,
        side=body.side,
        direction=body.direction,
        trigger_price=body.triggerPrice,
        limit_price=body.limitPrice,
        quantity=body.quantity,
        exchange=body.exchange,
        execution_enabled=body.executionEnabled,
        alerts_enabled=body.alertsEnabled,
        validity=body.validity,
    )
    return created


@router.patch("/api/trade-conditions/{condition_id}")
def update_trade_condition(
    condition_id: int,
    body: TradeConditionPatchBody,
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    if body.status is None and body.alertsEnabled is None:
        raise HTTPException(status_code=422, detail="status or alertsEnabled is required")
    if body.status is not None and body.status not in USER_MUTABLE_STATUSES:
        raise HTTPException(status_code=422, detail="status must be watching or paused")
    if simulator_mode_active(request.app):
        try:
            return simulator_gateway_from_app(request.app).update_condition(
                user.sub,
                condition_id,
                {"status": body.status, "alertsEnabled": body.alertsEnabled},
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    repository = _repository_from_app(request.app)
    existing = repository.get_condition(user.sub, condition_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="trade condition not found")
    if body.status is not None and existing.get("status") not in USER_MUTABLE_STATUSES:
        raise HTTPException(status_code=409, detail="a triggered trade condition cannot be resumed")
    condition = repository.update_condition(
        user.sub,
        condition_id,
        status=body.status,
        alerts_enabled=body.alertsEnabled,
    )
    if condition is None:
        raise HTTPException(status_code=409, detail="trade condition changed while it was being updated")
    alert = alert_repository_from_app(request.app).get_alert(user.sub, int(condition["alert_id"]))
    projection_status = _sync_projection(request.app, "upsert", alert) if alert else "pending"
    return {"condition": jsonable_encoder(condition), "projectionStatus": projection_status}


@router.delete("/api/trade-conditions/{condition_id}")
def delete_trade_condition(
    condition_id: int,
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    if simulator_mode_active(request.app):
        try:
            return simulator_gateway_from_app(request.app).delete_condition(user.sub, condition_id)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    repository = _repository_from_app(request.app)
    condition = repository.get_condition(user.sub, condition_id)
    if condition is None:
        raise HTTPException(status_code=404, detail="trade condition not found")
    alert_id = int(condition["alert_id"])
    symbol = str(condition.get("symbol") or "")
    deleted = repository.delete_condition(user.sub, condition_id)
    projection_status = _sync_projection(request.app, "delete", {"id": alert_id, "symbol": symbol})
    return {"deleted": True, "condition": jsonable_encoder(deleted), "projectionStatus": projection_status}


@router.post("/api/trade-conditions/commands")
def resolve_trade_condition_chat_command(
    body: TradeConditionCommandBody,
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    if simulator_mode_active(request.app):
        raise HTTPException(status_code=409, detail="simulation_data_unavailable")
    if not _commands_enabled():
        return {"status": "not_matched", "reason": "trade_condition_commands_disabled"}
    if not body.analysisId:
        return {"status": "not_matched"}
    report = _agent_report_for_user(request.app, body.analysisId, user.sub)
    proposals = report.get("tradeConditionProposals") if isinstance(report, dict) else []
    resolution = resolve_trade_condition_command(
        body.text,
        proposals if isinstance(proposals, list) else [],
        proposal_id=body.proposalId,
    )
    if resolution.status != "ready" or resolution.proposal is None:
        return {
            "status": resolution.status,
            "clarification": resolution.clarification,
            "reason": resolution.reason,
            "proposal": resolution.proposal,
        }
    proposal = resolution.proposal
    if str(proposal.get("analysisId")) != body.analysisId:
        raise HTTPException(status_code=422, detail="proposal does not belong to the selected analysis")
    existing = next((
        item for item in _repository_from_app(request.app).list_conditions(user.sub)
        if item.get("proposal_id") == proposal.get("proposalId")
    ), None)
    if existing is not None:
        return {"status": "created", "condition": jsonable_encoder(existing), "idempotentReplay": True}
    try:
        created = _create_from_values(
            request,
            user_sub=user.sub,
            source="agent",
            symbol=proposal.get("symbol"),
            side=proposal.get("side"),
            direction=proposal.get("direction"),
            trigger_price=proposal.get("triggerPrice"),
            limit_price=proposal.get("limitPrice"),
            quantity=proposal.get("quantity"),
            exchange=proposal.get("exchange") or "NASD",
            execution_enabled=proposal.get("executionEnabled") is not False,
            alerts_enabled=proposal.get("alertsEnabled") is not False,
            validity=proposal.get("validity") or "DAY",
            proposal_id=str(proposal.get("proposalId") or "") or None,
            analysis_id=body.analysisId,
        )
    except DuplicateProposalError:
        existing = next((
            item for item in _repository_from_app(request.app).list_conditions(user.sub)
            if item.get("proposal_id") == proposal.get("proposalId")
        ), None)
        if existing is None:
            raise HTTPException(status_code=409, detail="proposal was already registered")
        return {"status": "created", "condition": jsonable_encoder(existing), "idempotentReplay": True}
    return {"status": "created", **created, "idempotentReplay": False}


def _create_from_values(
    request: Request,
    *,
    user_sub: str,
    source: str,
    symbol: Any,
    side: Any,
    direction: Any,
    trigger_price: Any,
    limit_price: Any,
    quantity: Any,
    exchange: Any,
    execution_enabled: bool,
    alerts_enabled: bool,
    validity: Any,
    proposal_id: str | None = None,
    analysis_id: str | None = None,
) -> dict[str, Any]:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_side = str(side or "").strip().lower()
    normalized_direction = str(direction or "").strip()
    normalized_exchange = str(exchange or "NASD").strip().upper()
    normalized_validity = _normalize_validity(validity)
    if not normalized_symbol or len(normalized_symbol) > 12:
        raise HTTPException(status_code=422, detail="symbol is invalid")
    if normalized_side not in {"buy", "sell"}:
        raise HTTPException(status_code=422, detail="side must be buy or sell")
    if normalized_direction not in {"atOrBelow", "atOrAbove"}:
        raise HTTPException(status_code=422, detail="direction must be atOrBelow or atOrAbove")
    trigger = _positive_decimal(trigger_price, "triggerPrice")
    limit_value = _positive_decimal(limit_price, "limitPrice")
    try:
        quantity_value = int(quantity)
    except (TypeError, ValueError):
        quantity_value = 0
    if quantity_value <= 0 or Decimal(str(quantity)) != Decimal(quantity_value):
        raise HTTPException(status_code=422, detail="quantity must be a positive whole-share value")
    current_price = _resolve_current_price(request.app, normalized_symbol)
    if normalized_direction == "atOrBelow" and trigger >= current_price:
        raise HTTPException(status_code=422, detail="atOrBelow triggerPrice must be below the current price")
    if normalized_direction == "atOrAbove" and trigger <= current_price:
        raise HTTPException(status_code=422, detail="atOrAbove triggerPrice must be above the current price")
    expires_at = datetime.now(timezone.utc) + (timedelta(days=1) if normalized_validity == "DAY" else timedelta(days=90))
    alert_repository = alert_repository_from_app(request.app)
    if alert_repository.active_alert_count(user_sub) >= ACTIVE_ALERT_LIMIT:
        raise HTTPException(status_code=409, detail=f"활성 알림은 최대 {ACTIVE_ALERT_LIMIT}개까지 등록할 수 있습니다.")
    repository = _repository_from_app(request.app)
    created = repository.create_condition(
        TradeConditionCreate(
            user_sub=user_sub,
            source=source,
            symbol=normalized_symbol,
            side=normalized_side,
            direction=normalized_direction,
            trigger_price=trigger,
            limit_price=limit_value,
            quantity=quantity_value,
            exchange=normalized_exchange,
            execution_enabled=bool(execution_enabled),
            alerts_enabled=bool(alerts_enabled),
            validity=normalized_validity,
            proposal_id=proposal_id,
            analysis_id=analysis_id,
            expires_at=expires_at,
        )
    )
    alert = alert_repository.get_alert(user_sub, int(created["alert_id"]))
    projection_status = _sync_projection(request.app, "upsert", alert) if alert else "pending"
    return {"condition": jsonable_encoder(created), "projectionStatus": projection_status}


def _repository_from_app(app: Any):
    existing = getattr(app.state, "trade_condition_repository", None)
    if existing is not None:
        return existing
    if not auth_is_enabled() and not _database_configured():
        repository = InMemoryTradeConditionRepository(alert_repository_from_app(app))
    else:
        if not _database_configured():
            raise HTTPException(status_code=503, detail="database settings are required for trade conditions")
        repository = PostgresTradeConditionRepository.from_env()
    app.state.trade_condition_repository = repository
    return repository


def _agent_report_for_user(app: Any, analysis_id: str, user_sub: str) -> dict[str, Any]:
    provider = getattr(app.state, "trade_condition_report_provider", None)
    if callable(provider):
        report = provider(analysis_id, user_sub)
        return report if isinstance(report, dict) else {}
    return get_agent_report(analysis_id, user_id=user_sub)


def _positive_decimal(value: Any, field_name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=422, detail=f"{field_name} must be a positive decimal")
    if parsed <= 0:
        raise HTTPException(status_code=422, detail=f"{field_name} must be a positive decimal")
    return parsed


def _normalize_validity(value: Any) -> str:
    normalized = str(value or "DAY").strip().upper()
    if normalized in {"DAY", "당일"}:
        return "DAY"
    if normalized in {"GTC", "직접 취소 전", "취소 전까지"}:
        return "GTC"
    raise HTTPException(status_code=422, detail="validity must be DAY or GTC")


def _commands_enabled() -> bool:
    return os.getenv("TRADE_CONDITION_COMMANDS_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def _database_configured() -> bool:
    return bool(
        os.getenv("DATABASE_URL")
        or (
            os.getenv("DATABASE_HOST")
            and os.getenv("DATABASE_NAME")
            and os.getenv("DATABASE_USER")
            and os.getenv("DATABASE_PASSWORD")
        )
    )
