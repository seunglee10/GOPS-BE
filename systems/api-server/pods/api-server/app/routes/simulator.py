from __future__ import annotations

import os
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from app.auth.dependencies import auth_is_enabled, optional_current_user, require_current_user
from app.auth.models import AuthenticatedUser
from app.services.simulator_gateway import SimulatorGateway, SimulatorUnavailable


router = APIRouter(prefix="/api/simulator", tags=["simulator"])


class SimulatorModeRequest(BaseModel):
    mode: Literal["live", "simulation"]


class SimulatorActionRequest(BaseModel):
    action: Literal["start", "pause", "resume", "restart"]


class SimulatorSpeedRequest(BaseModel):
    speed: Literal[1, 2, 5, 10]


def require_simulator_operator(
    user: Annotated[AuthenticatedUser, Depends(require_current_user)],
) -> AuthenticatedUser:
    if not simulator_operator_allowed(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="simulator operator permission required",
        )
    return user


def simulator_operator_allowed(user: AuthenticatedUser | None) -> bool:
    # This is intentionally limited to the local auth-disabled runtime.  Production
    # still requires an authenticated user whose email is on the operator allowlist.
    if not auth_is_enabled() and _local_simulator_control_enabled():
        return True
    if user is None:
        return False
    allowed_emails = {
        email.strip().casefold()
        for email in os.getenv("SIMULATOR_OPERATOR_EMAILS", "").split(",")
        if email.strip()
    }
    return user.email.strip().casefold() in allowed_emails


def _local_simulator_control_enabled() -> bool:
    return os.getenv("SIMULATOR_LOCAL_CONTROL_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@router.get("/status")
def simulator_status(
    request: Request,
    user: Annotated[AuthenticatedUser | None, Depends(optional_current_user)],
) -> dict[str, Any]:
    capability_user = user
    if capability_user is None and not auth_is_enabled():
        capability_user = AuthenticatedUser.dev()
    can_control = simulator_operator_allowed(capability_user)
    try:
        return {
            "available": True,
            **simulator_gateway_from_app(request.app).status(),
            "canControl": can_control,
        }
    except SimulatorUnavailable as exc:
        return {
            "available": False,
            "canControl": can_control,
            "mode": "live",
            "state": "idle",
            "detail": str(exc),
            "datasetId": "sp500-full-20260715-kst-v3",
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
def simulator_mode(
    payload: SimulatorModeRequest,
    request: Request,
    _operator: Annotated[AuthenticatedUser, Depends(require_simulator_operator)],
) -> dict[str, Any]:
    _cancel_previous_simulation_run(request.app)
    return _call_simulator(lambda gateway: gateway.set_mode(payload.mode), request, can_control=True)


@router.post("/action")
def simulator_action(
    payload: SimulatorActionRequest,
    request: Request,
    _operator: Annotated[AuthenticatedUser, Depends(require_simulator_operator)],
) -> dict[str, Any]:
    if payload.action in {"start", "restart"}:
        _cancel_previous_simulation_run(request.app)
    return _call_simulator(lambda gateway: gateway.action(payload.action), request, can_control=True)


@router.put("/speed")
def simulator_speed(
    payload: SimulatorSpeedRequest,
    request: Request,
    _operator: Annotated[AuthenticatedUser, Depends(require_simulator_operator)],
) -> dict[str, Any]:
    return _call_simulator(lambda gateway: gateway.set_speed(payload.speed), request, can_control=True)


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
    gateway = simulator_gateway_from_app(app)
    try:
        return gateway.status().get("mode") == "simulation"
    except SimulatorUnavailable:
        return (getattr(gateway, "last_status", None) or {}).get("mode") == "simulation"


def _call_simulator(callback, request: Request, *, can_control: bool | None = None) -> dict[str, Any]:
    try:
        response = callback(simulator_gateway_from_app(request.app))
        if can_control is None:
            return response
        return {**response, "canControl": can_control}
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
