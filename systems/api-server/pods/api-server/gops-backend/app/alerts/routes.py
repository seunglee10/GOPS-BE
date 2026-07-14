from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketState

from app.alerts.commands import AlertCommandDraftStore, resolve_alert_command
from app.alerts.notifications import RedisNotificationBroker
from app.alerts.preferences import (
    MAX_COMPANY_OVERRIDES,
    NOTIFICATION_SETTING_KEYS,
    NOTIFICATION_THRESHOLD_ALLOWED_VALUES,
    InMemoryNotificationPreferenceRepository,
    PostgresNotificationPreferenceRepository,
    preference_response,
)
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
from app.services.agent_gateway import request_agent_alert_resolution


router = APIRouter(tags=["alerts"])

DEFAULT_EXPIRES_DAYS = 90
ALERT_TYPES = {"price_cross", "spike", "volume_absolute", "volume_relative", "rsi_threshold"}
ALERT_CONDITION_KINDS = {"price_cross", "price_change", "volume_absolute", "volume_relative", "rsi_threshold"}
ALERT_DIRECTIONS = {"above", "below"}
ALERT_CHANGE_DIRECTIONS = {"above", "below", "either"}
ALERT_INTERVALS = {"1m", "5m", "10m", "1h", "4h", "1D"}
USER_MUTABLE_STATUSES = {"active", "disabled"}
VALID_REPEAT_LIMITS = {1, 3, 5, 10}
ALERT_CREATED_VIA = {"manual", "chart", "ai_coach", "agent_chat", "trade_condition"}


class AlertConditionBody(BaseModel):
    kind: str = Field(min_length=1, max_length=32)
    operator: str = Field(min_length=1, max_length=16)
    threshold: Decimal
    interval: str | None = Field(default=None, max_length=8)
    windowMin: int | None = None
    lookback: int | None = None
    period: int | None = None


class AlertCreateBody(BaseModel):
    symbol: str = Field(min_length=1, max_length=12)
    type: str | None = None
    condition: AlertConditionBody | None = None
    targetPrice: Decimal | None = None
    changePct: Decimal | None = None
    windowMin: int | None = None
    direction: str | None = None
    repeat: bool | None = None
    repeatLimit: int | None = None
    expiresAt: datetime | None = None
    proposalSource: Literal["daily_trade", "entry_habit", "exit_habit", "portfolio_risk"] | None = None
    createdVia: Literal["manual", "chart", "ai_coach", "agent_chat", "trade_condition"] | None = None
    requestId: str | None = Field(default=None, min_length=1, max_length=128)


class AlertStatusBody(BaseModel):
    status: str


class AlertCommandBody(BaseModel):
    text: str = Field(min_length=1, max_length=2_000)
    contextSymbol: str | None = Field(default=None, max_length=12)
    contextInterval: str | None = Field(default=None, max_length=8)
    clarificationId: str | None = Field(default=None, max_length=64)


class NotificationPreferencesPatchBody(BaseModel):
    settings: dict[str, bool] | None = None
    thresholds: dict[str, Any] | None = None
    companyOverrides: dict[str, bool] | None = None


@router.post("/api/alerts", status_code=status.HTTP_201_CREATED)
def create_alert(
    body: AlertCreateBody,
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    repository = _repository_from_app(request.app)
    request_id = _request_id(body, request)
    if request_id:
        existing = repository.get_alert_by_request_id(user.sub, request_id)
        if existing is not None:
            return {
                "alert": jsonable_encoder(existing),
                "projectionStatus": "synced",
                "idempotentReplay": True,
            }
    if repository.active_alert_count(user.sub) >= ACTIVE_ALERT_LIMIT:
        raise HTTPException(status_code=409, detail=f"활성 알림은 최대 {ACTIVE_ALERT_LIMIT}개까지 등록할 수 있습니다.")

    symbol = normalize_market_symbol(body.symbol)
    alert_type, direction, target_price, change_pct, window_min, condition = _normalize_alert_condition(
        body,
        request.app,
        symbol,
    )

    repeat_limit = _resolve_repeat_limit(body)
    expires_at = _resolve_expires_at(body.expiresAt, repeat_limit)
    created_via = body.createdVia or ("ai_coach" if body.proposalSource else "manual")
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
            condition=condition,
            created_via=created_via,
            request_id=request_id,
            expires_at=expires_at,
        )
    )
    projection_status = _sync_projection(request.app, "upsert", alert)
    return {
        "alert": jsonable_encoder(alert),
        "projectionStatus": projection_status,
        "idempotentReplay": False,
    }


