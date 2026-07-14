from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.auth.dependencies import require_current_user
from app.auth.models import AuthenticatedUser
from app.services.simulator_gateway import SimulatorGateway, SimulatorUnavailable


router = APIRouter(prefix="/api/simulator", tags=["simulator"])


class SimulatorModeRequest(BaseModel):
    mode: Literal["live", "simulation"]


class SimulatorActionRequest(BaseModel):
    action: Literal["pause", "resume", "restart"]


class SimulatorPhaseRequest(BaseModel):
    phase: str


class SimulatorBasketOrderRequest(BaseModel):
    basket: Literal["semiconductor", "energy"]
    side: Literal["buy", "sell"]


@router.get("/status")
def simulator_status(request: Request) -> dict[str, Any]:
    try:
        return {"available": True, **simulator_gateway_from_app(request.app).status()}
    except SimulatorUnavailable as exc:
        return {
            "available": False,
            "mode": "live",
            "state": "idle",
            "detail": str(exc),
            "elapsedSeconds": 0,
            "durationSeconds": 300,
            "breakingNewsAtSeconds": 210,
            "breakingNewsReleased": False,
            "phase": "live",
            "phaseIndex": -1,
            "nextPhase": None,
            "phases": [],
            "symbols": [],
        }


@router.put("/mode")
def simulator_mode(payload: SimulatorModeRequest, request: Request) -> dict[str, Any]:
    return _call_simulator(lambda gateway: gateway.set_mode(payload.mode), request)


@router.post("/action")
def simulator_action(payload: SimulatorActionRequest, request: Request) -> dict[str, Any]:
    return _call_simulator(lambda gateway: gateway.action(payload.action), request)


@router.put("/phase")
def simulator_phase(payload: SimulatorPhaseRequest, request: Request) -> dict[str, Any]:
    return _call_simulator(lambda gateway: gateway.set_phase(payload.phase), request)


@router.get("/news")
def simulator_news(request: Request) -> dict[str, Any]:
    return _call_simulator(lambda gateway: gateway.news(), request)


@router.post("/orders/basket")
def simulator_basket_order(
    payload: SimulatorBasketOrderRequest,
    request: Request,
    current_user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    idempotency_key = request.headers.get("Idempotency-Key", "").strip()
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    if not simulator_mode_active(request.app):
        raise HTTPException(status_code=409, detail="simulation mode is not active")
    key = (current_user.sub, idempotency_key)
    cache = _simulation_idempotency_cache(request.app)
    if key in cache:
        return cache[key]
    result = _call_simulator(
        lambda gateway: gateway.basket_order(
            user_id=current_user.sub,
            basket=payload.basket,
            side=payload.side,
        ),
        request,
    )
    cache[key] = result
    return result


def simulator_gateway_from_app(app: Any) -> SimulatorGateway:
    existing = getattr(app.state, "simulator_gateway", None)
    if existing is not None:
        return existing
    gateway = SimulatorGateway()
    app.state.simulator_gateway = gateway
    return gateway


def simulator_mode_active(app: Any) -> bool:
    try:
        return simulator_gateway_from_app(app).status().get("mode") == "simulation"
    except SimulatorUnavailable:
        return False


def _simulation_idempotency_cache(app: Any) -> dict[tuple[str, str], dict[str, Any]]:
    cache = getattr(app.state, "simulation_idempotency_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        app.state.simulation_idempotency_cache = cache
    return cache


def _call_simulator(callback, request: Request) -> dict[str, Any]:
    try:
        return callback(simulator_gateway_from_app(request.app))
    except SimulatorUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
