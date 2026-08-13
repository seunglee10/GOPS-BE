from __future__ import annotations

import asyncio
import json
import logging
import os
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.encoders import jsonable_encoder
from starlette.websockets import WebSocketState

from app.auth.dependencies import (
    WebSocketAuthRequired,
    WebSocketAuthUnavailable,
    require_current_user,
    require_websocket_user,
)
from app.auth.models import AuthenticatedUser
from app.market_data.realtime.subscription_cohorts import RealtimeSubscriptionCohortService
from app.services.alfaka_market_data import get_market_data_provider
from app.services.portfolio_market_enrichment import enrich_holdings_with_market_stats
from kis_trader.domain.commands import validate_order_request_payload
from kis_trader.domain.status import OrderContractError
from kis_trader.paper import InMemoryPaperTradingRepository, PostgresPaperTradingRepository
from kis_trader.paper.fixture import HOLDING_BY_SYMBOL, SEED_PROFILE, configured_seed_profile, fallback_price
from kis_trader.paper.models import (
    PaperCapacityError,
    PaperIdempotencyConflictError,
    PaperOrderError,
    PaperOrderNotFoundError,
    PaperTradingRepository,
)
from kis_trader.security.idempotency import hash_idempotency_key, stable_body_hash
from kis_trader.security.validation import assert_no_forbidden_fields


router = APIRouter(tags=["paper-trading"])
logger = logging.getLogger(__name__)