@router.get("/api/alerts")
def list_alerts(
    request: Request,
    includeTerminal: bool = True,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    repository = _repository_from_app(request.app)
    alerts = repository.list_alerts(user.sub)
    if not includeTerminal:
        alerts = [item for item in alerts if item.get("status") in USER_MUTABLE_STATUSES]
    return {"alerts": jsonable_encoder(alerts)}


@router.post("/api/alerts/commands")
def create_alert_from_command(
    body: AlertCommandBody,
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    request_id = str(request.headers.get("Idempotency-Key") or "").strip()
    if not request_id:
        raise HTTPException(status_code=400, detail="Idempotency-Key가 필요합니다.")
    if len(request_id) > 128:
        raise HTTPException(status_code=400, detail="Idempotency-Key는 128자 이하여야 합니다.")

    repository = _repository_from_app(request.app)
    existing = repository.get_alert_by_request_id(user.sub, request_id)
    if existing is not None:
        return {"status": "created", "alert": jsonable_encoder(existing), "idempotentReplay": True}

    draft_store = _alert_command_store_from_app(request.app)
    previous_text = draft_store.consume(user.sub, body.clarificationId)
    combined_text = " ".join(part for part in (previous_text, body.text.strip()) if part)
    resolution = resolve_alert_command(
        combined_text,
        context_symbol=body.contextSymbol,
        context_interval=body.contextInterval,
    )
    if resolution.get("status") == "ai_fallback":
        try:
            resolution = request_agent_alert_resolution({
                "text": combined_text,
                "contextSymbol": resolution.get("symbol") or body.contextSymbol,
                "contextInterval": body.contextInterval,
            })
        except HTTPException:
            resolution = {
                "status": "clarify",
                "clarification": "알림 조건을 해석하지 못했습니다. 기업명, 조건값, 기준 시간을 한 문장으로 알려주세요.",
            }

    resolution_status = str(resolution.get("status") or "not_matched")
    if resolution_status == "not_matched":
        return {"status": "not_matched"}
    if resolution_status in {"clarify", "rejected"}:
        response = {
            "status": resolution_status,
            "clarification": str(resolution.get("clarification") or "알림 조건을 조금 더 구체적으로 알려주세요."),
        }
        if resolution_status == "clarify":
            response["clarificationId"] = draft_store.save(user.sub, combined_text)
        return response
    if resolution_status != "ready":
        return {"status": "not_matched"}

    try:
        create_body = AlertCreateBody(
            symbol=str(resolution.get("symbol") or body.contextSymbol or ""),
            condition=AlertConditionBody(**dict(resolution.get("condition") or {})),
            repeatLimit=resolution.get("repeatLimit", 1),
            expiresAt=resolution.get("expiresAt"),
            createdVia="agent_chat",
            requestId=request_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail="알림 조건을 생성 가능한 형식으로 해석하지 못했습니다.") from exc
    created = create_alert(create_body, request, user)
    return {
        "status": "created",
        "alert": created["alert"],
        "projectionStatus": created["projectionStatus"],
        "idempotentReplay": created["idempotentReplay"],
    }


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


@router.get("/api/notification-preferences")
def get_notification_preferences(
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    repository = _notification_preferences_repository_from_app(request.app)
    return preference_response(repository.get(user.sub))


@router.patch("/api/notification-preferences")
def patch_notification_preferences(
    body: NotificationPreferencesPatchBody,
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    settings = body.settings or {}
    unknown_keys = sorted(set(settings) - NOTIFICATION_SETTING_KEYS)
    if unknown_keys:
        raise HTTPException(status_code=422, detail=f"Unknown notification settings: {', '.join(unknown_keys)}")

    raw_thresholds = body.thresholds or {}
    unknown_thresholds = sorted(set(raw_thresholds) - NOTIFICATION_THRESHOLD_ALLOWED_VALUES.keys())
    if unknown_thresholds:
        raise HTTPException(status_code=400, detail=f"Unknown notification thresholds: {', '.join(unknown_thresholds)}")
    thresholds: dict[str, int] = {}
    for key, value in raw_thresholds.items():
        allowed_values = NOTIFICATION_THRESHOLD_ALLOWED_VALUES[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value or int(value) not in allowed_values:
            allowed_label = ", ".join(str(item) for item in sorted(allowed_values))
            raise HTTPException(status_code=400, detail=f"{key} must be one of: {allowed_label}")
        thresholds[key] = int(value)

    raw_company_overrides = body.companyOverrides or {}
    if len(raw_company_overrides) > MAX_COMPANY_OVERRIDES:
        raise HTTPException(status_code=422, detail=f"Company notification settings support up to {MAX_COMPANY_OVERRIDES} symbols.")
    company_overrides: dict[str, bool] = {}
    for symbol, enabled in raw_company_overrides.items():
        company_overrides[normalize_market_symbol(symbol)] = enabled

    if not settings and not thresholds and not company_overrides:
        raise HTTPException(status_code=422, detail="At least one notification preference is required.")

    repository = _notification_preferences_repository_from_app(request.app)
    existing = preference_response(repository.get(user.sub))
    if len({**existing["companyOverrides"], **company_overrides}) > MAX_COMPANY_OVERRIDES:
        raise HTTPException(status_code=422, detail=f"Company notification settings support up to {MAX_COMPANY_OVERRIDES} symbols.")
    row = repository.patch(
        user.sub,
        settings=settings,
        thresholds=thresholds,
        company_overrides=company_overrides,
    )
    return preference_response(row)


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


def _notification_preferences_repository_from_app(app: Any):
    existing = getattr(app.state, "notification_preferences_repository", None)
    if existing is not None:
        return existing
    repository_mode = "memory" if not auth_is_enabled() and not _database_configured() else "postgres"
    if os_mode := getattr(app.state, "alert_repository_mode", None):
        repository_mode = str(os_mode)
    if repository_mode == "memory":
        repository = InMemoryNotificationPreferenceRepository()
    else:
        if not _database_configured():
            raise HTTPException(status_code=503, detail="DATABASE_URL or DATABASE_* settings are required for notification preferences")
        repository = PostgresNotificationPreferenceRepository.from_env()
    app.state.notification_preferences_repository = repository
    return repository


def _alert_command_store_from_app(app: Any) -> AlertCommandDraftStore:
    existing = getattr(app.state, "alert_command_draft_store", None)
    if existing is not None:
        return existing
    try:
        broker = _notification_broker_from_app(app)
        redis_client = getattr(broker, "redis", None)
    except Exception:
        redis_client = None
    store = AlertCommandDraftStore(redis_client)
    app.state.alert_command_draft_store = store
    return store


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


def _normalize_alert_condition(
    body: AlertCreateBody,
    app: Any,
    symbol: str,
) -> tuple[str, str | None, Decimal | None, Decimal | None, int | None, dict[str, Any]]:
    if body.condition is None:
        return _normalize_legacy_alert_condition(body, app, symbol)

    raw = body.condition
    kind = raw.kind.strip().lower()
    operator = raw.operator.strip().lower()
    if kind not in ALERT_CONDITION_KINDS:
        raise HTTPException(status_code=422, detail="지원하지 않는 알림 조건입니다.")
    if kind == "price_change":
        if operator not in ALERT_CHANGE_DIRECTIONS:
            raise HTTPException(status_code=422, detail="가격 변동 방향은 above, below, either 중 하나여야 합니다.")
    elif operator not in ALERT_DIRECTIONS:
        raise HTTPException(status_code=422, detail="조건 방향은 above 또는 below여야 합니다.")

    threshold = _positive_decimal(raw.threshold, "기준값")
    condition: dict[str, Any] = {
        "kind": kind,
        "operator": operator,
        "threshold": threshold,
    }
    direction = None if operator == "either" else operator
    target_price: Decimal | None = None
    change_pct: Decimal | None = None
    window_min: int | None = None

    if kind == "price_cross":
        current_price = _resolve_current_price(app, symbol)
        if threshold == current_price:
            raise HTTPException(status_code=422, detail="목표가가 현재가와 같으면 알림 조건을 만들 수 없습니다.")
        target_price = threshold
        alert_type = "price_cross"
    elif kind == "price_change":
        window_min = raw.windowMin
        if window_min is None or window_min < 1 or window_min > 240:
            raise HTTPException(status_code=422, detail="가격 변동 비교 기간은 1분 이상 240분 이하로 입력해주세요.")
        change_pct = threshold
        condition["windowMin"] = int(window_min)
        alert_type = "spike"
    elif kind == "volume_absolute":
        interval = _alert_interval(raw.interval, required=True)
        condition["interval"] = interval
        alert_type = kind
    elif kind == "volume_relative":
        interval = _alert_interval(raw.interval, required=True)
        lookback = raw.lookback if raw.lookback is not None else 20
        if lookback < 5 or lookback > 200:
            raise HTTPException(status_code=422, detail="거래량 평균 비교 구간은 5개 이상 200개 이하의 봉이어야 합니다.")
        if threshold > 20:
            raise HTTPException(status_code=422, detail="거래량 평균 배수는 20배 이하여야 합니다.")
        condition.update({"interval": interval, "lookback": int(lookback)})
        alert_type = kind
    else:
        interval = _alert_interval(raw.interval or "1D", required=True)
        period = raw.period if raw.period is not None else 14
        if period < 2 or period > 100:
            raise HTTPException(status_code=422, detail="RSI 기간은 2 이상 100 이하로 입력해주세요.")
        if threshold >= 100:
            raise HTTPException(status_code=422, detail="RSI 기준값은 0보다 크고 100보다 작아야 합니다.")
        condition.update({"interval": interval, "period": int(period)})
        alert_type = kind

    return alert_type, direction, target_price, change_pct, window_min, _json_condition(condition)


def _normalize_legacy_alert_condition(
    body: AlertCreateBody,
    app: Any,
    symbol: str,
) -> tuple[str, str | None, Decimal | None, Decimal | None, int | None, dict[str, Any]]:
    alert_type = str(body.type or "").strip().lower()
    if alert_type not in {"price_cross", "spike"}:
        raise HTTPException(status_code=422, detail="알림 조건은 목표가 또는 급등락만 선택할 수 있습니다.")

    direction: str | None = None
    target_price: Decimal | None = None
    change_pct: Decimal | None = None
    window_min: int | None = None
    if alert_type == "price_cross":
        target_price = _positive_decimal(body.targetPrice, "목표가")
        current_price = _resolve_current_price(app, symbol)
        if target_price == current_price:
            raise HTTPException(status_code=422, detail="목표가가 현재가와 같으면 알림 조건을 만들 수 없습니다.")
        direction = "above" if target_price > current_price else "below"
        condition = {"kind": "price_cross", "operator": direction, "threshold": target_price}
    else:
        change_pct = _positive_decimal(body.changePct, "변동률")
        if body.windowMin is None or body.windowMin < 1 or body.windowMin > 240:
            raise HTTPException(status_code=422, detail="비교 기간은 1분 이상 240분 이하로 입력해주세요.")
        window_min = int(body.windowMin)
        if body.direction is not None:
            direction = body.direction.strip().lower()
            if direction not in ALERT_DIRECTIONS:
                raise HTTPException(status_code=422, detail="방향은 급등 또는 급락만 선택할 수 있습니다.")
        condition = {
            "kind": "price_change",
            "operator": direction or "either",
            "threshold": change_pct,
            "windowMin": window_min,
        }
    return alert_type, direction, target_price, change_pct, window_min, _json_condition(condition)


def _alert_interval(value: str | None, *, required: bool) -> str | None:
    interval = str(value or "").strip()
    if required and not interval:
        raise HTTPException(status_code=422, detail="거래량·지표 알림에는 봉 간격이 필요합니다.")
    normalized = "1D" if interval.lower() == "1d" else interval.lower()
    if normalized not in ALERT_INTERVALS:
        allowed = ", ".join(sorted(ALERT_INTERVALS))
        raise HTTPException(status_code=422, detail=f"봉 간격은 다음 중 하나여야 합니다: {allowed}")
    return normalized


def _json_condition(condition: dict[str, Any]) -> dict[str, Any]:
    return {
        key: float(value) if isinstance(value, Decimal) else value
        for key, value in condition.items()
    }


def _request_id(body: AlertCreateBody, request: Request) -> str | None:
    header_value = str(request.headers.get("Idempotency-Key") or "").strip()
    body_value = str(body.requestId or "").strip()
    if header_value and body_value and header_value != body_value:
        raise HTTPException(status_code=400, detail="Idempotency-Key와 requestId가 일치해야 합니다.")
    value = header_value or body_value
    if len(value) > 128:
        raise HTTPException(status_code=400, detail="Idempotency-Key는 128자 이하여야 합니다.")
    return value or None


def _resolve_expires_at(value: datetime | None, repeat_limit: int | None) -> datetime | None:
    if value is not None:
        resolved = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        resolved = resolved.astimezone(timezone.utc)
        if resolved <= datetime.now(timezone.utc):
            raise HTTPException(status_code=422, detail="유효기간은 현재 이후여야 합니다.")
        return resolved
    if repeat_limit is None:
        return None
    return datetime.now(timezone.utc) + timedelta(days=DEFAULT_EXPIRES_DAYS)


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
