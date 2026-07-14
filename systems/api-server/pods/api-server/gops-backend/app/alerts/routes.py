from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketState

from app.alerts.notifications import RedisNotificationBroker
from app.alerts.projection import AlertProjectionError, RedisAlertProjection
from app.alerts.repository import ACTIVE_ALERT_LIMIT, AlertCreate, InMemoryAlertRepository, PostgresAlertRepository
from app.auth.dependencies import (
    WebSocketAuthRequired,
    WebSocketAuthUnavailable,
    auth_is_enabled,
    require_current_user,
    require_websocket_user,
)
from app.auth.models import AuthenticatedUser
from app.services.alfaka_market_data import normalize_market_symbol, resolve_latest_trade_price


router = APIRouter(tags=["alerts"])

DEFAULT_EXPIRES_DAYS = 90
ALERT_TYPES = {"price_cross", "spike"}
ALERT_DIRECTIONS = {"above", "below"}
USER_MUTABLE_STATUSES = {"active", "disabled"}
VALID_REPEAT_LIMITS = {1, 3, 5, 10}


class AlertCreateBody(BaseModel):
    symbol: str = Field(min_length=1, max_length=12)
    type: str
    targetPrice: Decimal | None = None
    changePct: Decimal | None = None
    windowMin: int | None = None
    direction: str | None = None
    repeat: bool | None = None
    repeatLimit: int | None = None
    expiresAt: datetime | None = None
    proposalSource: Literal["daily_trade", "entry_habit", "exit_habit", "portfolio_risk"] | None = None


class AlertStatusBody(BaseModel):
    status: str


