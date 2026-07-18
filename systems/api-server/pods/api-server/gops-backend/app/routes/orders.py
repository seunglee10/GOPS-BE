import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.encoders import jsonable_encoder
from starlette.websockets import WebSocketState

from app.auth.dependencies import (
    auth_is_enabled,
    require_current_user,
    require_websocket_user,
    WebSocketAuthRequired,
    WebSocketAuthUnavailable,
)
from app.auth.models import AuthenticatedUser
from app.routes.paper_trading import paper_repository_from_app
from app.routes.simulator import simulator_gateway_from_app, simulator_mode_active
from app.services.risk_context import build_risk_context, risk_pretrade_enabled
from app.services.risk_settings import (
    ADJUSTABLE_FIELDS,
    GUARDRAILS,
    PRESETS,
    RiskSettingsError,
    budget_tracker_from_app,
    serialize_values,
    settings_store_from_app,
)
from kis_trader.domain.commands import validate_order_request_payload
from kis_trader.risk import PretradeVerdict, evaluate_pretrade, load_risk_config
from kis_trader.domain.envelope import build_order_command_envelope, validate_order_envelope
from kis_trader.domain.status import CANONICAL_STATUSES, OrderContractError
from kis_trader.domain.topics import CANONICAL_ORDER_TOPICS
from kis_trader.kis.client import DemoKisHttpClient
from kis_trader.kis.fake import KisConnectionReset, KisExplicitReject, KisHttpError, KisTimeout, KisTokenExpired
from kis_trader.persistence.memory import InMemoryOrderRepository
from kis_trader.persistence.postgres import PostgresOrderRepository
from kis_trader.persistence.repository import IdempotencyConflictError, OrderRepository
from kis_trader.security.idempotency import hash_idempotency_key, stable_body_hash
from kis_trader.security.validation import FORBIDDEN_FIELD_NAMES, assert_no_forbidden_fields


router = APIRouter(tags=["orders"])

ORDER_CONTRACT = {
    "version": "orders.v1",
    "environment": "demo",
    "submit": {
        "method": "POST",
        "path": "/api/orders",
        "required_headers": ["Idempotency-Key"],
        "required_fields": ["market", "symbol", "side", "qty", "exchange"],
        "optional_fields": [
            "account_alias",
            "order_division",
            "order_type",
            "price",
            "actor_id",
            "role",
            "risk_acknowledged",
        ],
        "accepted_values": {
            "market": ["overseas"],
            "side": ["buy", "sell"],
            "order_division": ["00"],
            "order_type": ["limit", "market (SIM only)"],
        },
    },
    "statuses": list(CANONICAL_STATUSES),
    "topics": list(CANONICAL_ORDER_TOPICS),
    "events": {
        "history": "GET /api/orders/{order_id}/events",
        "websocket": "/ws/orders/{order_id}",
    },
    "balance": "GET /api/orders/balance",
    "forbidden_fields": sorted(FORBIDDEN_FIELD_NAMES),
}


@router.get("/api/order-contract")
def order_contract() -> dict[str, Any]:
    return ORDER_CONTRACT


