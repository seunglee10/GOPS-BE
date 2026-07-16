import os
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
try:
    from pydantic import BaseModel, Field
except Exception:
    class BaseModel:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    def Field(default=None, **kwargs):
        if "default_factory" in kwargs and default is None:
            return kwargs["default_factory"]()
        return default

from app.auth.dependencies import optional_current_user, require_current_user
from app.auth.models import AuthenticatedUser
from app.market_data.compare.service import get_chart_compare_service
from app.market_data.realtime.active_symbols import ActiveSymbolManager
from app.market_data.query.service import get_query_service
from app.routes.simulator import simulator_gateway_from_app, simulator_mode_active
from app.services.alfaka_market_data import (
    get_market_data_provider,
    hot_symbol_summaries,
    normalize_market_symbol,
    ranking_symbol_summaries,
    replace_portfolio_subscription_symbols,
    replace_watchlist_symbols,
    search_symbol_summaries,
    symbol_summaries,
    watchlist_summaries,
)
from alfaka.analytics.analysis_candles import canonicalize_candle_identity
from alfaka.serving.intervals import MAX_CHART_CANDLE_LIMIT
from alfaka.serving.intervals import normalize_chart_interval

CHART_INTERVAL_PATTERN = "^(1m|5m|10m|1h|4h|1D|1W|1M|1d|1w|1mo|1MO|1month)$"
CHART_COMPARE_RANGE_PATTERN = "^(1D|1M|6M|1Y|5Y|1d|1m|6m|1y|5y)$"
SIMULATION_REPLAY_START = datetime(2026, 7, 14, 15, 0, tzinfo=UTC)
SIMULATION_REPLAY_MARKET_DATE = SIMULATION_REPLAY_START.astimezone(ZoneInfo("America/New_York")).date().isoformat()


def positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


PUBLIC_CHART_CANDLE_LIMIT = min(MAX_CHART_CANDLE_LIMIT, positive_int_env("CHART_API_MAX_LIMIT", 2000))

router = APIRouter()


class BackfillRequestBody(BaseModel):
    symbol: str = Field(min_length=1, max_length=12)
    interval: str = Field(default="1m", pattern=CHART_INTERVAL_PATTERN)
    start: str | None = None
    end: str | None = None
    mode: str = "default"
    force: bool = False


class SymbolListRequestBody(BaseModel):
    symbols: list[str] = Field(default_factory=list)


class ActiveChartHeartbeatBody(BaseModel):
    symbol: str = Field(min_length=1, max_length=12)
    sessionId: str | None = Field(default=None, max_length=120)
    ttlSeconds: int | None = Field(default=None, ge=15, le=300)


