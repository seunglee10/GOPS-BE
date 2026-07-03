from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from app.auth.dependencies import WebSocketAuthRequired, WebSocketAuthUnavailable, require_websocket_user
from app.market_data.realtime.session_manager import WebSocketSessionManager
from app.services.alfaka_market_data import normalize_market_symbol
from alfaka.serving.intervals import normalize_chart_interval

router = APIRouter()
CHART_INTERVAL_PATTERN = "^(1m|5m|10m|1D|1W|1M|1d|1w|1mo|1MO|1month)$"


@router.websocket("/ws/charts")
async def chart_stream(
    websocket: WebSocket,
    symbol: str = Query(min_length=1, max_length=12),
    interval: str = Query(default="1m", pattern=CHART_INTERVAL_PATTERN),
    cursor: str | None = Query(default=None),
) -> None:
    try:
        user = require_websocket_user(websocket)
    except WebSocketAuthRequired as exc:
        await websocket.accept()
        await websocket.send_json({"type": "ERROR", "detail": str(exc)})
        await websocket.close(code=1008)
        return
    except WebSocketAuthUnavailable as exc:
        await websocket.accept()
        await websocket.send_json({"type": "ERROR", "detail": str(exc), "retryable": True})
        await websocket.close(code=1013)
        return

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
        await WebSocketSessionManager().serve_chart(websocket, symbol, interval, cursor, user_id=user.sub)
    except WebSocketDisconnect:
        return
