from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.stocks.service import DEFAULT_MOVERS_LIMIT, MAX_MOVERS_LIMIT, StocksService, SymbolNotFound, get_stocks_service

router = APIRouter(prefix="/api/stocks", tags=["stocks"])


def stocks_service_from_app(request: Request) -> StocksService:
    return getattr(request.app.state, "stocks_service", None) or get_stocks_service()


@router.get("/movers")
def stock_movers(
    request: Request,
    sort: str = Query(default="trade", pattern="^(trade|volume|up|down)$"),
    limit: int = Query(default=DEFAULT_MOVERS_LIMIT, ge=1, le=MAX_MOVERS_LIMIT),
) -> dict[str, Any]:
    try:
        return stocks_service_from_app(request).movers(sort=sort, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "unsupported_sort", "message": str(exc)}) from exc


@router.get("/{symbol}/summary")
def stock_summary(request: Request, symbol: str) -> dict[str, Any]:
    try:
        return stocks_service_from_app(request).summary(symbol)
    except SymbolNotFound as exc:
        raise HTTPException(status_code=404, detail={"code": "symbol_not_found", "symbol": symbol.upper()}) from exc