@router.post("/api/orders", status_code=status.HTTP_202_ACCEPTED)
async def create_order(
    request: Request,
    response: Response,
    current_user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    idempotency_key = request.headers.get("Idempotency-Key")
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")

    payload = await _json_body(request)
    _validate_no_forbidden_fields(payload)
    if auth_is_enabled():
        payload = {
            **payload,
            "actor_id": current_user.email,
            "role": payload.get("role") or "trader",
        }
    simulator_mode = simulator_mode_active(request.app)
    simulator_quote: dict[str, Any] | None = None
    risk_acknowledged = payload.get("risk_acknowledged") is True
    order_type = str(payload.get("order_type") or "limit").strip().lower()
    limit_price: float | None = None
    validation_payload = dict(payload)
    if simulator_mode:
        if order_type not in {"market", "limit"}:
            raise HTTPException(status_code=422, detail="order_type must be market or limit in simulation mode")
        if order_type == "market":
            side = str(payload.get("side") or "").strip().lower()
            if side not in {"buy", "sell"}:
                raise HTTPException(status_code=422, detail="side must be buy or sell")
            try:
                simulator_quote = simulator_gateway_from_app(request.app).quote(str(payload.get("symbol") or "").upper())
                market_price = simulator_quote.get("ask") if side == "buy" else simulator_quote.get("bid")
                validation_payload["price"] = str(float(market_price))
            except Exception as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        else:
            try:
                limit_price = float(payload.get("price"))
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="price must be positive for a limit order") from exc
            if limit_price <= 0:
                raise HTTPException(status_code=422, detail="price must be positive for a limit order")
            try:
                simulator_quote = simulator_gateway_from_app(request.app).quote(str(payload.get("symbol") or "").upper())
            except Exception:
                simulator_quote = None
        validation_payload.setdefault("order_division", "00")
    else:
        if order_type != "limit":
            raise HTTPException(status_code=422, detail="LIVE KIS orders support limit orders only")
    order_request = _validate_order_request(validation_payload)
    repository = None
    idempotency_key_hash = None
    body_hash = None
    if simulator_mode:
        repository = paper_repository_from_app(request.app)
        idempotency_key_hash = hash_idempotency_key(_scoped_idempotency_key(idempotency_key.strip(), current_user))
        body_hash = stable_body_hash(payload)
    else:
        repository = _repository_from_app(request.app)
        idempotency_key_hash = hash_idempotency_key(_scoped_idempotency_key(idempotency_key.strip(), current_user))
        body_hash = stable_body_hash(payload)
        # 멱등 재시도는 이미 접수된 주문의 결과 재조회 — 리스크 판정을 다시 태우지 않는다.
        replay = _find_idempotent_response(repository, idempotency_key_hash, body_hash)
        if replay is not None:
            response.headers["X-Idempotent-Replay"] = "true"
            return jsonable_encoder({**replay, "idempotent_replay": True})

    verdict = _risk_verdict(request.app, current_user.sub, order_request)
    if verdict is not None and verdict.verdict != "allow" and not risk_acknowledged:
        # API clients must explicitly acknowledge a non-allow verdict. The UI
        # keeps the original quantity and sends this acknowledgement only when
        # the user chooses to continue after seeing the risk explanation.
        raise HTTPException(
            status_code=422,
            detail={"reason": "risk rejected", "risk": verdict.to_dict()},
        )
    if simulator_mode:
        try:
            status_payload = simulator_gateway_from_app(request.app).status()
            run_id = str(status_payload.get("runId") or "")
            if not run_id:
                raise ValueError("simulation run is unavailable")
            submitted_sequence = int(
                (simulator_quote or {}).get("sequence")
                or status_payload.get("processedEventCount")
                or 0
            )
            virtual_time = str(
                (simulator_quote or {}).get("virtualTime")
                or status_payload.get("virtualTime")
                or ""
            ) or None
            created = repository.create_order(
                user_id=current_user.sub,
                idempotency_key_hash=idempotency_key_hash,
                body_hash=body_hash,
                request=order_request,
                execution_mode="simulation",
                simulation_run_id=run_id,
                simulation_submitted_sequence=submitted_sequence,
                virtual_submitted_at=virtual_time,
                order_type=order_type,
            )
            order = created.order
            if simulator_quote is not None and order_type == "market":
                matches = repository.match_quote(
                    symbol=order_request.symbol,
                    bid_price=simulator_quote.get("bid"),
                    ask_price=simulator_quote.get("ask"),
                    quote_timestamp=virtual_time,
                    quote_event_id=f"simulation:{run_id}:{submitted_sequence}",
                    execution_mode="simulation",
                    simulation_run_id=run_id,
                    quote_sequence=submitted_sequence,
                    virtual_timestamp=virtual_time,
                )
                matched = next((item for item in matches if item.get("order_id") == order.get("order_id")), None)
                if matched is not None:
                    order = matched
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        _record_buy_spend(request.app, current_user.sub, order_request)
        response_order = _simulation_order_response(order)
        if created.idempotent_replay:
            response.headers["X-Idempotent-Replay"] = "true"
        return jsonable_encoder(_with_risk(response_order, verdict))
    envelope = build_order_command_envelope(
        order_request,
        occurred_at=datetime.now(timezone.utc).isoformat(),
    )
    _validate_no_forbidden_fields(envelope)
    command = _validate_order_envelope(envelope)

    try:
        result = repository.create_received_order(
            idempotency_key_hash=idempotency_key_hash,
            body_hash=body_hash,
            command=command,
            user_sub=current_user.sub,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if result.idempotent_replay:
        response.headers["X-Idempotent-Replay"] = "true"
    else:
        _record_buy_spend(request.app, current_user.sub, order_request)
    return jsonable_encoder(_with_risk(dict(result.response), verdict))


@router.get("/api/risk/settings")
def get_risk_settings(
    request: Request,
    current_user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    """사용자 리스크 설정 조회 — active(디폴트 병합)·pending·메타."""
    store = settings_store_from_app(request.app)
    state = store.resolve(current_user.sub)
    return {
        "active": serialize_values(store.active_values(current_user.sub)),
        "pending": state.get("pending"),
        "history": (state.get("history") or [])[:10],
        "meta": {
            "adjustable": sorted(ADJUSTABLE_FIELDS),
            "guardrails": {
                key: {"min": str(minimum), "max": (str(maximum) if maximum is not None else None)}
                for key, (minimum, maximum) in GUARDRAILS.items()
            },
            "presets": {name: serialize_values(dict(values)) for name, values in PRESETS.items()},
            "policy": "조이는 변경은 즉시, 푸는 변경은 다음 날부터 적용됩니다.",
        },
    }


@router.patch("/api/risk/settings")
async def patch_risk_settings(
    request: Request,
    current_user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    """사용자 리스크 설정 변경 — {preset} 또는 {values:{...}}. 비대칭 적용."""
    payload = await _json_body(request)
    preset = payload.get("preset")
    values = payload.get("values")
    if values is not None and not isinstance(values, dict):
        raise HTTPException(status_code=422, detail="values must be an object")
    store = settings_store_from_app(request.app)
    try:
        result = store.apply_change(
            current_user.sub,
            values=values,
            preset=(str(preset) if preset is not None else None),
        )
    except RiskSettingsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "appliedNow": serialize_values(result.applied_now),
        "scheduled": serialize_values(result.scheduled),
        "effectiveDate": result.effective_date,
        "active": serialize_values(store.active_values(current_user.sub)),
    }


@router.get("/api/risk/report")
def risk_daily_report(
    request: Request,
    date: str | None = None,
    _user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    """오늘(또는 지정일) 발동한 리스크 룰 이력 요약 — 장마감 리포트 리스크 섹션."""
    day = (date or datetime.now(timezone.utc).strftime("%Y-%m-%d"))[:10]
    client = _risk_report_redis(request.app)
    if client is None:
        raise HTTPException(status_code=503, detail="risk report storage unavailable (REDIS_URL)")
    try:
        rows = client.lrange(f"agent.alerts:log:{day}", 0, 499)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"risk report read failed: {exc}") from exc
    events = []
    for row in rows or []:
        if isinstance(row, bytes):
            row = row.decode("utf-8")
        try:
            decoded = json.loads(row)
        except (ValueError, TypeError):
            continue
        if isinstance(decoded, dict):
            events.append(decoded)
    by_type: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for event in events:
        event_type = str(event.get("eventType") or "unknown")
        severity = str(event.get("severity") or "info")
        by_type[event_type] = by_type.get(event_type, 0) + 1
        by_severity[severity] = by_severity.get(severity, 0) + 1
    return {
        "date": day,
        "totalEvents": len(events),
        "byType": by_type,
        "bySeverity": by_severity,
        "events": [
            {
                "eventId": event.get("eventId"),
                "symbol": event.get("symbol"),
                "eventType": event.get("eventType"),
                "severity": event.get("severity"),
                "observedAt": event.get("observedAt"),
                "summary": event.get("summary"),
            }
            for event in events[:50]
        ],
    }


@router.post("/api/risk/pretrade")
async def risk_pretrade_preview(
    request: Request,
    current_user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    """Advisory pre-trade risk verdict for the order ticket (no order created)."""
    payload = await _json_body(request)
    _validate_no_forbidden_fields(payload)
    order_request = _validate_order_request(payload)
    verdict = _risk_verdict(request.app, current_user.sub, order_request, force=True)
    if verdict is None:
        raise HTTPException(status_code=503, detail="risk pretrade preview is disabled")
    return {
        "symbol": order_request.symbol,
        "side": order_request.side,
        "risk": verdict.to_dict(),
    }


@router.get("/api/orders/balance")
def get_order_balance(
    request: Request,
    symbol: str = "AAPL",
    exchange: str = "NASD",
    price: str = "0",
    _user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    if simulator_mode_active(request.app):
        try:
            snapshot = paper_repository_from_app(request.app).account_snapshot(_user.sub)
            account = snapshot.get("account") if isinstance(snapshot, dict) else {}
            cash = account.get("available_cash", 0) if isinstance(account, dict) else 0
            numeric_price = float(price or 0)
            return {
                "env": "simulation",
                "market": "overseas",
                "symbol": symbol.upper(),
                "exchange": exchange.upper(),
                "currency": "USD",
                "orderable_cash": str(cash),
                "orderable_qty": str(int(float(cash) / numeric_price)) if numeric_price > 0 else None,
            }
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    try:
        balance = DemoKisHttpClient.from_env().fetch_orderable_cash(
            symbol=symbol,
            exchange=exchange,
            price=price,
        )
    except KisExplicitReject as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (KisTimeout, KisConnectionReset, KisTokenExpired, KisHttpError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return jsonable_encoder(balance)


@router.get("/api/orders/{order_id}")
def get_order(
    order_id: str,
    request: Request,
    current_user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    if simulator_mode_active(request.app):
        order = paper_repository_from_app(request.app).get_order(current_user.sub, order_id)
        if order is None or order.get("execution_mode") != "simulation":
            raise HTTPException(status_code=404, detail="order not found")
        return jsonable_encoder(_simulation_order_response(order))
    repository = _repository_from_app(request.app)
    order = _get_owned_order(repository, order_id, current_user.sub)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return jsonable_encoder(order)


@router.get("/api/orders/{order_id}/events")
def get_order_events(
    order_id: str,
    request: Request,
    current_user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    if simulator_mode_active(request.app):
        repository = paper_repository_from_app(request.app)
        order = repository.get_order(current_user.sub, order_id)
        if order is None or order.get("execution_mode") != "simulation":
            raise HTTPException(status_code=404, detail="order not found")
        return {
            "order_id": order_id,
            "events": jsonable_encoder(_simulation_order_events(repository.list_order_events(current_user.sub, order_id))),
        }
    repository = _repository_from_app(request.app)
    if _get_owned_order(repository, order_id, current_user.sub) is None:
        raise HTTPException(status_code=404, detail="order not found")
    return {"order_id": order_id, "events": jsonable_encoder(repository.list_order_events(order_id))}


@router.websocket("/ws/orders/{order_id}")
async def order_events_socket(websocket: WebSocket, order_id: str) -> None:
    try:
        current_user = require_websocket_user(websocket)
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
        if simulator_mode_active(websocket.app):
            repository = paper_repository_from_app(websocket.app)
            last_fingerprint: str | None = None
            while True:
                if not await asyncio.to_thread(simulator_mode_active, websocket.app):
                    break
                order = await asyncio.to_thread(repository.get_order, current_user.sub, order_id)
                if order is None or order.get("execution_mode") != "simulation":
                    await websocket.send_json({"type": "error", "detail": "order not found"})
                    await websocket.close(code=1008)
                    return
                events = _simulation_order_events(
                    await asyncio.to_thread(repository.list_order_events, current_user.sub, order_id)
                )
                payload = {
                    "type": "snapshot" if last_fingerprint is None else "update",
                    "order": _simulation_order_response(order),
                    "events": jsonable_encoder(events),
                }
                fingerprint = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
                if fingerprint != last_fingerprint:
                    await websocket.send_json(payload)
                    last_fingerprint = fingerprint
                await asyncio.sleep(0.25)
            return
        repository = _repository_from_app(websocket.app)
        last_fingerprint: str | None = None
        while True:
            order = _get_owned_order(repository, order_id, current_user.sub)
            if order is None:
                await websocket.send_json({"type": "error", "detail": "order not found"})
                await websocket.close(code=1008)
                return
            payload = {
                "type": "snapshot" if last_fingerprint is None else "update",
                "order": jsonable_encoder(order),
                "events": jsonable_encoder(repository.list_order_events(order_id)),
            }
            fingerprint = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
            if fingerprint != last_fingerprint:
                await websocket.send_json(payload)
                last_fingerprint = fingerprint
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        return
    except Exception as exc:
        try:
            if websocket.client_state != WebSocketState.DISCONNECTED:
                await websocket.send_json({"type": "error", "detail": str(exc)})
                await websocket.close(code=1011)
        except Exception:
            return


def _risk_verdict(
    app: Any,
    user_sub: str,
    order_request: Any,
    *,
    force: bool = False,
) -> PretradeVerdict | None:
    if not force and not risk_pretrade_enabled():
        return None
    try:
        context = build_risk_context(app, user_sub, order_request.symbol)
        config = _user_risk_config(app, user_sub)
        if config.daily_buy_budget is not None:
            import dataclasses

            spent = budget_tracker_from_app(app).used_today(user_sub)
            context = dataclasses.replace(context, daily_buy_notional=spent)
        return evaluate_pretrade(
            side=order_request.side,
            symbol=order_request.symbol,
            qty=order_request.qty,
            price=order_request.price,
            context=context,
            config=config,
        )
    except Exception:
        # Risk evaluation must never break order submission.
        return None


def _user_risk_config(app: Any, user_sub: str):
    """글로벌 디폴트 위에 사용자 성향 설정을 병합한 RiskConfig."""
    overrides = settings_store_from_app(app).engine_overrides(user_sub)
    if not overrides:
        return _risk_config_from_app(app)
    return load_risk_config(overrides=overrides)


def _find_idempotent_response(repository: Any, idempotency_key_hash: str, body_hash: str) -> dict[str, Any] | None:
    finder = getattr(repository, "find_idempotent_response", None)
    if not callable(finder):
        return None
    try:
        return finder(idempotency_key_hash, body_hash)
    except Exception:
        return None


def _record_buy_spend(app: Any, user_sub: str, order_request: Any) -> None:
    """주문 접수 성공 시 매수 금액 누적 (예산 룰 입력, 접수 기준 보수적 근사)."""
    try:
        if str(order_request.side).lower() != "buy":
            return
        budget_tracker_from_app(app).record_buy(user_sub, order_request.qty * order_request.price)
    except Exception:
        # 집계 실패가 주문 응답을 깨면 안 됨
        return


def _risk_report_redis(app: Any):
    existing = getattr(app.state, "risk_report_redis", None)
    if existing is not None:
        return existing
    url = os.getenv("REDIS_URL")
    if not url:
        return None
    try:
        import redis

        client = redis.from_url(url, decode_responses=True)
    except Exception:
        return None
    app.state.risk_report_redis = client
    return client


def _risk_config_from_app(app: Any):
    existing = getattr(app.state, "risk_config", None)
    if existing is not None:
        return existing
    config = load_risk_config()
    app.state.risk_config = config
    return config


def _with_risk(order: dict[str, Any], verdict: PretradeVerdict | None) -> dict[str, Any]:
    if verdict is None:
        return order
    return {**order, "risk": verdict.to_dict()}


def _simulation_order_response(order: dict[str, Any]) -> dict[str, Any]:
    status_value = "accepted" if order.get("status") == "pending" else order.get("status")
    return {
        **order,
        "status": status_value,
        "simulation": True,
        "runId": order.get("runId") or order.get("simulation_run_id"),
        "order_type": order.get("order_type") or "limit",
        "virtualSubmittedAt": order.get("virtualSubmittedAt") or order.get("virtual_submitted_at"),
        "virtualFilledAt": order.get("virtualFilledAt") or order.get("virtual_filled_at"),
    }


def _simulation_order_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rendered = []
    for event in events:
        if event.get("status") == "pending":
            rendered.append({**event, "status": "accepted", "event_type": "order.accepted"})
        else:
            rendered.append(dict(event))
    return rendered


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="order request must be a JSON object")
    return payload


def _validate_order_request(payload: dict[str, Any]):
    try:
        return validate_order_request_payload(
            payload,
            default_account_alias=os.getenv("KAFKA_ACCOUNT_ALIAS", "demo-account"),
        )
    except OrderContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _validate_order_envelope(envelope: dict[str, Any]):
    try:
        return validate_order_envelope(envelope)
    except OrderContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _validate_no_forbidden_fields(value: Any) -> None:
    try:
        assert_no_forbidden_fields(value)
    except OrderContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _repository_from_app(app: Any) -> OrderRepository:
    existing = getattr(app.state, "order_repository", None)
    if existing is not None:
        return existing

    kis_env = os.getenv("KIS_ENV", "demo").strip().lower()
    if kis_env != "demo":
        raise HTTPException(status_code=503, detail="Only KIS demo trading is enabled. Set KIS_ENV=demo.")

    repository_mode = os.getenv("ORDER_REPOSITORY", "postgres").strip().lower()
    if repository_mode == "memory":
        repository = InMemoryOrderRepository()
    else:
        if not os.getenv("DATABASE_URL") and not (
            os.getenv("DATABASE_HOST")
            and os.getenv("DATABASE_NAME")
            and os.getenv("DATABASE_USER")
            and os.getenv("DATABASE_PASSWORD")
        ):
            raise HTTPException(status_code=503, detail="DATABASE_URL or DATABASE_* settings are required for order API")
        repository = PostgresOrderRepository.from_env()
    app.state.order_repository = repository
    return repository


def _get_owned_order(repository: OrderRepository, order_id: str, user_sub: str) -> dict[str, Any] | None:
    """Return an order only when it belongs to the authenticated principal.

    Missing, legacy-unowned, and foreign-user orders intentionally collapse to
    the same result so callers cannot use order or event routes as an existence
    oracle. Order ownership is immutable after creation, so a successful check
    also safely gates the following event-history read.
    """
    order = repository.get_order(order_id)
    if order is None or order.get("user_sub") != user_sub:
        return None
    return order


def _scoped_idempotency_key(idempotency_key: str, user: AuthenticatedUser) -> str:
    if not auth_is_enabled():
        return idempotency_key
    return f"{user.sub}:{idempotency_key}"