@router.get("/api/charts/candles")
def chart_candles(
    request: Request = None,
    symbol: str = Query(min_length=1, max_length=12),
    interval: str = Query(default="1m", pattern=CHART_INTERVAL_PATTERN),
    ma: str = Query(default=""),
    limit: int | None = Query(default=None, ge=1, le=PUBLIC_CHART_CANDLE_LIMIT),
    before: str | None = Query(default=None),
    from_time: str | None = Query(default=None, alias="from"),
    to_time: str | None = Query(default=None, alias="to"),
    include_previous_close: bool = Query(default=False, alias="includePreviousClose"),
) -> dict[str, Any]:
    try:
        if request is not None and simulator_mode_active(request.app):
            replay_limit = limit or PUBLIC_CHART_CANDLE_LIMIT
            replay = simulator_gateway_from_app(request.app).candles(symbol.upper(), interval, replay_limit)
            historical = get_query_service().candle_snapshot(
                symbol,
                interval,
                ma,
                replay_limit,
                before=before,
                from_time=from_time,
                to_time=SIMULATION_REPLAY_START.isoformat().replace("+00:00", "Z"),
                include_previous_close=include_previous_close,
            )
            return _merge_simulation_candles(historical, replay, replay_limit)
        return get_query_service().candle_snapshot(
            symbol,
            interval,
            ma,
            limit,
            before=before,
            from_time=from_time,
            to_time=to_time,
            include_previous_close=include_previous_close,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Market data provider failed: {exc}") from exc


@router.post("/api/charts/active-symbol")
def chart_active_symbol_heartbeat(
    body: ActiveChartHeartbeatBody,
    request: Request = None,
    user: AuthenticatedUser | None = Depends(optional_current_user),
) -> dict[str, Any]:
    try:
        symbol = normalize_market_symbol(body.symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if request is not None and simulator_mode_active(request.app):
        available = simulator_gateway_from_app(request.app).symbols(symbol, 100).get("symbols", [])
        if not any(str(item.get("symbol") or "").upper() == symbol for item in available if isinstance(item, dict)):
            raise HTTPException(status_code=404, detail="symbol is not available in the active replay dataset")
        return {
            "symbol": symbol,
            "sessionId": body.sessionId or "chart-simulation-http",
            "ttlSeconds": body.ttlSeconds or 60,
            "layers": ["candles", "trades", "quotes"],
            "pendingReconcile": False,
            "simulation": True,
        }
    provider = get_market_data_provider()
    redis_client = getattr(getattr(provider, "redis_provider", None), "redis", None)
    if redis_client is None:
        raise HTTPException(status_code=503, detail="Realtime subscription Redis is unavailable.")
    manager = ActiveSymbolManager(redis_client, ttl_seconds=body.ttlSeconds)
    session_id = body.sessionId or "chart-active-http"
    user_id = user.sub if user else "anonymous"
    manager.refresh(user_id, session_id, symbol)
    return {
        "symbol": symbol,
        "sessionId": session_id,
        "ttlSeconds": manager.ttl_seconds,
        "layers": ["candles", "trades", "quotes"],
        "pendingReconcile": True,
    }


@router.get("/api/charts/compare")
def chart_compare(
    symbols: str = Query(min_length=1, max_length=160),
    range_value: str = Query(default="1D", alias="range", pattern=CHART_COMPARE_RANGE_PATTERN),
    base_mode: str = Query(default="first_close", alias="baseMode", pattern="^(first_close)$"),
    adjustment: str = Query(default="split", pattern="^(split)$"),
    session: str = Query(default="regular", pattern="^(regular)$"),
) -> dict[str, Any]:
    try:
        requested_symbols = parse_symbol_csv(symbols)
        return get_chart_compare_service().snapshot(
            requested_symbols,
            range_value,
            base_mode=base_mode,
            adjustment=adjustment,
            session=session,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Chart comparison provider failed: {exc}") from exc


@router.post("/api/charts/backfill")
def chart_backfill(body: BackfillRequestBody) -> dict[str, Any]:
    raise HTTPException(
        status_code=410,
        detail="Backfill queue endpoints were replaced by on-demand fill in GET /api/charts/candles.",
    )


@router.get("/api/charts/backfill/status")
def chart_backfill_status(
    symbol: str = Query(min_length=1, max_length=12),
    interval: str = Query(default="1m", pattern=CHART_INTERVAL_PATTERN),
    request_id: str | None = Query(default=None, alias="requestId"),
) -> dict[str, Any]:
    raise HTTPException(
        status_code=410,
        detail="Backfill status was replaced by per-request on-demand fill trace in GET /api/charts/candles.",
    )


@router.get("/api/charts/backfill/queue")
def chart_backfill_queue() -> dict[str, Any]:
    raise HTTPException(
        status_code=410,
        detail="Backfill queue metrics were replaced by per-request on-demand fill trace.",
    )


@router.get("/api/charts/watchlist")
def chart_watchlist(
    symbols: str | None = Query(default=None, max_length=512),
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    requested = parse_symbol_csv(symbols) if symbols is not None else None
    return watchlist_summaries(requested, user_id=user.sub)


@router.put("/api/charts/watchlist")
def chart_watchlist_replace(body: SymbolListRequestBody, user: AuthenticatedUser = Depends(require_current_user)) -> dict[str, Any]:
    return replace_watchlist_symbols(user.sub, body.symbols)


@router.get("/api/charts/hot-symbols")
def chart_hot_symbols(
    limit: int = Query(default=10, ge=1, le=10),
) -> dict[str, Any]:
    return hot_symbol_summaries(limit)


@router.get("/api/charts/rankings")
def chart_rankings(
    kind: str = Query(default="dollar-volume", pattern="^(dollar-volume|dollar|volume|gainers|gainer|losers|loser)$"),
    limit: int = Query(default=10, ge=1, le=10),
) -> dict[str, Any]:
    try:
        return ranking_symbol_summaries(kind, limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/charts/subscription-cohorts/portfolio")
def chart_portfolio_subscription_cohort(body: SymbolListRequestBody, user: AuthenticatedUser = Depends(require_current_user)) -> dict[str, Any]:
    return replace_portfolio_subscription_symbols(user.sub, body.symbols)


@router.get("/api/charts/symbols")
def chart_symbols(
    request: Request = None,
    query: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    # 프론트의 심볼 목록/요약 영역이 호출합니다.
    # 검색 후보는 현재 universe 기준이며, 최신 가격은 Redis/ClickHouse에서 보완합니다.
    if request is not None and simulator_mode_active(request.app):
        try:
            return simulator_gateway_from_app(request.app).symbols(query or "", limit)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "source": "alpaca",
        "feed": "configured-market-feed",
        "symbols": search_symbol_summaries(query, limit) if query is not None else symbol_summaries(limit),
    }


def parse_symbol_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _merge_simulation_candles(
    historical: dict[str, Any],
    replay: dict[str, Any],
    limit: int,
) -> dict[str, Any]:
    interval = normalize_chart_interval(replay.get("interval") or historical.get("interval") or "1m")
    merged: dict[str, dict[str, Any]] = {}
    for candle in historical.get("candles") or []:
        if not isinstance(candle, dict):
            continue
        timestamp = _candle_timestamp(candle)
        if timestamp is not None and _historical_candle_ends_before_replay(candle, interval, timestamp):
            key, normalized = _simulation_candle_identity(candle, interval)
            if key is not None:
                merged[key] = normalized
    for candle in replay.get("candles") or []:
        if isinstance(candle, dict) and candle.get("timestamp"):
            key, normalized = _simulation_candle_identity(candle, interval)
            if key is not None:
                merged[key] = normalized
    candles = sorted(merged.values(), key=lambda item: _candle_timestamp(item) or datetime.min.replace(tzinfo=UTC))[-limit:]
    return {
        **historical,
        **replay,
        "simulation": True,
        "candles": candles,
        "futureDataCutoff": replay.get("asOf"),
    }


def _simulation_candle_identity(candle: dict[str, Any], interval: str) -> tuple[str | None, dict[str, Any]]:
    if interval != "1D":
        timestamp = str(candle.get("timestamp") or "").strip()
        return (timestamp or None), candle
    normalized = canonicalize_candle_identity(candle, interval)
    if normalized is None:
        return None, candle
    return str(normalized["candleKey"]), normalized


def _historical_candle_ends_before_replay(
    candle: dict[str, Any],
    interval: str,
    timestamp: datetime,
) -> bool:
    if interval != "1D":
        return timestamp < SIMULATION_REPLAY_START
    normalized = canonicalize_candle_identity(candle, interval)
    return bool(normalized and str(normalized["candleKey"]) < SIMULATION_REPLAY_MARKET_DATE)


def _candle_timestamp(candle: dict[str, Any]) -> datetime | None:
    value = str(candle.get("timestamp") or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
