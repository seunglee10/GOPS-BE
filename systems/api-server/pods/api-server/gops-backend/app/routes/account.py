from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder

from app.auth.dependencies import require_current_user
from app.auth.models import AuthenticatedUser
from kis_trader.kis.config import KisConfigError
from kis_trader.kis.fake import KisConnectionReset, KisExplicitReject, KisHttpError, KisTimeout, KisTokenExpired


router = APIRouter(tags=["account"])


@router.get("/api/account/holdings")
def account_holdings(
    request: Request,
    market: str = Query(default="overseas", pattern="^(overseas|domestic)$"),
    currency: str = Query(default="USD", min_length=3, max_length=3),
    exchange: str = Query(default="", max_length=8),
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    try:
        client = _kis_client_from_app(request.app)
        payload = client.fetch_holdings(market=market, currency=currency, exchange=exchange)
    except KisConfigError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except (KisTimeout, KisConnectionReset) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="KIS holdings API is temporarily unavailable") from exc
    except KisTokenExpired as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="KIS authentication failed") from exc
    except KisExplicitReject as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except KisHttpError as exc:
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE if exc.safe_to_retry else status.HTTP_502_BAD_GATEWAY
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    _remember_portfolio_holdings_snapshot(request.app, user.sub, payload)
    return jsonable_encoder(payload)


def _kis_client_from_app(app: Any) -> Any:
    existing = getattr(app.state, "kis_client", None)
    if existing is not None:
        return existing
    try:
        from kis_trader.kis.client import DemoKisHttpClient
    except ModuleNotFoundError as exc:
        if exc.name == "requests":
            raise KisConfigError("Missing local Python dependency: requests. Install root requirements into .venv to use KIS holdings.") from exc
        raise
    client = DemoKisHttpClient.from_env()
    app.state.kis_client = client
    return client


def _remember_portfolio_holdings_snapshot(app: Any, user_sub: str, payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        return
    snapshots = getattr(app.state, "portfolio_holdings_snapshots", None)
    if not isinstance(snapshots, dict):
        snapshots = {}
        app.state.portfolio_holdings_snapshots = snapshots
    snapshots[user_sub] = payload
    try:
        from app.recommendations.routes import _repository_from_app

        repository = _repository_from_app(app)
        upsert_snapshot = getattr(repository, "upsert_portfolio_snapshot", None)
        if callable(upsert_snapshot):
            upsert_snapshot(user_sub, payload)
    except Exception:
        return
