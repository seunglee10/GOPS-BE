from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request

from app.auth.dependencies import require_current_user
from app.auth.models import AuthenticatedUser
from app.market_data.calendar.service import next_market_open_payload
from app.market_data.indices.related import build_related_indices_payload
from app.market_data.query.service import get_query_service
from app.routes import charts as chart_routes
from app.routes.simulator import simulator_gateway_from_app
from app.services.simulator_gateway import SimulatorUnavailable
from alfaka.orderflow import ORDER_FLOW_CLASSIFICATION_VERSION, ORDER_FLOW_SIDE_CLASSIFICATION, price_bin_size_from_env
from alfaka.serving.intervals import MAX_CHART_CANDLE_LIMIT, resolve_candle_limit
from alfaka.serving.indicators import indicator_required_lookback_bars, indicator_specs_from_csv
from alfaka.serving.time_utils import parse_utc_time

router = APIRouter()
CHART_INTERVAL_PATTERN = "^(1m|5m|10m|1h|4h|1D|1W|1M|1d|1w|1mo|1MO|1month)$"
SIMULATION_DERIVED_REPLAY_LIMIT = 5_000


@router.get("/api/market/symbols")
def market_symbols(
    q: str = Query(default="", max_length=64),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100, alias="pageSize"),
) -> dict[str, Any]:
    return get_query_service().symbol_page(q, page, page_size)


