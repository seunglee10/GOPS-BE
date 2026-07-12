"""FastAPI surface for the order reliability backend."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import FastAPI, Header, HTTPException, WebSocket
from fastapi.encoders import jsonable_encoder

from kis_trader.domain.commands import OrderRequest, validate_order_request_payload
from kis_trader.domain.envelope import build_order_command_envelope
from kis_trader.domain.status import CANONICAL_STATUSES, OrderContractError
from kis_trader.domain.topics import CANONICAL_ORDER_TOPICS
from kis_trader.persistence.memory import InMemoryOrderRepository
from kis_trader.persistence.postgres import PostgresOrderRepository
from kis_trader.persistence.repository import IdempotencyConflictError, OrderRepository
from kis_trader.risk import PretradeVerdict, RiskContext, evaluate_pretrade, load_risk_config
from kis_trader.risk.context import risk_context_from_dict
from kis_trader.security.idempotency import hash_idempotency_key, stable_body_hash
from kis_trader.security.validation import assert_no_forbidden_fields

RiskContextProvider = Callable[[OrderRequest], RiskContext | None]


def create_app(
    repository: OrderRepository | None = None,
    risk_context_provider: RiskContextProvider | None = None,
) -> FastAPI:
    if os.getenv("KIS_ENV", "demo").strip().lower() != "demo":
        raise RuntimeError("Only KIS demo trading is implemented. KIS_ENV=real is not allowed.")

    app = FastAPI(title="GOPS KIS Trader API", version="0.1.0")
    app.state.repository = repository or _default_repository()
    app.state.risk_context_provider = risk_context_provider
    app.state.risk_config = load_risk_config()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/order-contract")
    def order_contract() -> dict[str, tuple[str, ...]]:
        return {"statuses": CANONICAL_STATUSES, "topics": CANONICAL_ORDER_TOPICS}

    @app.post("/orders", status_code=202)
    def submit_order(payload: dict[str, Any], idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
        if not idempotency_key:
            raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
        try:
            assert_no_forbidden_fields(payload)
            body_hash = stable_body_hash(payload)
            request = validate_order_request_payload(
                payload,
                default_account_alias=os.getenv("KAFKA_ACCOUNT_ALIAS", "demo-account"),
            )
            verdict = _pretrade_verdict(app, request, payload)
            if verdict is not None and verdict.verdict != "allow":
                # Block outright, or ask the user to confirm the resized qty by
                # resubmitting. The risk engine never silently changes an order.
                raise HTTPException(status_code=422, detail={"reason": "risk rejected", "risk": verdict.to_dict()})
            envelope = build_order_command_envelope(
                request,
                occurred_at=datetime.now(timezone.utc).isoformat(),
            )
            command = build_order_command(envelope)
            result = app.state.repository.create_received_order(
                idempotency_key_hash=hash_idempotency_key(idempotency_key),
                body_hash=body_hash,
                command=command,
            )
        except IdempotencyConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OrderContractError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        response = jsonable_encoder(result.response)
        if verdict is not None:
            response["risk"] = verdict.to_dict()
        return response

    @app.post("/risk/pretrade")
    def pretrade_preview(payload: dict[str, Any]) -> dict[str, Any]:
        """Advisory pre-trade risk verdict for the order ticket UI (no order created)."""
        try:
            assert_no_forbidden_fields(payload)
            request = validate_order_request_payload(
                payload,
                default_account_alias=os.getenv("KAFKA_ACCOUNT_ALIAS", "demo-account"),
            )
        except OrderContractError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        verdict = _pretrade_verdict(app, request, payload, allow_inline_context=True)
        if verdict is None:
            verdict = _evaluate(app, request, RiskContext())
        return {"symbol": request.symbol, "side": request.side, "risk": verdict.to_dict()}

    @app.get("/orders/{order_id}")
    def get_order(order_id: str) -> dict[str, Any]:
        order = app.state.repository.get_order(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="order not found")
        return jsonable_encoder(order)

    @app.get("/orders/{order_id}/events")
    def get_order_events(order_id: str) -> dict[str, Any]:
        if app.state.repository.get_order(order_id) is None:
            raise HTTPException(status_code=404, detail="order not found")
        events = app.state.repository.list_order_events(order_id)
        return {"order_id": order_id, "events": jsonable_encoder(events)}

    @app.websocket("/ws/orders/{order_id}")
    async def order_status_ws(websocket: WebSocket, order_id: str) -> None:
        await websocket.accept()
        last_status: str | None = None
        while True:
            order = app.state.repository.get_order(order_id)
            if order is None:
                await websocket.send_json({"type": "error", "detail": "order not found"})
                await websocket.close()
                return
            if order["status"] != last_status:
                await websocket.send_json({"type": "order.status", "order": jsonable_encoder(order)})
                last_status = order["status"]
            await asyncio.sleep(1)

    return app


def build_order_command(envelope: dict[str, Any]):
    from kis_trader.domain.envelope import validate_order_envelope

    return validate_order_envelope(envelope)


def _pretrade_verdict(
    app: FastAPI,
    request: OrderRequest,
    payload: dict[str, Any],
    *,
    allow_inline_context: bool = False,
) -> PretradeVerdict | None:
    provider: RiskContextProvider | None = app.state.risk_context_provider
    context: RiskContext | None = None
    if provider is not None:
        context = provider(request)
    elif allow_inline_context and isinstance(payload.get("risk_context"), dict):
        # Preview-only convenience: the order ticket can pass the data it
        # already has. Advisory output — never used to admit an order.
        context = risk_context_from_dict(payload["risk_context"])
    if context is None:
        return None
    return _evaluate(app, request, context)


def _evaluate(
    app: FastAPI,
    request: OrderRequest,
    context: RiskContext,
) -> PretradeVerdict:
    return evaluate_pretrade(
        side=request.side,
        symbol=request.symbol,
        qty=request.qty,
        price=request.price,
        context=context,
        config=app.state.risk_config,
    )


def _default_repository() -> OrderRepository:
    try:
        return PostgresOrderRepository.from_env()
    except KeyError:
        return InMemoryOrderRepository()


app = create_app()
