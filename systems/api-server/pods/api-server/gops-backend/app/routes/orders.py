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
from app.routes.simulator import simulator_gateway_from_app, simulator_mode_active
from kis_trader.domain.commands import validate_order_request_payload
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
        "required_fields": ["market", "symbol", "side", "qty", "price", "exchange"],
        "optional_fields": [
            "account_alias",
            "order_division",
            "actor_id",
            "role",
        ],
        "accepted_values": {
            "market": ["overseas"],
            "side": ["buy", "sell"],
            "order_division": ["00"],
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
    order_request = _validate_order_request(payload)
    if simulator_mode_active(request.app):
        try:
            result = simulator_gateway_from_app(request.app).individual_order(
                user_id=current_user.sub,
                symbol=str(payload["symbol"]).upper(),
                side=str(payload["side"]).lower(),
                quantity=int(payload["qty"]),
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        order = result.get("order") if isinstance(result, dict) else None
        if not isinstance(order, dict):
            raise HTTPException(status_code=502, detail="simulator returned an invalid order")
        return jsonable_encoder(order)
    envelope = build_order_command_envelope(
        order_request,
        occurred_at=datetime.now(timezone.utc).isoformat(),
    )
    _validate_no_forbidden_fields(envelope)
    command = _validate_order_envelope(envelope)

    repository = _repository_from_app(request.app)
    try:
        result = repository.create_received_order(
            idempotency_key_hash=hash_idempotency_key(_scoped_idempotency_key(idempotency_key.strip(), current_user)),
            body_hash=stable_body_hash(payload),
            command=command,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if result.idempotent_replay:
        response.headers["X-Idempotent-Replay"] = "true"
    return jsonable_encoder(result.response)


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
            payload = simulator_gateway_from_app(request.app).account(_user.sub)
            account = payload.get("account") if isinstance(payload, dict) else {}
            cash = account.get("cashForeign", 0) if isinstance(account, dict) else 0
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
    _user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    repository = _repository_from_app(request.app)
    order = repository.get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return jsonable_encoder(order)


@router.get("/api/orders/{order_id}/events")
def get_order_events(
    order_id: str,
    request: Request,
    _user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    repository = _repository_from_app(request.app)
    if repository.get_order(order_id) is None:
        raise HTTPException(status_code=404, detail="order not found")
    return {"order_id": order_id, "events": jsonable_encoder(repository.list_order_events(order_id))}


@router.websocket("/ws/orders/{order_id}")
async def order_events_socket(websocket: WebSocket, order_id: str) -> None:
    try:
        require_websocket_user(websocket)
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
        last_fingerprint: str | None = None
        while True:
            order = repository.get_order(order_id)
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


def _scoped_idempotency_key(idempotency_key: str, user: AuthenticatedUser) -> str:
    if not auth_is_enabled():
        return idempotency_key
    return f"{user.sub}:{idempotency_key}"
