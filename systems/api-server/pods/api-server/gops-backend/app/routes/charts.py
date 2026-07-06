from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
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

from app.auth.dependencies import require_current_user
from app.auth.models import AuthenticatedUser
from app.market_data.query.service import get_query_service
from app.services.alfaka_market_data import (
    hot_symbol_summaries,
    ranking_symbol_summaries,
    replace_portfolio_subscription_symbols,
    replace_watchlist_symbols,
    search_symbol_summaries,
    symbol_summaries,
    watchlist_summaries,
)
from alfaka.serving.intervals import MAX_CHART_CANDLE_LIMIT

CHART_INTERVAL_PATTERN = "^(1m|5m|10m|1D|1W|1M|1d|1w|1mo|1MO|1month)$"

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


@router.get("/api/charts/candles")
def chart_candles(
    symbol: str = Query(min_length=1, max_length=12),
    interval: str = Query(default="1m", pattern=CHART_INTERVAL_PATTERN),
    ma: str = Query(default=""),
    limit: int | None = Query(default=None, ge=1, le=MAX_CHART_CANDLE_LIMIT),
    before: str | None = Query(default=None),
    from_time: str | None = Query(default=None, alias="from"),
    to_time: str | None = Query(default=None, alias="to"),
) -> dict[str, Any]:
    try:
        return get_query_service().candle_snapshot(symbol, interval, ma, limit, before=before, from_time=from_time, to_time=to_time)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Market data provider failed: {exc}") from exc


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
    query: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    # 프론트의 심볼 목록/요약 영역이 호출합니다.
    # 검색 후보는 현재 universe 기준이며, 최신 가격은 Redis/ClickHouse에서 보완합니다.
    return {
        "source": "alpaca",
        "feed": "configured-market-feed",
        "symbols": search_symbol_summaries(query, limit) if query is not None else symbol_summaries(limit),
    }


def parse_symbol_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]
