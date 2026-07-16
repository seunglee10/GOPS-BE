import asyncio
import json
import time

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from app.auth.dependencies import WebSocketAuthUnavailable, optional_websocket_user
from app.market_data.realtime.session_manager import WebSocketSessionManager
from app.routes.simulator import simulator_gateway_from_app, simulator_mode_active
from app.services.alfaka_market_data import normalize_market_symbol
from alfaka.serving.dto import websocket_event
from alfaka.serving.intervals import normalize_chart_interval

router = APIRouter()
CHART_INTERVAL_PATTERN = "^(1m|5m|10m|1h|4h|1D|1W|1M|1d|1w|1mo|1MO|1month)$"


@router.websocket("/ws/charts")
async def chart_stream(
    websocket: WebSocket,
    symbol: str = Query(min_length=1, max_length=12),
    interval: str = Query(default="1m", pattern=CHART_INTERVAL_PATTERN),
    cursor: str | None = Query(default=None),
) -> None:
    try:
        user = optional_websocket_user(websocket)
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
        if await asyncio.to_thread(simulator_mode_active, websocket.app):
            await _serve_simulation_chart(websocket, symbol, interval, cursor)
            return
        await WebSocketSessionManager().serve_chart(
            websocket,
            symbol,
            interval,
            cursor,
            user_id=user.sub if user else "anonymous",
        )
    except WebSocketDisconnect:
        return


async def _serve_simulation_chart(
    websocket: WebSocket,
    symbol: str,
    interval: str,
    cursor: str | None,
) -> None:
    await websocket.accept()
    gateway = simulator_gateway_from_app(websocket.app)
    fingerprints_by_timestamp: dict[str, str] = {}
    last_heartbeat = 0.0
    while True:
        if not await asyncio.to_thread(simulator_mode_active, websocket.app):
            await websocket.close(code=1012)
            return
        payload = await asyncio.to_thread(gateway.candles, symbol, interval, 500)
        candles = payload.get("candles") if isinstance(payload, dict) else []
        for candle in candles if isinstance(candles, list) else []:
            if not isinstance(candle, dict):
                continue
            timestamp = str(candle.get("timestamp") or "")
            if cursor and timestamp and timestamp < cursor:
                continue
            fingerprint = json.dumps(candle, ensure_ascii=False, sort_keys=True, default=str)
            if fingerprints_by_timestamp.get(timestamp) == fingerprint:
                continue
            event_type = "CANDLE_CLOSED" if candle.get("isClosed") else "LIVE_CANDLE_UPDATE"
            await websocket.send_json(
                websocket_event(
                    event_type,
                    symbol,
                    interval,
                    candle,
                    source="simulation_replay",
                    feed=str(candle.get("feed") or "mixed"),
                )
            )
            fingerprints_by_timestamp[timestamp] = fingerprint
            cursor = timestamp or cursor
        if len(fingerprints_by_timestamp) > 1_000:
            for expired in sorted(fingerprints_by_timestamp)[:-500]:
                fingerprints_by_timestamp.pop(expired, None)
        now = time.monotonic()
        if now - last_heartbeat >= 5:
            await websocket.send_json({"type": "HEARTBEAT", "symbol": symbol, "interval": interval, "simulation": True})
            last_heartbeat = now
        await asyncio.sleep(0.25)
