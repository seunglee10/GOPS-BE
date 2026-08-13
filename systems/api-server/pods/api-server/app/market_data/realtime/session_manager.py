from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid

from fastapi import WebSocket, WebSocketDisconnect

from app.market_data.realtime.active_symbols import ActiveSymbolManager
from app.market_data.realtime.stream_hub import StreamSession, get_stream_hub
from app.services.alfaka_market_data import get_market_data_provider

logger = logging.getLogger(__name__)


class WebSocketSessionManager:
    def __init__(self, provider=None):
        self.provider = provider or get_market_data_provider()
        self.redis = self.provider.redis_provider.redis
        self.active_symbols = ActiveSymbolManager(self.redis)
        self.hub = get_stream_hub(self.redis, self.provider)
        self.queue_size = parse_int(os.getenv("REALTIME_CLIENT_QUEUE_SIZE"), 128)
        self.heartbeat_seconds = parse_int(os.getenv("REALTIME_HEARTBEAT_SECONDS"), 15)

    async def serve_chart(self, websocket: WebSocket, symbol: str, interval: str, cursor: str | None = None, user_id: str = "anonymous") -> None:
        session_id = uuid.uuid4().hex
        session = StreamSession(symbol=symbol, interval=interval, queue_size=self.queue_size)
        await websocket.accept()
        await self.hub.subscribe(session)
        last_heartbeat = 0.0
        try:
            await self._send_gap_fill(websocket, symbol, interval, cursor)
            while True:
                self._refresh_active_symbol(user_id, session_id, symbol)
                try:
                    event = await asyncio.wait_for(session.queue.get(), timeout=1.0)
                    await websocket.send_json(event)
                    if event.get("type") == "ERROR" and event.get("retryable"):
                        await websocket.close(code=1013)
                        return
                except asyncio.TimeoutError:
                    now = time.monotonic()
                    if now - last_heartbeat >= self.heartbeat_seconds:
                        await websocket.send_json({"type": "HEARTBEAT", "symbol": symbol, "interval": interval})
                        last_heartbeat = now
        except WebSocketDisconnect:
            return
        finally:
            close = getattr(self.active_symbols, "close", None)
            if callable(close):
                close(user_id, session_id)
            await self.hub.unsubscribe(session)

    def _refresh_active_symbol(self, user_id: str, session_id: str, symbol: str) -> None:
        refresh = getattr(self.active_symbols, "refresh", None)
        if not callable(refresh):
            return
        try:
            refresh(user_id, session_id, symbol)
        except TypeError:
            try:
                refresh(symbol)
            except Exception as exc:
                logger.warning("Realtime active symbol legacy refresh skipped: symbol=%s error=%s", symbol, exc)
        except Exception as exc:
            logger.warning("Realtime active symbol refresh skipped: symbol=%s error=%s", symbol, exc)

    async def _send_gap_fill(self, websocket: WebSocket, symbol: str, interval: str, cursor: str | None) -> None:
        if not cursor:
            return
        candles = self.provider.candles_since_cursor(symbol, interval, cursor)
        from market_data.serving.dto import websocket_event
        for candle in candles:
            if not bool(candle.get("isClosed", candle.get("is_closed", True))):
                event_type = "LIVE_CANDLE_UPDATE"
            else:
                event_type = "CANDLE_CORRECTED" if candle.get("correctionType") == "UPDATED" else "CANDLE_CLOSED"
            await websocket.send_json(websocket_event(event_type, symbol, interval, candle, feed=candle.get("feed") or "unknown"))


def parse_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
