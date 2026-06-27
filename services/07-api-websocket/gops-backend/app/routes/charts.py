from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.services.alfaka_market_data import (
    get_market_data_provider,
    normalize_market_symbol,
    requested_ma_from_csv,
    symbol_summaries,
)

router = APIRouter()


@router.get("/api/charts/candles")
def chart_candles(
    symbol: str = Query(default="AAPL", min_length=1, max_length=12),
    interval: str = Query(default="1m", pattern="^(1m|5m|10m)$"),
    ma: str = Query(default="5,20,60"),
    limit: int = Query(default=96, ge=30, le=240),
) -> dict[str, Any]:
    # 이 API는 프론트가 과거/최근 캔들 화면을 처음 그릴 때 호출합니다.
    # Redis 최근 캔들이 부족하면 ClickHouse 과거 캔들을 같이 읽어 GOPS 형식으로 반환합니다.
    symbol = normalize_market_symbol(symbol)
    requested_ma = requested_ma_from_csv(ma)
    try:
        payload = get_market_data_provider().candle_snapshot(symbol, interval, limit)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Market data provider failed: {exc}") from exc

    payload["indicators"] = {"ma": requested_ma, "volume": True}
    payload["isSynthetic"] = False
    return payload


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
