from typing import Any

from fastapi import APIRouter, HTTPException, Query
try:
    from pydantic import BaseModel, Field
except Exception:
    class BaseModel:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    def Field(default=None, **kwargs):
        return default

from app.market_data.query.service import get_query_service
from app.services.alfaka_market_data import (
    search_symbol_summaries,
    symbol_summaries,
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


@router.get("/api/charts/candles")
def chart_candles(
    symbol: str = Query(min_length=1, max_length=12),
    interval: str = Query(default="1m", pattern=CHART_INTERVAL_PATTERN),
    ma: str = Query(default="5,20,60"),
    limit: int | None = Query(default=None, ge=1, le=MAX_CHART_CANDLE_LIMIT),
    before: str | None = Query(default=None),
    from_time: str | None = Query(default=None, alias="from"),
    to_time: str | None = Query(default=None, alias="to"),
) -> dict[str, Any]:
    try:
        return get_query_service().candle_snapshot(symbol, interval, ma, limit, before=before, from_time=from_time, to_time=to_time)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Market data provider failed: {exc}") from exc


@router.post("/api/charts/backfill")
def chart_backfill(body: BackfillRequestBody) -> dict[str, Any]:
    return get_query_service().request_backfill(body.symbol, body.interval, start=body.start, end=body.end, mode=body.mode, force=body.force)


@router.get("/api/charts/backfill/status")
def chart_backfill_status(
    symbol: str = Query(min_length=1, max_length=12),
    interval: str = Query(default="1m", pattern=CHART_INTERVAL_PATTERN),
    request_id: str | None = Query(default=None, alias="requestId"),
) -> dict[str, Any]:
    return get_query_service().backfill_status(symbol, interval, request_id=request_id)


@router.get("/api/charts/backfill/queue")
def chart_backfill_queue() -> dict[str, Any]:
    return get_query_service().backfill_queue_metrics()


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
        "symbols": search_symbol_summaries(query, limit) if query is not None else symbol_summaries(),
    }
