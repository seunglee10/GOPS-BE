from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder

from app.auth.dependencies import require_current_user
from app.auth.models import AuthenticatedUser
from app.core.sectors import sector_payload_fields
from app.market_data.indices.service import get_indices_service
from app.services.alpaca_corporate_actions import enrich_holdings_with_alpaca_dividends
from app.services.portfolio_performance import build_portfolio_performance, parse_datetime, performance_start_at
from app.services.portfolio_market_enrichment import enrich_holdings_with_market_stats
from app.routes.simulator import simulator_gateway_from_app, simulator_mode_active
from kis_trader.kis.config import KisConfigError
from kis_trader.kis.fake import KisConnectionReset, KisExplicitReject, KisHttpError, KisTimeout, KisTokenExpired


router = APIRouter(tags=["account"])
logger = logging.getLogger(__name__)

PAPER_PERFORMANCE_SOURCES = frozenset({"paper-shared", "account-history", "seeded-demo"})


@router.get("/api/account/holdings")
def account_holdings(
    request: Request,
    market: str = Query(default="overseas", pattern="^(overseas|domestic)$"),
    currency: str = Query(default="USD", min_length=3, max_length=3),
    exchange: str = Query(default="", max_length=8),
    source: str = Query(default="active", pattern="^(active|kis)$"),
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    if source == "active":
        simulation_active = simulator_mode_active(request.app)
        try:
            from app.routes.paper_trading import _enriched_account_snapshot, paper_repository_from_app

            repository = paper_repository_from_app(request.app)
            snapshot = _enriched_account_snapshot(request.app, repository, user.sub)
            payload = _paper_portfolio_payload(request.app, repository, user.sub, snapshot)
            if not simulation_active:
                payload = enrich_holdings_with_alpaca_dividends(payload)
        except HTTPException:
            raise
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


@router.get("/api/account/performance")
def account_performance(
    request: Request,
    range_value: str = Query(default="1M", alias="range", pattern="^(1W|1M|3M|1Y|ALL)$"),
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    simulation_time = _simulation_reference_time(request.app)
    now_provider = getattr(request.app.state, "portfolio_performance_now_provider", None)
    now = simulation_time or (now_provider() if callable(now_provider) else datetime.now(timezone.utc))
    start_at = performance_start_at(range_value, now)
    performance_sources, current_principal, current_principal_started_at = _paper_performance_context(
        request.app,
        user.sub,
    )
    try:
        from app.recommendations.repository import RecommendationSchemaUnavailable
        from app.recommendations.routes import _repository_from_app

        repository = _repository_from_app(request.app)
        source_reader = getattr(repository, "list_daily_portfolio_snapshots_for_sources", None)
        if callable(source_reader):
            snapshots = source_reader(
                user.sub,
                start_at.isoformat() if start_at is not None else None,
                performance_sources,
            )
        else:
            snapshots = _paper_performance_snapshots(
                repository.list_daily_portfolio_snapshots(
                    user.sub,
                    start_at.isoformat() if start_at is not None else None,
                )
            )
    except RecommendationSchemaUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    if simulation_time is not None:
        snapshots = _snapshots_available_at(snapshots, simulation_time)
    if not snapshots:
        try:
            from app.routes.paper_trading import paper_repository_from_app

            paper_repository = paper_repository_from_app(request.app)
            history = getattr(paper_repository, "portfolio_history", None)
            if isinstance(history, list):
                snapshots = [
                    {"payload": row.get("payload"), "source_as_of": row.get("source_as_of")}
                    for row in history
                    if row.get("user_sub") == user.sub
                    and (
                        start_at is None
                        or (parse_datetime(row.get("source_as_of")) or datetime.min.replace(tzinfo=timezone.utc)) >= start_at
                    )
                ]
        except Exception:
            snapshots = snapshots or []
    if simulation_time is not None:
        snapshots = _snapshots_available_at(snapshots, simulation_time)

    benchmark = None
    if snapshots:
        first_snapshot = snapshots[0]
        first_payload = first_snapshot.get("payload") if isinstance(first_snapshot, dict) else None
        benchmark_start = parse_datetime(
            first_snapshot.get("source_as_of")
            if isinstance(first_snapshot, dict)
            else None
        )
        if benchmark_start is None and isinstance(first_payload, dict):
            benchmark_start = parse_datetime(first_payload.get("asOf") or first_payload.get("sourceAsOf"))
        try:
            if simulation_time is not None:
                benchmark = simulator_gateway_from_app(request.app).index_performance(range_value, benchmark_start)
            else:
                benchmark_provider = getattr(request.app.state, "portfolio_benchmark_provider", None)
                benchmark = (
                    benchmark_provider(range_value, benchmark_start)
                    if callable(benchmark_provider)
                    else get_indices_service().performance_history(range_value, benchmark_start)
                )
        except Exception:
            benchmark = None
    result = build_portfolio_performance(
        snapshots,
        benchmark,
        range_value=range_value,
        net_invested_principal=_performance_principal_for_snapshots(
            snapshots,
            current_principal=current_principal,
            current_principal_started_at=current_principal_started_at,
            simulation_time=simulation_time,
        ),
    )
    result["dataOrigin"] = _performance_data_origin(snapshots)
    result["isDevFixture"] = result["dataOrigin"] == "seeded-demo"
    return jsonable_encoder(result)


def _simulation_reference_time(app: Any) -> datetime | None:
    if not simulator_mode_active(app):
        return None
    try:
        status_payload = simulator_gateway_from_app(app).status()
        parsed = datetime.fromisoformat(str(status_payload.get("virtualTime") or "").replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="simulation_virtual_time_unavailable",
        ) from exc
    if parsed.tzinfo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="simulation_virtual_time_unavailable",
        )
    return parsed.astimezone(timezone.utc)


def _snapshots_available_at(snapshots: list[dict[str, Any]], cutoff: datetime) -> list[dict[str, Any]]:
    available = []
    for row in snapshots:
        if not isinstance(row, dict):
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        source_as_of = parse_datetime(
            row.get("source_as_of") or payload.get("asOf") or payload.get("sourceAsOf")
        )
        if source_as_of is not None and source_as_of <= cutoff:
            available.append(row)
    return available


def _paper_portfolio_payload(app: Any, repository: Any, user_sub: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    account = snapshot.get("account") or {}
    account_orders = repository.list_orders(user_sub, include_previous=False, limit=500)
    has_account_activity = any(not order.get("seed_profile") for order in account_orders)
    data_origin = "account-history" if has_account_activity or not account.get("seed_profile") else "seeded-demo"
    positions = []
    for raw in snapshot.get("positions") or []:
        quantity = float(raw.get("qty") or 0)
        current_price = float(raw.get("current_price") or 0)
        average_price = float(raw.get("average_price") or 0)
        market_value = float(raw.get("market_value") or current_price * quantity)
        unrealized = float(raw.get("unrealized_pnl") or market_value - average_price * quantity)
        positions.append({
            "symbol": raw.get("symbol"),
            "name": raw.get("name"),
            "market": raw.get("market") or "overseas",
            "exchange": raw.get("exchange"),
            "currency": raw.get("currency") or "USD",
            "sector": raw.get("sector"),
            "industry": raw.get("industry"),
            "quantity": quantity,
            "availableQuantity": float(raw.get("available_qty") or quantity),
            "averagePrice": average_price,
            "currentPrice": current_price,
            "purchaseAmountForeign": average_price * quantity,
            "marketValueForeign": market_value,
            "unrealizedPnlForeign": unrealized,
            "unrealizedPnlRate": float(raw.get("unrealized_pnl_rate") or 0),
            "dayPnlForeign": raw.get("day_pnl"),
            "dayPnlRate": raw.get("day_pnl_rate"),
            "peRatio": raw.get("pe_ratio"),
            "epsTtm": raw.get("eps_ttm"),
            "low52": raw.get("low_52"),
            "high52": raw.get("high_52"),
            "dividendYield": raw.get("dividend_yield"),
            "dividendPerShare": raw.get("dividend_per_share"),
            "annualDividend": raw.get("annual_dividend"),
        })
    as_of = datetime.now(timezone.utc).isoformat()
    run_id = None
    orders = []
    if simulator_mode_active(app):
        try:
            sim_status = simulator_gateway_from_app(app).status()
            as_of = sim_status.get("virtualTime") or as_of
            run_id = sim_status.get("runId")
            orders = [
                order for order in account_orders
                if order.get("execution_mode") == "simulation" and order.get("runId") == run_id
            ]
        except Exception:
            orders = []
    return {
        "status": "ok" if positions else "empty",
        "source": "paper-shared",
        "asOf": as_of,
        "seedProfile": account.get("seed_profile"),
        "dataOrigin": data_origin,
        "simulation": bool(run_id),
        "runId": run_id,
        "account": {
            "alias": "10종목 7섹터 균형형 가상계좌",
            "market": "overseas",
            "currency": "USD",
            "cashForeign": account.get("cash_balance"),
            "stockValueForeign": account.get("market_value"),
            "totalValueForeign": account.get("equity"),
            "unrealizedPnlForeign": account.get("unrealized_pnl"),
            "unrealizedPnlRate": (
                float(account.get("unrealized_pnl") or 0)
                / max(float(account.get("market_value") or 0) - float(account.get("unrealized_pnl") or 0), 1e-9)
                * 100
            ),
            "realizedPnlForeign": account.get("realized_pnl"),
            "netInvestedPrincipal": account.get("starting_cash"),
            "paperGeneration": account.get("generation"),
            "paperStartedAt": account.get("started_at"),
        },
        "positions": positions,
        "orders": orders,
        "limitations": [],
    }


def _performance_data_origin(snapshots: list[dict[str, Any]]) -> str:
    for row in reversed(snapshots):
        payload = row.get("payload") if isinstance(row, dict) else None
        if isinstance(payload, dict):
            if payload.get("dataOrigin") in {"seeded-demo", "account-history"}:
                return str(payload["dataOrigin"])
            return (
                "seeded-demo"
                if payload.get("seedProfile") or payload.get("seed_profile") or payload.get("source") == "seeded-demo"
                else "account-history"
            )
    return "account-history"


def _paper_performance_snapshots(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in snapshots:
        payload = row.get("payload") if isinstance(row, dict) else None
        if not isinstance(payload, dict):
            continue
        if payload.get("source") in PAPER_PERFORMANCE_SOURCES:
            result.append(row)
    return result


def _paper_performance_context(
    app: Any,
    user_sub: str,
) -> tuple[tuple[str, ...], float | None, datetime | None]:
    all_sources = tuple(sorted(PAPER_PERFORMANCE_SOURCES))
    try:
        from app.routes.paper_trading import paper_repository_from_app

        paper_repository = paper_repository_from_app(app)
        snapshot = paper_repository.account_snapshot(user_sub)
        account = snapshot.get("account") or {}
        sources = all_sources
        if account.get("seed_profile"):
            current_orders = paper_repository.list_orders(
                user_sub,
                include_previous=False,
                limit=500,
            )
            if all(order.get("seed_profile") for order in current_orders):
                sources = ("seeded-demo",)
        try:
            parsed_principal = float(account.get("starting_cash"))
            principal = parsed_principal if parsed_principal >= 0 else None
        except (TypeError, ValueError):
            principal = None
        return sources, principal, parse_datetime(account.get("started_at"))
    except Exception:
        return all_sources, None, None


def _performance_principal_for_snapshots(
    snapshots: list[dict[str, Any]],
    *,
    current_principal: float | None,
    current_principal_started_at: datetime | None,
    simulation_time: datetime | None,
) -> float | None:
    """Use a live paper principal only when every visible point belongs to the current account run."""
    if current_principal is None:
        return None
    if _is_current_seeded_demo_history(snapshots):
        return current_principal
    if simulation_time is not None:
        return None
    if current_principal_started_at is None:
        return current_principal
    observed_times: list[datetime] = []
    for row in snapshots:
        if not isinstance(row, dict):
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        timestamp = parse_datetime(
            row.get("source_as_of") or payload.get("asOf") or payload.get("sourceAsOf")
        )
        if timestamp is not None:
            observed_times.append(timestamp)
    if observed_times and min(observed_times) < current_principal_started_at:
        return None
    return current_principal


def _is_current_seeded_demo_history(snapshots: list[dict[str, Any]]) -> bool:
    """Seed history is an immutable reconstruction of the current seeded paper account."""
    if not snapshots:
        return False
    for row in snapshots:
        payload = row.get("payload") if isinstance(row, dict) else None
        if not isinstance(payload, dict):
            return False
        if not (
            payload.get("source") == "seeded-demo"
            or payload.get("dataOrigin") == "seeded-demo"
            or payload.get("seedProfile")
            or payload.get("seed_profile")
        ):
            return False
    return True


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
    normalized_payload = jsonable_encoder(payload)
    snapshots = getattr(app.state, "portfolio_holdings_snapshots", None)
    if not isinstance(snapshots, dict):
        snapshots = {}
        app.state.portfolio_holdings_snapshots = snapshots
    snapshots[user_sub] = normalized_payload
    try:
        from app.recommendations.routes import _repository_from_app

        repository = _repository_from_app(app)
        upsert_snapshot = getattr(repository, "upsert_portfolio_snapshot", None)
        if callable(upsert_snapshot):
            upsert_snapshot(user_sub, normalized_payload)
    except Exception:
        logger.exception("failed to persist normalized portfolio holdings snapshot")
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
