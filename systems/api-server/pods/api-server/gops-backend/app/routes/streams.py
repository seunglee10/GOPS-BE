from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from app.market_data.realtime.session_manager import WebSocketSessionManager
from app.services.alfaka_market_data import normalize_market_symbol
from alfaka.serving.intervals import normalize_chart_interval

router = APIRouter()
CHART_INTERVAL_PATTERN = "^(1m|5m|10m|1D|1W|1M|1d|1w|1mo|1MO|1month)$"


@router.websocket("/ws/charts")
async def chart_stream(
    websocket: WebSocket,
    symbol: str = Query(default="AAPL", min_length=1, max_length=12),
    interval: str = Query(default="1m", pattern=CHART_INTERVAL_PATTERN),
    cursor: str | None = Query(default=None),
) -> None:
    try:
        symbol = normalize_market_symbol(symbol)
        interval = normalize_chart_interval(interval)
    except HTTPException as exc:
        await websocket.accept()
        await websocket.send_json({"type": "ERROR", "detail": exc.detail})
        await websocket.close(code=1008)
        return
    except ValueError as exc:
        await websocket.accept()
        await websocket.send_json({"type": "ERROR", "detail": str(exc)})
        await websocket.close(code=1008)
        return

    try:
        await WebSocketSessionManager().serve_chart(websocket, symbol, interval, cursor)
    except WebSocketDisconnect:
        return


@router.websocket("/ws/quotes")
async def quote_stream(
    websocket: WebSocket,
    symbols: str = Query(default="", max_length=2000),
    interval: str = Query(default="1m", pattern=CHART_INTERVAL_PATTERN),
    max_hz: float = Query(default=1.0, alias="maxHz", ge=0.2, le=4.0),
) -> None:
    try:
        interval = normalize_chart_interval(interval)
        normalized_symbols = unique_symbols(
            normalize_market_symbol(item)
            for item in symbols.split(",")
            if item.strip()
        )[:100]
        if not normalized_symbols:
            raise ValueError("At least one quote symbol is required.")
    except HTTPException as exc:
        await websocket.accept()
        await websocket.send_json({"type": "ERROR", "detail": exc.detail})
        await websocket.close(code=1008)
        return
    except ValueError as exc:
        await websocket.accept()
        await websocket.send_json({"type": "ERROR", "detail": str(exc)})
        await websocket.close(code=1008)
        return

    try:
        await WebSocketSessionManager().serve_quotes(websocket, normalized_symbols, interval, max_hz=max_hz)
    except WebSocketDisconnect:
        return


def unique_symbols(symbols) -> list[str]:
    seen = set()
    result = []
    for symbol in symbols:
        if symbol in seen:
            continue
        seen.add(symbol)
        result.append(symbol)
    return result