@router.get("/api/market/symbols/search")
def market_symbol_search(
    q: str = Query(default="", max_length=64),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    return get_query_service().symbol_search(q, limit)


@router.get("/api/market/heatmap")
def market_heatmap(
    universe: str = Query(default="sp500", max_length=32),
) -> dict[str, Any]:
    return get_query_service().heatmap(universe)


@router.get("/api/market/fundamentals/{symbol}/series")
def market_fundamentals_series(
    symbol: str,
    years: int = Query(default=3, ge=1, le=10),
    period: str = Query(default="quarterly", pattern="^(quarterly|annual)$"),
) -> dict[str, Any]:
    return get_query_service().financial_series(symbol, years=years, period=period)


@router.get("/api/market/fundamentals/{symbol}/earnings")
def market_fundamentals_earnings(
    symbol: str,
    years: int = Query(default=3, ge=1, le=10),
) -> dict[str, Any]:
    return get_query_service().earnings_series(symbol, years=years)


@router.get("/api/market/indices")
def market_indices(request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    gateway = simulator_gateway_from_app(request.app)
    simulation_active = False
    try:
        simulation_active = gateway.status().get("mode") == "simulation"
        if simulation_active:
            return gateway.indices()
    except SimulatorUnavailable as exc:
        last_status = getattr(gateway, "last_status", None) or {}
        if simulation_active or last_status.get("mode") == "simulation":
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    return get_query_service().indices(background_tasks=background_tasks)


@router.get("/api/market/indices/related")
def market_related_indices(
    request: Request,
    background_tasks: BackgroundTasks,
    symbol: str = Query(min_length=1, max_length=12),
) -> dict[str, Any]:
    gateway = simulator_gateway_from_app(request.app)
    simulation_active = False
    try:
        status = gateway.status()
        simulation_active = status.get("mode") == "simulation"
        if simulation_active:
            virtual_time = str(status.get("virtualTime") or "").strip()
            try:
                reference_time = datetime.fromisoformat(virtual_time.replace("Z", "+00:00"))
            except ValueError as exc:
                raise HTTPException(status_code=503, detail="simulation_virtual_time_unavailable") from exc
            if reference_time.tzinfo is None:
                reference_time = reference_time.replace(tzinfo=timezone.utc)
            normalized_symbol = symbol.strip().upper()
            replay_symbol = next((
                item
                for item in status.get("symbols") or []
                if isinstance(item, dict) and str(item.get("symbol") or "").strip().upper() == normalized_symbol
            ), {})
            payload = build_related_indices_payload(
                normalized_symbol,
                indices_payload=gateway.indices(),
                provider=None,
                now=reference_time,
                company_change_percent=replay_symbol.get("changePercent"),
                use_stored_market_data=False,
            )
            return {
                **payload,
                "source": "simulation_replay_related",
                "simulation": True,
                "datasetId": status.get("datasetId"),
                "runId": status.get("runId"),
                "virtualTime": virtual_time,
            }
    except SimulatorUnavailable as exc:
        last_status = getattr(gateway, "last_status", None) or {}
        if simulation_active or last_status.get("mode") == "simulation":
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    return get_query_service().related_indices(symbol, background_tasks=background_tasks)


@router.get("/api/market/next-open")
def market_next_open(request: Request) -> dict[str, Any]:
    clock_provider = getattr(request.app.state, "market_clock_provider", None)
    return next_market_open_payload(clock_provider=clock_provider if callable(clock_provider) else None)


@router.get("/api/market/symbols/{symbol}")
def market_symbol_detail(symbol: str) -> dict[str, Any]:
    try:
        return get_query_service().symbol_detail(symbol)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/charts/volume-profile-bins")
def chart_volume_profile_bins(
    request: Request,
    symbol: str = Query(min_length=1, max_length=12),
    interval: str = Query(default="1m", pattern=CHART_INTERVAL_PATTERN),
    from_time: str = Query(alias="from"),
    to_time: str = Query(alias="to"),
    price_bin_size: str = Query(default="auto", alias="priceBinSize"),
    target_bins: int = Query(default=10, ge=4, le=48, alias="targetBins"),
    price_min: float | None = Query(default=None, alias="priceMin"),
    price_max: float | None = Query(default=None, alias="priceMax"),
    candle_count: int | None = Query(
        default=None,
        ge=1,
        le=MAX_CHART_CANDLE_LIMIT,
        alias="candleCount",
    ),
) -> dict[str, Any]:
    service = get_query_service()
    simulation_input = replay_derived_input(
        request,
        service,
        symbol=symbol,
        interval=interval,
        from_time=from_time,
        to_time=to_time,
        historical_from_time=from_time,
        historical_limit=resolve_candle_limit(interval, candle_count),
    )
    return service.volume_profile_bins(
        symbol,
        from_time,
        to_time,
        price_bin_size,
        target_bins,
        price_min,
        price_max,
        interval=interval,
        candle_count=candle_count,
        **simulation_input,
    )


@router.get("/api/charts/indicators")
def chart_indicators(
    request: Request,
    symbol: str = Query(min_length=1, max_length=12),
    interval: str = Query(default="1m", pattern=CHART_INTERVAL_PATTERN),
    from_time: str | None = Query(default=None, alias="from"),
    to_time: str | None = Query(default=None, alias="to"),
    layers: str = Query(default="sma:5,sma:20,sma:60", max_length=256),
    limit: int = Query(default=300, ge=1, le=MAX_CHART_CANDLE_LIMIT),
) -> dict[str, Any]:
    requested_limit = resolve_candle_limit(interval, limit)
    try:
        lookback = indicator_required_lookback_bars(indicator_specs_from_csv(layers))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    service = get_query_service()
    simulation_input = replay_derived_input(
        request,
        service,
        symbol=symbol,
        interval=interval,
        from_time=from_time,
        to_time=to_time,
        historical_from_time=None,
        historical_limit=min(SIMULATION_DERIVED_REPLAY_LIMIT, requested_limit + lookback),
    )
    return service.indicator_series(
        symbol,
        interval,
        from_time,
        to_time,
        layers,
        requested_limit,
        **simulation_input,
    )


def replay_derived_input(
    request: Request,
    service: Any,
    *,
    symbol: str,
    interval: str,
    from_time: str | None,
    to_time: str | None,
    historical_from_time: str | None,
    historical_limit: int,
) -> dict[str, Any]:
    gateway = simulator_gateway_from_app(request.app)
    try:
        status_payload = gateway.status()
    except SimulatorUnavailable as exc:
        last_status = getattr(gateway, "last_status", None) or {}
        if last_status.get("mode") == "simulation":
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {}
    if status_payload.get("mode") != "simulation":
        return {}

    dataset_id = str(status_payload.get("datasetId") or "").strip()
    run_id = str(status_payload.get("runId") or "").strip()
    if not dataset_id or not run_id:
        raise HTTPException(status_code=409, detail="simulation_data_unavailable")

    replay_start = chart_routes.SIMULATION_REPLAY_START
    requested_start = parse_utc_time(historical_from_time)
    requested_end = parse_utc_time(to_time)
    historical_end = min(requested_end, replay_start) if requested_end is not None else replay_start
    historical = {
        "symbol": symbol.upper(),
        "interval": interval,
        "source": "clickhouse",
        "feed": "sip",
        "indicators": {"ma": [], "volume": True},
        "candles": [],
    }
    if requested_start is None or requested_start < historical_end:
        historical = service.candle_snapshot(
            symbol,
            interval,
            "",
            max(1, int(historical_limit)),
            from_time=historical_from_time,
            to_time=historical_end.isoformat().replace("+00:00", "Z"),
        )

    if requested_end is not None and requested_end < replay_start:
        replay = {
            "symbol": symbol.upper(),
            "interval": interval,
            "source": "simulation_replay",
            "feed": "sip+boats",
            "simulation": True,
            "asOf": status_payload.get("virtualTime"),
            "candles": [],
        }
    else:
        replay = gateway.candles(
            symbol.strip().upper(),
            interval,
            SIMULATION_DERIVED_REPLAY_LIMIT,
        )
    merged = chart_routes._merge_simulation_candles(
        historical,
        replay,
        SIMULATION_DERIVED_REPLAY_LIMIT,
    )
    return {
        "candle_payload": merged,
        "cache_scope": f"simulation:{dataset_id}:{run_id}",
    }


@router.get("/api/charts/events")
def chart_events(
    request: Request,
    symbol: str = Query(min_length=1, max_length=12),
    from_time: str = Query(alias="from"),
    to_time: str = Query(alias="to"),
    locale: str = Query(default="ko-KR", min_length=2, max_length=16, pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$"),
    upcoming_days: int = Query(default=90, ge=1, le=365, alias="upcomingDays"),
) -> dict[str, Any]:
    return get_query_service().chart_events(
        symbol,
        from_time,
        to_time,
        locale=locale,
        upcoming_days=upcoming_days,
        now=chart_event_reference_time(request),
    )


def chart_event_reference_time(request: Request) -> datetime | None:
    try:
        status_payload = simulator_gateway_from_app(request.app).status()
    except SimulatorUnavailable:
        return None
    if status_payload.get("mode") != "simulation":
        return None
    virtual_time = str(status_payload.get("virtualTime") or "").strip()
    try:
        parsed = datetime.fromisoformat(virtual_time.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="simulation_virtual_time_unavailable") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@router.get("/api/charts/order-flow/symbols")
def chart_order_flow_symbols(request: Request) -> dict[str, Any]:
    gateway = simulator_gateway_from_app(request.app)
    try:
        status = gateway.status()
        if status.get("mode") == "simulation":
            replay_symbols = gateway.symbols(limit=100)
            symbols = [
                str(item.get("symbol") or "").strip().upper()
                for item in replay_symbols.get("symbols", [])
                if isinstance(item, dict) and item.get("symbol")
            ]
            return {
                "symbols": symbols,
                "priceBinSize": price_bin_size_from_env(),
                "sideClassification": ORDER_FLOW_SIDE_CLASSIFICATION,
                "classificationVersion": ORDER_FLOW_CLASSIFICATION_VERSION,
                "marketSession": "regular",
                "source": "simulation_replay",
                "simulation": True,
                "datasetId": status.get("datasetId"),
                "runId": status.get("runId"),
                "virtualTime": status.get("virtualTime"),
            }
    except SimulatorUnavailable as exc:
        if (getattr(gateway, "last_status", None) or {}).get("mode") == "simulation":
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    return get_query_service().order_flow_symbols()


@router.get("/api/charts/order-flow/daily")
def chart_order_flow_daily(
    symbol: str = Query(min_length=1, max_length=12),
    from_date: str = Query(alias="from"),
    to_date: str = Query(alias="to"),
    limit_days: int = Query(default=60, ge=1, le=250, alias="limitDays"),
) -> dict[str, Any]:
    try:
        return get_query_service().order_flow_daily(symbol, from_date, to_date, limit_days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/charts/order-flow/intraday")
def chart_order_flow_intraday(
    request: Request,
    symbol: str = Query(min_length=1, max_length=12),
) -> dict[str, Any]:
    gateway = simulator_gateway_from_app(request.app)
    try:
        if gateway.status().get("mode") == "simulation":
            return gateway.order_flow(symbol.strip().upper())
    except SimulatorUnavailable as exc:
        if (getattr(gateway, "last_status", None) or {}).get("mode") == "simulation":
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    return get_query_service().order_flow_intraday(symbol)


@router.get("/api/market/status")
def market_status() -> dict[str, Any]:
    return get_query_service().latest_status()


@router.get("/api/market/status/{symbol}")
def market_symbol_status(symbol: str) -> dict[str, Any]:
    return get_query_service().latest_status(symbol)


@router.get("/api/market/news/latest")
def market_latest_news(
    request: Request,
    symbol: str = Query(min_length=1, max_length=12),
    limit: int = Query(default=10, ge=1, le=30),
    locale: str = Query(default="ko-KR", max_length=16),
) -> dict[str, Any]:
    return get_query_service().latest_news(
        symbol,
        limit=limit,
        locale=locale,
        now=chart_event_reference_time(request),
    )


@router.get("/api/market/news/daily")
def market_daily_news(
    request: Request,
    symbol: str = Query(min_length=1, max_length=12),
    limit: int = Query(default=5, ge=1, le=30),
    locale: str = Query(default="ko-KR", max_length=16),
) -> dict[str, Any]:
    return get_query_service().daily_news(
        symbol,
        limit=limit,
        locale=locale,
        now=chart_event_reference_time(request),
    )


@router.get("/api/market/news/watchlist")
def market_watchlist_news(
    request: Request,
    limit: int = Query(default=30, ge=1, le=50),
    locale: str = Query(default="ko-KR", max_length=16),
    mode: str = Query(default="watchlist", pattern="^(watchlist|hot|popular|recommended|recommendation)$"),
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    resolved_mode = mode if isinstance(mode, str) else "watchlist"
    recommendation_repository = None
    if resolved_mode.strip().lower().replace("_", "-") in {"recommended", "recommendation"}:
        from app.recommendations.routes import _repository_from_app

        recommendation_repository = _repository_from_app(request.app)
    return get_query_service().watchlist_news(
        user.sub,
        limit=limit,
        locale=locale,
        mode=resolved_mode,
        recommendation_repository=recommendation_repository,
    )


@router.get("/api/agent/context/chart")
def agent_chart_context(
    symbol: str = Query(min_length=1, max_length=12),
    interval: str = Query(default="1m", pattern=CHART_INTERVAL_PATTERN),
    from_time: str = Query(alias="from"),
    to_time: str = Query(alias="to"),
    include: str = Query(default="volumeProfile,status,daily"),
) -> dict[str, Any]:
    return get_query_service().agent_chart_context(symbol, interval, from_time, to_time, include)
