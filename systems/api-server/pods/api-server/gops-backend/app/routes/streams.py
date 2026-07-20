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
    order_flow: bool = Query(default=False, alias="orderFlow"),
    candles: bool = Query(default=True),
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
            await _serve_simulation_chart(
                websocket,
                symbol,
                interval,
                cursor,
                order_flow=order_flow,
                candles=candles,
            )
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
    *,
    order_flow: bool = False,
    candles: bool = True,
) -> None:
    await websocket.accept()
    gateway = simulator_gateway_from_app(websocket.app)
    fingerprints_by_timestamp: dict[str, str] = {}
    order_flow_fingerprints: dict[str, str] = {}
    order_flow_run_id: str | None = None
    order_flow_session_date: str | None = None
    order_flow_sequence: int | None = None
    quote_fingerprint: str | None = None
    last_heartbeat = 0.0
    while True:
        if not await asyncio.to_thread(simulator_mode_active, websocket.app):
            await websocket.close(code=1012)
            return
        if candles:
            payload = await asyncio.to_thread(gateway.candles, symbol, interval, 500)
            candle_rows = payload.get("candles") if isinstance(payload, dict) else []
            for candle in candle_rows if isinstance(candle_rows, list) else []:
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
        if order_flow:
            order_flow_payload = await asyncio.to_thread(
                gateway.order_flow,
                symbol,
                after_sequence=order_flow_sequence,
                latest_only=order_flow_sequence is None,
            )
            run_id = str(order_flow_payload.get("runId") or "") or None
            session_date = str(order_flow_payload.get("sessionDate") or "") or None
            reset_projection = run_id != order_flow_run_id or session_date != order_flow_session_date
            if reset_projection:
                order_flow_fingerprints.clear()
                quote_fingerprint = None
            order_flow_run_id = run_id
            order_flow_session_date = session_date
            try:
                order_flow_sequence = max(0, int(order_flow_payload.get("nextSequence") or 0))
            except (TypeError, ValueError):
                order_flow_sequence = None
            raw_minutes = order_flow_payload.get("minutes")
            minutes = [item for item in raw_minutes if isinstance(item, dict)] if isinstance(raw_minutes, list) else []
            if reset_projection and not minutes:
                order_flow_sequence = None
            initial_snapshot = not order_flow_fingerprints
            latest_minute = str(minutes[-1].get("eventMinute") or "") if minutes else ""
            for minute in minutes:
                event_minute = str(minute.get("eventMinute") or "")
                if not event_minute:
                    continue
                minute_fingerprint = json.dumps(minute, ensure_ascii=False, sort_keys=True, default=str)
                if order_flow_fingerprints.get(event_minute) == minute_fingerprint:
                    continue
                order_flow_fingerprints[event_minute] = minute_fingerprint
                if initial_snapshot and event_minute != latest_minute:
                    continue
                await websocket.send_json({
                    "type": "ORDER_FLOW_BINS_UPDATE",
                    "symbol": symbol,
                    "interval": "1m",
                    "source": "simulation_replay",
                    "simulation": True,
                    "datasetId": order_flow_payload.get("datasetId"),
                    "runId": run_id,
                    "virtualTime": order_flow_payload.get("virtualTime"),
                    "data": {
                        "eventMinute": event_minute,
                        "sessionDate": session_date,
                        "priceBinSize": order_flow_payload.get("priceBinSize"),
                        "bins": minute.get("bins") if isinstance(minute.get("bins"), list) else [],
                        "updatedAt": minute.get("updatedAt") or order_flow_payload.get("virtualTime"),
                    },
                })
            live_quote = order_flow_payload.get("liveQuote")
            if isinstance(live_quote, dict):
                next_quote_fingerprint = json.dumps(live_quote, ensure_ascii=False, sort_keys=True, default=str)
                if next_quote_fingerprint != quote_fingerprint:
                    await websocket.send_json({
                        "type": "LIVE_QUOTE_UPDATE",
                        "symbol": symbol,
                        "interval": "quotes",
                        "source": "simulation_replay",
                        "simulation": True,
                        "datasetId": order_flow_payload.get("datasetId"),
                        "runId": run_id,
                        "virtualTime": order_flow_payload.get("virtualTime"),
                        "data": live_quote,
                    })
                    quote_fingerprint = next_quote_fingerprint
        now = time.monotonic()
        if now - last_heartbeat >= 5:
            await websocket.send_json({"type": "HEARTBEAT", "symbol": symbol, "interval": interval, "simulation": True})
            last_heartbeat = now
        await asyncio.sleep(1.0 if order_flow and not candles else 0.25)
