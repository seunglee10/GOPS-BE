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
    symbol_summaries,
)

router = APIRouter()


class BackfillRequestBody(BaseModel):
    symbol: str = Field(min_length=1, max_length=12)
    interval: str = Field(default="1m", pattern="^(1m|5m|10m|1d)$")
    start: str | None = None
    end: str | None = None
    mode: str = "default"


@router.get("/api/charts/candles")
def chart_candles(
    symbol: str = Query(default="AAPL", min_length=1, max_length=12),
    interval: str = Query(default="1m", pattern="^(1m|5m|10m|1d)$"),
    ma: str = Query(default="5,20,60"),
    limit: int = Query(default=96, ge=30, le=240),
) -> dict[str, Any]:
    try:
        return get_query_service().candle_snapshot(symbol, interval, ma, limit)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Market data provider failed: {exc}") from exc


@router.post("/api/charts/backfill")
def chart_backfill(body: BackfillRequestBody) -> dict[str, Any]:
    return get_query_service().request_backfill(body.symbol, body.interval, start=body.start, end=body.end, mode=body.mode)


@router.get("/api/charts/backfill/status")
def chart_backfill_status(
    symbol: str = Query(min_length=1, max_length=12),
    interval: str = Query(default="1m", pattern="^(1m|5m|10m|1d)$"),
    request_id: str | None = Query(default=None, alias="requestId"),
) -> dict[str, Any]:
    return get_query_service().backfill_status(symbol, interval, request_id=request_id)


@router.get("/api/charts/symbols")
def chart_symbols() -> dict[str, Any]:
    # 프론트의 심볼 목록/요약 영역이 호출합니다.
    # 심볼 목록은 ALPACA_SYMBOLS 기준이며, 최신 가격은 Redis에 있으면 같이 내려갑니다.
    return {
        "source": "alpaca",
        "feed": "configured-market-feed",
        "isSynthetic": False,
        "symbols": symbol_summaries(),
    }
