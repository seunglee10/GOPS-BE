import asyncio
import json
import os
import time

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from app.services.alfaka_market_data import get_market_data_provider, normalize_market_symbol

router = APIRouter()


@router.websocket("/ws/charts")
async def chart_stream(
    websocket: WebSocket,
    symbol: str = Query(default="AAPL", min_length=1, max_length=12),
    interval: str = Query(default="1m", pattern="^(1m|5m|10m)$"),
) -> None:
    try:
        symbol = normalize_market_symbol(symbol)
    except HTTPException as exc:
        await websocket.accept()
        await websocket.send_json({"type": "ERROR", "detail": exc.detail})
        await websocket.close(code=1008)
        return

    # 이 WebSocket은 프론트 차트에 실시간 캔들 이벤트를 밀어주는 통로입니다.
    # Redis Pub/Sub market.events:{symbol}을 우선 보고, 없으면 live candle key를 확인합니다.
    provider = get_market_data_provider()
    redis_client = provider.redis_provider.redis
    pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(f"market.events:{symbol}", "market.events")
    last_sent_marker: str | None = None
    active_refresh_at = 0.0

    await websocket.accept()
    try:
        while True:
            now = time.monotonic()
            if now >= active_refresh_at:
                _mark_active_chart_symbol(redis_client, symbol)
                active_refresh_at = now + 5.0

            message = await asyncio.to_thread(pubsub.get_message, timeout=1.0)
            event = _parse_pubsub_event(message)
            if event and event.get("symbol") == symbol and event.get("interval") == interval:
                await websocket.send_json(event)
                last_sent_marker = _event_marker(event)
                continue

            live_event = provider.redis_provider.live_event(symbol)
            if live_event and live_event.get("interval") == interval:
                # 진행 중인 1분봉은 timestamp가 같아도 close/volume/updatedAt이 계속 바뀝니다.
                # timestamp만 비교하면 같은 1분 안의 실시간 tick 반영이 멈춘 것처럼 보입니다.
                marker = _event_marker(live_event)
                if marker and marker != last_sent_marker:
                    await websocket.send_json(live_event)
                    last_sent_marker = marker
            await asyncio.sleep(0.25)
    except WebSocketDisconnect:
        return
    finally:
        pubsub.close()


def _parse_pubsub_event(message: dict | None) -> dict | None:
    if not message or message.get("type") != "message":
        return None
    data = message.get("data")
    if not isinstance(data, str):
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


def _event_marker(event: dict | None) -> str | None:
    if not event:
        return None
    data = event.get("data") or {}
    if not isinstance(data, dict):
        return None
    return "|".join(
        str(value)
        for value in (
            event.get("type"),
            event.get("symbol"),
            event.get("interval"),
            data.get("timestamp"),
            data.get("updatedAt") or data.get("createdAt"),
            data.get("close"),
            data.get("volume"),
        )
    )


def _mark_active_chart_symbol(redis_client, symbol: str) -> None:
    try:
        ttl_seconds = int(os.getenv("ACTIVE_CHART_TTL_SECONDS", "45"))
    except ValueError:
        ttl_seconds = 45
    redis_client.sadd("active:charts:symbols", symbol)
    redis_client.setex(f"active:charts:{symbol}", ttl_seconds, "1")
