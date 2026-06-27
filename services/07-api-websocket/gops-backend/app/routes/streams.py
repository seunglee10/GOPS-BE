from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from app.market_data.realtime.session_manager import WebSocketSessionManager
from app.services.alfaka_market_data import normalize_market_symbol

router = APIRouter()


@router.websocket("/ws/charts")
async def chart_stream(
    websocket: WebSocket,
    symbol: str = Query(default="AAPL", min_length=1, max_length=12),
    interval: str = Query(default="1m", pattern="^(1m|5m|10m|1d)$"),
    cursor: str | None = Query(default=None),
) -> None:
    try:
        symbol = normalize_market_symbol(symbol)
    except HTTPException as exc:
        await websocket.accept()
        await websocket.send_json({"type": "ERROR", "detail": exc.detail})
        await websocket.close(code=1008)
        return

    try:
        await WebSocketSessionManager().serve_chart(websocket, symbol, interval, cursor)
    except WebSocketDisconnect:
        return
