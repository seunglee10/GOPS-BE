from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request

from app.auth.dependencies import require_current_user
from app.auth.models import AuthenticatedUser
from app.market_data.calendar.service import next_market_open_payload
from app.market_data.query.service import get_query_service
from app.routes.simulator import simulator_gateway_from_app
from app.services.simulator_gateway import SimulatorUnavailable
from alfaka.serving.intervals import MAX_CHART_CANDLE_LIMIT, resolve_candle_limit

router = APIRouter()
CHART_INTERVAL_PATTERN = "^(1m|5m|10m|1h|4h|1D|1W|1M|1d|1w|1mo|1MO|1month)$"


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
def market_indices(background_tasks: BackgroundTasks) -> dict[str, Any]:
    return get_query_service().indices(background_tasks=background_tasks)


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
    return get_query_service().volume_profile_bins(
        symbol,
        from_time,
        to_time,
        price_bin_size,
        target_bins,
        price_min,
        price_max,
        interval=interval,
        candle_count=candle_count,
    )


@router.get("/api/charts/indicators")
def chart_indicators(
    symbol: str = Query(min_length=1, max_length=12),
    interval: str = Query(default="1m", pattern=CHART_INTERVAL_PATTERN),
    from_time: str | None = Query(default=None, alias="from"),
    to_time: str | None = Query(default=None, alias="to"),
    layers: str = Query(default="sma:5,sma:20,sma:60", max_length=256),
    limit: int = Query(default=300, ge=1, le=MAX_CHART_CANDLE_LIMIT),
) -> dict[str, Any]:
    return get_query_service().indicator_series(
        symbol,
        interval,
        from_time,
        to_time,
        layers,
        resolve_candle_limit(interval, limit),
    )


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
def chart_order_flow_symbols() -> dict[str, Any]:
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
    symbol: str = Query(min_length=1, max_length=12),
) -> dict[str, Any]:
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
