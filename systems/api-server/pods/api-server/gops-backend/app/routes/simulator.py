from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from app.auth.dependencies import require_current_user
from app.auth.models import AuthenticatedUser
from app.services.simulator_gateway import SimulatorGateway, SimulatorUnavailable


router = APIRouter(prefix="/api/simulator", tags=["simulator"])


class SimulatorModeRequest(BaseModel):
    mode: Literal["live", "simulation"]


class SimulatorActionRequest(BaseModel):
    action: Literal["start", "pause", "resume", "restart"]


class SimulatorSpeedRequest(BaseModel):
    speed: Literal[1, 5, 20, 60, 300]


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
            "datasetId": "sp500-top20-20260715-kst-v1",
            "runId": None,
            "virtualTime": "2026-07-15T00:00:00+09:00",
            "startTime": "2026-07-15T00:00:00+09:00",
            "endTime": "2026-07-16T00:00:00+09:00",
            "requestedSpeed": 1,
            "effectiveSpeed": 0,
            "processedEventCount": 0,
            "totalEventCount": 0,
            "progress": 0,
            "lagMs": 0,
            "symbols": [],
        }


@router.put("/mode")
def simulator_mode(payload: SimulatorModeRequest, request: Request) -> dict[str, Any]:
    _cancel_previous_simulation_run(request.app)
    return _call_simulator(lambda gateway: gateway.set_mode(payload.mode), request)


@router.post("/action")
def simulator_action(payload: SimulatorActionRequest, request: Request) -> dict[str, Any]:
    if payload.action in {"start", "restart"}:
        _cancel_previous_simulation_run(request.app)
    return _call_simulator(lambda gateway: gateway.action(payload.action), request)


@router.put("/speed")
def simulator_speed(payload: SimulatorSpeedRequest, request: Request) -> dict[str, Any]:
    return _call_simulator(lambda gateway: gateway.set_speed(payload.speed), request)


@router.get("/quote")
def simulator_quote(
    request: Request,
    symbol: str = Query(min_length=1, max_length=12),
) -> dict[str, Any]:
    if not simulator_mode_active(request.app):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="simulation mode is not active")
    normalized_symbol = symbol.strip().upper()
    return _call_simulator(lambda gateway: gateway.quote(normalized_symbol), request)


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


def _call_simulator(callback, request: Request) -> dict[str, Any]:
    try:
        return callback(simulator_gateway_from_app(request.app))
    except SimulatorUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


def _cancel_previous_simulation_run(app: Any) -> None:
    try:
        current = simulator_gateway_from_app(app).status()
    except SimulatorUnavailable:
        return
    run_id = current.get("runId") if current.get("mode") == "simulation" else None
    if not run_id:
        return
    try:
        from app.routes.paper_trading import paper_repository_from_app

        paper_repository_from_app(app).cancel_simulation_run(str(run_id))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"failed to release simulation reservations: {exc}",
        ) from exc
