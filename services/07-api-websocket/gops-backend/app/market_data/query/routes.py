from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.market_data.query.service import get_query_service

router = APIRouter()


@router.get("/api/market/symbols/search")
def market_symbol_search(
    q: str = Query(default="", max_length=64),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    return get_query_service().symbol_search(q, limit)


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
    symbol: str = Query(default="AAPL", min_length=1, max_length=12),
    from_time: str = Query(alias="from"),
    to_time: str = Query(alias="to"),
    price_bin_size: str = Query(default="auto", alias="priceBinSize"),
) -> dict[str, Any]:
    return get_query_service().volume_profile_bins(symbol, from_time, to_time, price_bin_size)


@router.get("/api/market/status")
def market_status() -> dict[str, Any]:
    return get_query_service().latest_status()


@router.get("/api/market/status/{symbol}")
def market_symbol_status(symbol: str) -> dict[str, Any]:
    return get_query_service().latest_status(symbol)


@router.get("/api/agent/context/chart")
def agent_chart_context(
    symbol: str = Query(default="AAPL", min_length=1, max_length=12),
    interval: str = Query(default="1m", pattern="^(1m|5m|10m|1d)$"),
    from_time: str = Query(alias="from"),
    to_time: str = Query(alias="to"),
    include: str = Query(default="volumeProfile,status,daily"),
) -> dict[str, Any]:
    return get_query_service().agent_chart_context(symbol, interval, from_time, to_time, include)
