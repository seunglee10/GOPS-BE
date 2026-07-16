from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder

from app.auth.dependencies import require_current_user
from app.auth.models import AuthenticatedUser
from app.core.sectors import sector_payload_fields
from app.services.alpaca_corporate_actions import enrich_holdings_with_alpaca_dividends
from app.services.portfolio_market_enrichment import enrich_holdings_with_market_stats
from app.routes.simulator import simulator_gateway_from_app, simulator_mode_active
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
    if simulator_mode_active(request.app):
        try:
            payload = _normalize_simulator_holdings(simulator_gateway_from_app(request.app).account(user.sub))
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
        _remember_portfolio_holdings_snapshot(request.app, user.sub, payload)
        return jsonable_encoder(payload)
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
    payload = _enrich_portfolio_holdings_sectors(request.app, payload)
    if market == "overseas":
        payload = enrich_holdings_with_alpaca_dividends(payload)
        payload = enrich_holdings_with_market_stats(request.app, payload)
    _remember_portfolio_holdings_snapshot(request.app, user.sub, payload)
    return jsonable_encoder(payload)


def _normalize_simulator_holdings(payload: dict[str, Any]) -> dict[str, Any]:
    positions = payload.get("positions")
    if isinstance(positions, dict):
        rendered_positions = [value for value in positions.values() if isinstance(value, dict)]
    elif isinstance(positions, list):
        rendered_positions = [value for value in positions if isinstance(value, dict)]
    else:
        rendered_positions = []
    return {
        **payload,
        "asOf": payload.get("virtualTime") or datetime.now(timezone.utc).isoformat(),
        "positions": rendered_positions,
    }


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


def _enrich_portfolio_holdings_sectors(app: Any, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    positions = payload.get("positions")
    if not isinstance(positions, list):
        return payload
    sector_by_symbol = _portfolio_sector_map(app)
    enriched_positions = []
    for position in positions:
        if not isinstance(position, dict):
            continue
        symbol = str(position.get("symbol") or "").strip().upper()
        sector_source = sector_by_symbol.get(symbol) or position.get("sector")
        enriched_positions.append({**position, **sector_payload_fields(sector_source)})
    return {**payload, "positions": enriched_positions}


def _portfolio_sector_map(app: Any) -> dict[str, str]:
    provider = getattr(app.state, "portfolio_sector_provider", None)
    if callable(provider):
        try:
            return _sector_map_from_items(provider())
        except Exception:
            return {}
    try:
        from app.market_data.heatmap.service import get_heatmap_service

        payload = get_heatmap_service().snapshot("sp500")
        return _sector_map_from_items(payload.get("items") if isinstance(payload, dict) else [])
    except Exception:
        try:
            from app.market_data.heatmap.service import load_heatmap_seed_items

            return _sector_map_from_items(load_heatmap_seed_items("sp500"))
        except Exception:
            return {}


def _sector_map_from_items(items: Any) -> dict[str, str]:
    if not isinstance(items, list):
        return {}
    result: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        if symbol:
            result[symbol] = str(item.get("sector") or "")
    return result
