import asyncio

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from backend.app.services.market_data import build_live_candle, normalize_dummy_symbol

router = APIRouter()


@router.websocket("/ws/charts")
async def chart_stream(
    websocket: WebSocket,
    symbol: str = Query(default="AAPL", min_length=1, max_length=12),
    interval: str = Query(default="1m", pattern="^(1m|5m|10m)$"),
) -> None:
    try:
        symbol = normalize_dummy_symbol(symbol)
    except HTTPException as exc:
        await websocket.accept()
        await websocket.send_json({"type": "ERROR", "detail": exc.detail})
        await websocket.close(code=1008)
        return

    await websocket.accept()
    try:
        candle_index = 159
        cycle = 0
        while True:
            for update_step in range(1, 5):
                await websocket.send_json({
                    "type": "LIVE_CANDLE_UPDATE",
                    "symbol": symbol,
                    "interval": interval,
                    "source": "dummy",
                    "feed": "synthetic-demo",
                    "isSynthetic": True,
                    "data": build_live_candle(symbol, interval, candle_index, "LIVE_CANDLE_UPDATE", update_step),
                })
                await asyncio.sleep(0.45)

            await websocket.send_json({
                "type": "CANDLE_CLOSED",
                "symbol": symbol,
                "interval": interval,
                "source": "dummy",
                "feed": "synthetic-demo",
                "isSynthetic": True,
                "data": build_live_candle(symbol, interval, candle_index, "CANDLE_CLOSED", 5),
            })
            await asyncio.sleep(0.45)

            if cycle % 6 == 4:
                await websocket.send_json({
                    "type": "CANDLE_CORRECTED",
                    "symbol": symbol,
                    "interval": interval,
                    "source": "dummy",
                    "feed": "synthetic-demo",
                    "isSynthetic": True,
                    "data": build_live_candle(symbol, interval, candle_index, "CANDLE_CORRECTED", cycle),
                })

            candle_index += 1
            cycle += 1
    except WebSocketDisconnect:
        return