@router.post("/api/alerts", status_code=status.HTTP_201_CREATED)
def create_alert(
    body: AlertCreateBody,
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    repository = _repository_from_app(request.app)
    if repository.active_alert_count(user.sub) >= ACTIVE_ALERT_LIMIT:
        raise HTTPException(status_code=409, detail=f"활성 알림은 최대 {ACTIVE_ALERT_LIMIT}개까지 등록할 수 있습니다.")

    symbol = normalize_market_symbol(body.symbol)
    alert_type = body.type.strip().lower()
    if alert_type not in ALERT_TYPES:
        raise HTTPException(status_code=422, detail="알림 조건은 목표가 또는 급등락만 선택할 수 있습니다.")

    direction: str | None = None
    target_price: Decimal | None = None
    change_pct: Decimal | None = None
    window_min: int | None = None

    if alert_type == "price_cross":
        target_price = _positive_decimal(body.targetPrice, "목표가")
        current_price = _resolve_current_price(request.app, symbol)
        if target_price == current_price:
            raise HTTPException(status_code=422, detail="목표가가 현재가와 같으면 알림 조건을 만들 수 없습니다.")
        direction = "above" if target_price > current_price else "below"
    else:
        change_pct = _positive_decimal(body.changePct, "변동률")
        if body.windowMin is None or body.windowMin < 1 or body.windowMin > 240:
            raise HTTPException(status_code=422, detail="비교 기간은 1분 이상 240분 이하로 입력해주세요.")
        window_min = int(body.windowMin)
        if body.direction is not None:
            direction = body.direction.strip().lower()
            if direction not in ALERT_DIRECTIONS:
                raise HTTPException(status_code=422, detail="방향은 급등 또는 급락만 선택할 수 있습니다.")

    repeat_limit = _resolve_repeat_limit(body)
    expires_at = body.expiresAt or datetime.now(timezone.utc) + timedelta(days=DEFAULT_EXPIRES_DAYS)
    alert = repository.create_alert(
        AlertCreate(
            user_sub=user.sub,
            symbol=symbol,
            type=alert_type,
            direction=direction,
            target_price=target_price,
            change_pct=change_pct,
            window_min=window_min,
            repeat=repeat_limit is None or repeat_limit > 1,
            repeat_limit=repeat_limit,
            proposal_source=body.proposalSource,
            expires_at=expires_at,
        )
    )
    projection_status = _sync_projection(request.app, "upsert", alert)
    return {"alert": jsonable_encoder(alert), "projectionStatus": projection_status}


@router.get("/api/alerts")
def list_alerts(request: Request, user: AuthenticatedUser = Depends(require_current_user)) -> dict[str, Any]:
    repository = _repository_from_app(request.app)
    return {"alerts": jsonable_encoder(repository.list_alerts(user.sub))}


@router.delete("/api/alerts")
def delete_alerts(
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    repository = _repository_from_app(request.app)
    deleted_alerts = repository.delete_alerts(user.sub)
    projection_status = "synced"
    for alert in deleted_alerts:
        if _sync_projection(request.app, "delete", alert) != "synced":
            projection_status = "pending"
    return {"deleted": len(deleted_alerts), "projectionStatus": projection_status}


@router.patch("/api/alerts/{alert_id}")
def update_alert_status(
    alert_id: int,
    body: AlertStatusBody,
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    status_value = body.status.strip().lower()
    if status_value not in USER_MUTABLE_STATUSES:
        raise HTTPException(status_code=422, detail="status must be active or disabled")
    repository = _repository_from_app(request.app)
    alert = repository.update_alert_status(user.sub, alert_id, status_value)
    if alert is None:
        raise HTTPException(status_code=404, detail="alert not found")
    projection_status = _sync_projection(request.app, "upsert" if status_value == "active" else "delete", alert)
    return {"alert": jsonable_encoder(alert), "projectionStatus": projection_status}


@router.delete("/api/alerts/{alert_id}")
def delete_alert(
    alert_id: int,
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    repository = _repository_from_app(request.app)
    alert = repository.delete_alert(user.sub, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="alert not found")
    projection_status = _sync_projection(request.app, "delete", alert)
    return {"deleted": True, "alert": jsonable_encoder(alert), "projectionStatus": projection_status}


@router.get("/api/notifications")
def list_notifications(
    request: Request,
    after: int | None = None,
    limit: int = 50,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    repository = _repository_from_app(request.app)
    normalized_limit = min(max(limit, 1), 100)
    return {
        "notifications": jsonable_encoder(
            repository.list_notifications(user.sub, after=after, limit=normalized_limit)
        ),
        "unreadCount": repository.unread_count(user.sub),
    }


@router.get("/api/notifications/unread-count")
def notification_unread_count(request: Request, user: AuthenticatedUser = Depends(require_current_user)) -> dict[str, Any]:
    repository = _repository_from_app(request.app)
    return {"unreadCount": repository.unread_count(user.sub)}


@router.patch("/api/notifications/read-all")
def mark_all_notifications_read(
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    repository = _repository_from_app(request.app)
    return {"updated": repository.mark_all_notifications_read(user.sub)}


@router.patch("/api/notifications/{notification_id}/read")
def mark_notification_read(
    notification_id: int,
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    repository = _repository_from_app(request.app)
    notification = repository.mark_notification_read(user.sub, notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="notification not found")
    return {"notification": jsonable_encoder(notification)}


@router.delete("/api/notifications/{notification_id}")
def delete_notification(
    notification_id: int,
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    repository = _repository_from_app(request.app)
    notification = repository.delete_notification(user.sub, notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="읽은 알림만 삭제할 수 있습니다.")
    return {
        "deleted": True,
        "notification": jsonable_encoder(notification),
        "unreadCount": repository.unread_count(user.sub),
    }


@router.websocket("/ws/notifications")
async def notification_socket(websocket: WebSocket) -> None:
    try:
        user = require_websocket_user(websocket)
    except WebSocketAuthRequired as exc:
        await websocket.accept()
        await websocket.send_json({"type": "error", "detail": str(exc)})
        await websocket.close(code=1008)
        return
    except WebSocketAuthUnavailable as exc:
        await websocket.accept()
        await websocket.send_json({"type": "error", "detail": str(exc)})
        await websocket.close(code=1011)
        return

    await websocket.accept()
    try:
        repository = _repository_from_app(websocket.app)
        await websocket.send_json(
            {
                "type": "snapshot",
                "notifications": jsonable_encoder(repository.list_notifications(user.sub, limit=20)),
                "unreadCount": repository.unread_count(user.sub),
            }
        )
        broker = _notification_broker_from_app(websocket.app)
        async for payload in broker.listen_user(user.sub):
            await websocket.send_json(jsonable_encoder(payload))
    except WebSocketDisconnect:
        return
    except Exception as exc:
        try:
            if websocket.client_state != WebSocketState.DISCONNECTED:
                await websocket.send_json({"type": "error", "detail": str(exc)})
                await websocket.close(code=1011)
        except Exception:
            return


def _repository_from_app(app: Any):
    existing = getattr(app.state, "alert_repository", None)
    if existing is not None:
        return existing
    repository_mode = "memory" if not auth_is_enabled() and not _database_configured() else "postgres"
    if os_mode := getattr(app.state, "alert_repository_mode", None):
        repository_mode = str(os_mode)
    if repository_mode == "memory":
        repository = InMemoryAlertRepository()
    else:
        if not _database_configured():
            raise HTTPException(status_code=503, detail="DATABASE_URL or DATABASE_* settings are required for alert API")
        repository = PostgresAlertRepository.from_env()
    app.state.alert_repository = repository
    return repository


def _projection_from_app(app: Any):
    existing = getattr(app.state, "alert_projection", None)
    if existing is not None:
        return existing
    projection = RedisAlertProjection.from_env()
    app.state.alert_projection = projection
    return projection


def _notification_broker_from_app(app: Any):
    existing = getattr(app.state, "alert_notification_broker", None)
    if existing is not None:
        return existing
    broker = RedisNotificationBroker.from_env()
    app.state.alert_notification_broker = broker
    return broker


def _sync_projection(app: Any, action: str, alert: dict[str, Any]) -> str:
    try:
        projection = _projection_from_app(app)
        if action == "delete":
            projection.delete_alert(alert["id"], symbol=alert.get("symbol"))
        else:
            projection.upsert_alert(alert)
        return "synced"
    except (AlertProjectionError, Exception):
        return "pending"


def _resolve_current_price(app: Any, symbol: str) -> Decimal:
    provider = getattr(app.state, "alert_price_provider", None)
    payload = provider(symbol) if callable(provider) else resolve_latest_trade_price(symbol)
    value = payload.get("price") if isinstance(payload, dict) else payload
    price = _decimal_or_none(value)
    if price is None:
        raise HTTPException(
            status_code=503,
            detail=f"{symbol} 현재가를 확인할 수 없어 목표가 알림을 등록하지 못했습니다. 잠시 후 다시 시도해주세요.",
        )
    return price


def _positive_decimal(value: Decimal | None, field_name: str) -> Decimal:
    parsed = _decimal_or_none(value)
    if parsed is None or parsed <= 0:
        raise HTTPException(status_code=422, detail=f"{field_name}는 0보다 큰 숫자로 입력해주세요.")
    return parsed


def _resolve_repeat_limit(body: AlertCreateBody) -> int | None:
    fields_set = getattr(body, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(body, "__fields_set__", set())
    if "repeatLimit" in fields_set:
        if body.repeatLimit is None:
            return None
        if body.repeatLimit not in VALID_REPEAT_LIMITS:
            raise HTTPException(status_code=422, detail="재알림 방식은 한 번, 매번, 최대 3회, 최대 5회, 최대 10회 중에서 선택해주세요.")
        return int(body.repeatLimit)
    if body.repeat is True:
        return None
    return 1


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _database_configured() -> bool:
    import os

    return bool(
        os.getenv("DATABASE_URL")
        or (
            os.getenv("DATABASE_HOST")
            and os.getenv("DATABASE_NAME")
            and os.getenv("DATABASE_USER")
            and os.getenv("DATABASE_PASSWORD")
        )
    )