@router.get("/api/paper/symbols/search")
def search_paper_symbols(
    request: Request,
    q: str = Query(default="", max_length=64),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    return {
        "source": "paper-symbol-registry",
        "query": q,
        "symbols": _search_paper_symbol_registry(request.app, q, limit),
    }


@router.get("/api/paper/account")
def paper_account(
    request: Request,
    current_user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    repository = paper_repository_from_app(request.app)
    snapshot = _enriched_account_snapshot(request.app, repository, current_user.sub)
    _sync_paper_portfolio_snapshot(request.app, repository, current_user.sub, snapshot)
    return jsonable_encoder(snapshot)


@router.get("/api/paper/account/balance")
def paper_balance(
    request: Request,
    symbol: str = "AAPL",
    exchange: str = "NASD",
    price: str = "0",
    current_user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    snapshot = paper_repository_from_app(request.app).account_snapshot(current_user.sub)
    available = Decimal(snapshot["account"]["available_cash"])
    numeric_price = _non_negative_decimal(price, "price")
    return jsonable_encoder({
        "env": "paper",
        "market": "overseas",
        "symbol": symbol.strip().upper(),
        "exchange": exchange.strip().upper(),
        "currency": "USD",
        "orderable_cash": available,
        "orderable_qty": int(available / numeric_price) if numeric_price > 0 else None,
    })


@router.post("/api/paper/account/reset")
async def reset_paper_account(
    request: Request,
    current_user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    payload = await _json_body(request)
    starting_cash = _positive_decimal(payload.get("starting_cash"), "starting_cash")
    repository = paper_repository_from_app(request.app)
    try:
        repository.reset_account(current_user.sub, starting_cash)
    except PaperOrderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _sync_paper_subscriptions(request.app, repository)
    snapshot = _enriched_account_snapshot(request.app, repository, current_user.sub)
    _sync_paper_portfolio_snapshot(request.app, repository, current_user.sub, snapshot)
    return jsonable_encoder(snapshot)


@router.post("/api/paper/risk/pretrade")
async def paper_risk_pretrade(
    request: Request,
    current_user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    payload = await _json_body(request)
    order_request = _validate_paper_order_payload(payload)
    _validate_paper_symbol(request.app, order_request.symbol)
    risk = paper_repository_from_app(request.app).pretrade(current_user.sub, order_request)
    return {"symbol": order_request.symbol, "side": order_request.side, "risk": jsonable_encoder(risk)}


@router.post("/api/paper/orders", status_code=status.HTTP_202_ACCEPTED)
async def create_paper_order(
    request: Request,
    response: Response,
    current_user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    payload = await _json_body(request)
    try:
        assert_no_forbidden_fields(payload)
    except OrderContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    order_request = _validate_paper_order_payload(payload)
    _validate_paper_symbol(request.app, order_request.symbol)
    repository = paper_repository_from_app(request.app)
    try:
        result = repository.create_order(
            user_id=current_user.sub,
            idempotency_key_hash=hash_idempotency_key(f"paper:{current_user.sub}:{idempotency_key}"),
            body_hash=stable_body_hash(payload),
            request=order_request,
        )
    except PaperIdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PaperCapacityError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except PaperOrderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result.idempotent_replay:
        response.headers["X-Idempotent-Replay"] = "true"
    else:
        _sync_paper_subscriptions(request.app, repository)
    return jsonable_encoder({**result.order, "idempotent_replay": result.idempotent_replay})


@router.get("/api/paper/orders")
def list_paper_orders(
    request: Request,
    order_status: str | None = None,
    include_previous: bool = False,
    limit: int = 100,
    current_user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    normalized_status = order_status.strip().lower() if order_status else None
    if normalized_status not in {None, "pending", "filled", "cancelled", "rejected"}:
        raise HTTPException(status_code=422, detail="unsupported paper order status")
    orders = paper_repository_from_app(request.app).list_orders(
        current_user.sub,
        status=normalized_status,
        include_previous=include_previous,
        limit=limit,
    )
    return {"orders": jsonable_encoder(orders)}


@router.get("/api/paper/orders/{order_id}")
def get_paper_order(
    order_id: str,
    request: Request,
    current_user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    order = paper_repository_from_app(request.app).get_order(current_user.sub, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="paper order not found")
    return jsonable_encoder(order)


@router.get("/api/paper/orders/{order_id}/events")
def get_paper_order_events(
    order_id: str,
    request: Request,
    current_user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    repository = paper_repository_from_app(request.app)
    if repository.get_order(current_user.sub, order_id) is None:
        raise HTTPException(status_code=404, detail="paper order not found")
    return {"order_id": order_id, "events": jsonable_encoder(repository.list_order_events(current_user.sub, order_id))}


@router.post("/api/paper/orders/{order_id}/cancel")
def cancel_paper_order(
    order_id: str,
    request: Request,
    current_user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    repository = paper_repository_from_app(request.app)
    try:
        order = repository.cancel_order(current_user.sub, order_id)
    except PaperOrderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _sync_paper_subscriptions(request.app, repository)
    return jsonable_encoder(order)


@router.websocket("/ws/paper/orders/{order_id}")
async def paper_order_socket(websocket: WebSocket, order_id: str) -> None:
    user = await _accept_paper_socket(websocket)
    if user is None:
        return
    try:
        repository = paper_repository_from_app(websocket.app)
        last_fingerprint: str | None = None
        while True:
            order = await asyncio.to_thread(repository.get_order, user.sub, order_id)
            if order is None:
                await websocket.send_json({"type": "error", "detail": "paper order not found"})
                await websocket.close(code=1008)
                return
            events = await asyncio.to_thread(repository.list_order_events, user.sub, order_id)
            payload = {
                "type": "snapshot" if last_fingerprint is None else "update",
                "order": jsonable_encoder(order),
                "events": jsonable_encoder(events),
            }
            fingerprint = _fingerprint(payload)
            if fingerprint != last_fingerprint:
                await websocket.send_json(payload)
                last_fingerprint = fingerprint
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        return
    except Exception as exc:
        await _close_socket_with_error(websocket, exc)


@router.websocket("/ws/paper/account")
async def paper_account_socket(websocket: WebSocket) -> None:
    user = await _accept_paper_socket(websocket)
    if user is None:
        return
    try:
        repository = paper_repository_from_app(websocket.app)
        last_fingerprint: str | None = None
        last_portfolio_fingerprint: str | None = None
        while True:
            raw_account = await asyncio.to_thread(
                _enriched_account_snapshot,
                websocket.app,
                repository,
                user.sub,
            )
            portfolio_fingerprint = _paper_portfolio_state_fingerprint(raw_account)
            if portfolio_fingerprint != last_portfolio_fingerprint:
                await asyncio.to_thread(
                    _sync_paper_portfolio_snapshot,
                    websocket.app,
                    repository,
                    user.sub,
                    raw_account,
                )
                last_portfolio_fingerprint = portfolio_fingerprint
            account = jsonable_encoder(raw_account)
            payload = {"type": "snapshot" if last_fingerprint is None else "update", "account": account}
            fingerprint = _fingerprint(payload)
            if fingerprint != last_fingerprint:
                await websocket.send_json(payload)
                last_fingerprint = fingerprint
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        return
    except Exception as exc:
        await _close_socket_with_error(websocket, exc)


def paper_repository_from_app(app: Any) -> PaperTradingRepository:
    existing = getattr(app.state, "paper_trading_repository", None)
    if existing is not None:
        return existing
    mode = os.getenv("PAPER_REPOSITORY", "postgres").strip().lower()
    if mode == "memory":
        repository: PaperTradingRepository = InMemoryPaperTradingRepository(
            seed_profile=configured_seed_profile(),
        )
    else:
        if not os.getenv("DATABASE_URL") and not all(
            os.getenv(name) for name in ("DATABASE_HOST", "DATABASE_NAME", "DATABASE_USER", "DATABASE_PASSWORD")
        ):
            raise HTTPException(status_code=503, detail="DATABASE_URL or DATABASE_* settings are required for paper trading")
        repository = PostgresPaperTradingRepository.from_env()
    app.state.paper_trading_repository = repository
    return repository


def _sync_paper_portfolio_snapshot(
    app: Any,
    repository: PaperTradingRepository,
    user_id: str,
    snapshot: dict[str, Any],
) -> None:
    try:
        from app.routes.account import _paper_portfolio_payload, _remember_portfolio_holdings_snapshot

        payload = _paper_portfolio_payload(app, repository, user_id, snapshot)
        _remember_portfolio_holdings_snapshot(app, user_id, payload)
    except Exception:
        logger.exception("failed to synchronize paper account with portfolio snapshot")


def _paper_portfolio_state_fingerprint(snapshot: dict[str, Any]) -> str:
    account = snapshot.get("account") or {}
    return _fingerprint({
        "generation": account.get("generation"),
        "cash": account.get("cash_balance"),
        "reservedCash": account.get("reserved_cash"),
        "positions": [
            [
                position.get("symbol"),
                position.get("qty"),
                position.get("reserved_qty"),
                position.get("average_price"),
                position.get("realized_pnl"),
            ]
            for position in snapshot.get("positions") or []
        ],
        "openOrders": [
            [order.get("order_id"), order.get("status"), order.get("qty")]
            for order in snapshot.get("open_orders") or []
        ],
    })


def _validate_paper_order_payload(payload: dict[str, Any]):
    try:
        return validate_order_request_payload(payload, default_account_alias="paper-account")
    except OrderContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _validate_paper_symbol(app: Any, symbol: str) -> dict[str, Any]:
    validator = getattr(app.state, "paper_symbol_validator", None)
    if callable(validator):
        result = validator(symbol)
        if result is False:
            raise HTTPException(status_code=422, detail=f"unsupported paper trading symbol: {symbol}")
        metadata = result if isinstance(result, dict) else {
            "symbol": symbol,
            "assetClass": "us_equity",
            "tradable": True,
            "status": "active",
        }
        if not _paper_symbol_is_eligible(metadata):
            raise HTTPException(status_code=422, detail=f"unsupported paper trading symbol: {symbol}")
        return metadata
    try:
        metadata = get_market_data_provider().symbol_detail(symbol)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"unknown paper trading symbol: {symbol}") from exc
    if not _paper_symbol_is_eligible(metadata):
        raise HTTPException(status_code=422, detail=f"unsupported paper trading symbol: {symbol}")
    return metadata


def _search_paper_symbol_registry(app: Any, query: str, limit: int) -> list[dict[str, Any]]:
    normalized_query = query.strip().upper()
    candidate_limit = min(max(limit * 5, 100), 500)
    injected = getattr(app.state, "paper_symbol_search", None)
    if callable(injected):
        records = injected(normalized_query, candidate_limit)
    else:
        provider = get_market_data_provider()
        clickhouse_provider = getattr(provider, "clickhouse_provider", None)
        direct_search = getattr(clickhouse_provider, "search_symbols", None)
        try:
            records = direct_search(normalized_query, candidate_limit) if callable(direct_search) else []
        except Exception:
            records = []
        if not records:
            records = provider.search_symbols(normalized_query, candidate_limit)

    eligible: dict[str, dict[str, Any]] = {}
    for raw in records or []:
        if not isinstance(raw, dict) or not _paper_symbol_is_eligible(raw):
            continue
        symbol = str(raw.get("symbol") or "").strip().upper()
        if not symbol or symbol in eligible:
            continue
        eligible[symbol] = {**raw, "symbol": symbol}
    ordered = sorted(
        eligible.values(),
        key=lambda item: (_paper_symbol_search_score(item, normalized_query), item["symbol"]),
    )
    return ordered[:limit]


def _paper_symbol_is_eligible(metadata: dict[str, Any]) -> bool:
    asset_class = str(
        metadata.get("assetClass") or metadata.get("asset_class") or metadata.get("class") or ""
    ).strip().lower()
    symbol_status = str(metadata.get("status") or "").strip().lower()
    return (
        metadata.get("tradable") is True
        and symbol_status == "active"
        and asset_class in {"us_equity", "us_etf"}
    )


def _paper_symbol_search_score(metadata: dict[str, Any], query: str) -> int:
    if not query:
        return 0
    symbol = str(metadata.get("symbol") or "").upper()
    name = str(metadata.get("name") or "").upper()
    if symbol == query:
        return 0
    if symbol.startswith(query):
        return 1
    if name.startswith(query):
        return 2
    if query in symbol:
        return 3
    if query in name:
        return 4
    return 5


def _enriched_account_snapshot(app: Any, repository: PaperTradingRepository, user_id: str) -> dict[str, Any]:
    from app.routes.simulator import simulator_mode_active

    simulation_active = simulator_mode_active(app)
    snapshot = repository.account_snapshot(user_id)
    raw_positions = [dict(raw) for raw in snapshot.get("positions") or []]
    simulation_quotes = (
        _simulation_position_quotes(app, [str(position.get("symbol") or "") for position in raw_positions])
        if simulation_active and raw_positions
        else {}
    )
    positions = []
    market_value = Decimal("0")
    unrealized_total = Decimal("0")
    realized_total = Decimal(snapshot["account"].get("realized_pnl") or 0)
    for position in raw_positions:
        qty = Decimal(position.get("qty") or 0)
        average_price = Decimal(position.get("average_price") or 0)
        current_price, price_source, price_timestamp = _position_price(
            app,
            position["symbol"],
            average_price,
            simulation_active=simulation_active,
            simulation_quotes=simulation_quotes,
        )
        value = qty * current_price
        cost = qty * average_price
        unrealized = value - cost
        market_value += value
        unrealized_total += unrealized
        positions.append({
            **position,
            **_fixture_position_fields(
                position["symbol"],
                qty,
                include_market_facts=not simulation_active,
                include_dividend_facts=simulation_active,
            ),
            "available_qty": qty - Decimal(position.get("reserved_qty") or 0),
            "current_price": current_price,
            "price_source": price_source,
            "price_timestamp": price_timestamp,
            "market_value": value,
            "cost_basis": cost,
            "unrealized_pnl": unrealized,
            "unrealized_pnl_rate": (unrealized / cost * 100) if cost > 0 else Decimal("0"),
        })
    if not simulation_active:
        positions = _enrich_position_market_facts(app, positions)
    account = dict(snapshot["account"])
    cash_balance = Decimal(account["cash_balance"])
    equity = cash_balance + market_value
    starting_cash = Decimal(account["starting_cash"])
    return {
        **snapshot,
        "account": {
            **account,
            "market_value": market_value,
            "equity": equity,
            "unrealized_pnl": unrealized_total,
            "realized_pnl": realized_total,
            "total_pnl": equity - starting_cash,
            "total_pnl_rate": ((equity - starting_cash) / starting_cash * 100) if starting_cash > 0 else Decimal("0"),
        },
        "positions": positions,
    }


def _position_price(
    app: Any,
    symbol: str,
    fallback: Decimal,
    *,
    simulation_active: bool,
    simulation_quotes: dict[str, dict[str, Any]] | None = None,
) -> tuple[Decimal, str, str | None]:
    if simulation_active:
        quote = (simulation_quotes or {}).get(symbol.strip().upper()) or {}
        replay_price = _optional_positive_decimal(quote.get("bid") or quote.get("ask"))
        if replay_price is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "simulation_quote_not_ready", "symbols": [symbol.strip().upper()], "retryable": True},
            )
        return replay_price, "simulation_replay", quote.get("virtualTime")

    resolver = getattr(app.state, "paper_price_resolver", None)
    if callable(resolver):
        resolved = resolver(symbol)
        if isinstance(resolved, dict):
            value = _optional_positive_decimal(resolved.get("price"))
            if value is not None:
                return value, str(resolved.get("source") or "injected"), resolved.get("timestamp")
        else:
            value = _optional_positive_decimal(resolved)
            if value is not None:
                return value, "injected", None
    try:
        provider = getattr(app.state, "portfolio_market_data_provider", None) or get_market_data_provider()
        trade = provider.redis_provider.live_trade(symbol) or {}
        live_price = _optional_positive_decimal(trade.get("price"))
        if live_price is not None:
            return live_price, "redis.live_trade", trade.get("timestamp")
        candles = provider.candle_snapshot(symbol, "1m", 1, ma_windows=()).get("candles") or []
        if candles:
            close = _optional_positive_decimal(candles[-1].get("close"))
            if close is not None:
                return close, "market.latest_candle", candles[-1].get("timestamp")
    except Exception:
        pass
    fixture_value = fallback_price(symbol)
    if fixture_value is not None:
        return fixture_value, "seeded-demo", None
    return fallback, "average_price", None


def _simulation_position_quotes(app: Any, symbols: list[str]) -> dict[str, dict[str, Any]]:
    from app.routes.simulator import simulator_gateway_from_app
    from app.services.simulator_gateway import SimulatorTimeout, SimulatorUnavailable

    normalized = list(dict.fromkeys(
        symbol.strip().upper()
        for symbol in symbols
        if symbol.strip()
    ))
    try:
        payload = simulator_gateway_from_app(app).quotes(normalized)
    except SimulatorTimeout as exc:
        raise HTTPException(
            status_code=504,
            detail={"code": "simulation_quote_timeout", "symbols": normalized, "retryable": True},
        ) from exc
    except SimulatorUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "simulation_service_unavailable", "symbols": normalized, "retryable": True},
        ) from exc

    raw_quotes = payload.get("quotes") if isinstance(payload, dict) else None
    quotes = {
        str(symbol).strip().upper(): quote
        for symbol, quote in (raw_quotes.items() if isinstance(raw_quotes, dict) else [])
        if isinstance(quote, dict)
    }
    missing = [
        symbol
        for symbol in normalized
        if symbol not in quotes
        or _optional_positive_decimal(quotes[symbol].get("bid") or quotes[symbol].get("ask")) is None
    ]
    if missing:
        raise HTTPException(
            status_code=409,
            detail={"code": "simulation_quote_not_ready", "symbols": missing, "retryable": True},
        )
    return quotes


def _fixture_position_fields(
    symbol: str,
    quantity: Decimal,
    *,
    include_market_facts: bool = True,
    include_dividend_facts: bool = False,
) -> dict[str, Any]:
    holding = HOLDING_BY_SYMBOL.get(str(symbol).strip().upper())
    if holding is None:
        return {}
    identity = {
        "name": holding.name,
        "market": "overseas",
        "exchange": holding.exchange,
        "currency": "USD",
        "sector": holding.sector,
        "industry": holding.industry,
        "metadata_source": SEED_PROFILE,
    }
    dividend_facts = {
        "dividend_yield": holding.dividend_yield,
        "dividend_per_share": holding.dividend_per_share,
        "annual_dividend": holding.dividend_per_share * quantity if holding.dividend_per_share is not None else None,
    }
    if not include_market_facts:
        return {**identity, **dividend_facts} if include_dividend_facts else identity
    day_pnl_rate = holding.day_pnl_rate
    day_pnl = (
        quantity * holding.fallback_price * day_pnl_rate / Decimal("100")
        if day_pnl_rate is not None else None
    )
    return {
        **identity,
        "pe_ratio": holding.pe_ratio,
        "eps_ttm": holding.eps_ttm,
        "low_52": holding.low_52,
        "high_52": holding.high_52,
        **dividend_facts,
        "day_pnl_rate": day_pnl_rate,
        "day_pnl": day_pnl,
    }


def _enrich_position_market_facts(app: Any, positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload = {
        "positions": [{
            "symbol": row.get("symbol"),
            "currentPrice": row.get("current_price"),
            "peRatio": row.get("pe_ratio"),
            "epsTtm": row.get("eps_ttm"),
            "low52": row.get("low_52"),
            "high52": row.get("high_52"),
        } for row in positions],
    }
    try:
        enriched = enrich_holdings_with_market_stats(app, payload).get("positions") or []
    except Exception:
        return positions
    by_symbol = {str(row.get("symbol") or "").upper(): row for row in enriched if isinstance(row, dict)}
    field_map = {
        "peRatio": "pe_ratio",
        "epsTtm": "eps_ttm",
        "low52": "low_52",
        "high52": "high_52",
        "marketStatsAsOf": "market_stats_as_of",
        "stats52wSource": "stats_52w_source",
        "fundamentalsSource": "fundamentals_source",
        "fundamentalsAsOf": "fundamentals_as_of",
    }
    result = []
    for position in positions:
        override = by_symbol.get(str(position.get("symbol") or "").upper(), {})
        rendered = dict(position)
        for source_field, target_field in field_map.items():
            if override.get(source_field) is not None:
                rendered[target_field] = override[source_field]
        result.append(rendered)
    return result


def _sync_paper_subscriptions(app: Any, repository: PaperTradingRepository) -> None:
    order_symbols = repository.active_order_symbols()
    position_symbols = repository.active_position_symbols()
    injected = getattr(app.state, "paper_subscription_sync", None)
    if callable(injected):
        injected(order_symbols, position_symbols)
        return
    try:
        provider = get_market_data_provider()
        service = RealtimeSubscriptionCohortService(provider.redis_provider.redis, auto_reconcile=False)
        service.replace_paper_order_source(order_symbols)
        service.replace_paper_portfolio_source(position_symbols)
    except Exception:
        # The matcher also reconciles these sets at startup and after fills.
        return


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="request body must be a JSON object")
    return payload


def _positive_decimal(value: Any, field: str) -> Decimal:
    parsed = _optional_positive_decimal(value)
    if parsed is None:
        raise HTTPException(status_code=422, detail=f"{field} must be a positive decimal")
    return parsed


def _non_negative_decimal(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"{field} must be a decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise HTTPException(status_code=422, detail=f"{field} must be non-negative")
    return parsed


def _optional_positive_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


async def _accept_paper_socket(websocket: WebSocket) -> AuthenticatedUser | None:
    try:
        user = await asyncio.to_thread(require_websocket_user, websocket)
    except WebSocketAuthRequired as exc:
        await _reject_paper_socket(websocket, str(exc), code=1008)
        return None
    except WebSocketAuthUnavailable as exc:
        await _reject_paper_socket(websocket, str(exc), code=1011)
        return None
    await websocket.accept()
    return user


async def _reject_paper_socket(websocket: WebSocket, detail: str, *, code: int) -> None:
    try:
        await websocket.accept()
        await websocket.send_json({"type": "error", "detail": detail})
        await websocket.close(code=code)
    except (RuntimeError, WebSocketDisconnect):
        return


async def _close_socket_with_error(websocket: WebSocket, exc: Exception) -> None:
    try:
        if websocket.client_state != WebSocketState.DISCONNECTED:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            await websocket.send_json({"type": "error", "detail": detail})
            await websocket.close(code=1011)
    except Exception:
        return


def _fingerprint(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
