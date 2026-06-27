from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from alfaka.common.redis_keys import RedisKeyBuilder


@dataclass(eq=False)
class StreamSession:
    symbol: str
    interval: str
    queue_size: int = 128
    queue: asyncio.Queue = field(init=False)

    def __post_init__(self) -> None:
        self.queue = asyncio.Queue(maxsize=self.queue_size)

    async def enqueue(self, event: dict[str, Any]) -> None:
        if not self.queue.full():
            await self.queue.put(event)
            return
        if event.get("type") == "LIVE_CANDLE_UPDATE":
            await self._drop_one_droppable_update()
            if not self.queue.full():
                await self.queue.put(event)
            return
        await self._drop_one_droppable_update()
        if not self.queue.full():
            await self.queue.put(event)
            return
        await self._replace_oldest_with_slow_client_error()

    async def _drop_one_droppable_update(self) -> None:
        retained = []
        dropped = False
        while not self.queue.empty():
            item = self.queue.get_nowait()
            if not dropped and item.get("type") in {"LIVE_CANDLE_UPDATE", "VOLUME_PROFILE_BINS_UPDATE", "HEARTBEAT"}:
                dropped = True
                continue
            retained.append(item)
        for item in retained:
            await self.queue.put(item)

    async def _replace_oldest_with_slow_client_error(self) -> None:
        retained = []
        if not self.queue.empty():
            self.queue.get_nowait()
        while not self.queue.empty():
            retained.append(self.queue.get_nowait())
        for item in retained:
            await self.queue.put(item)
        await self.queue.put({
            "type": "ERROR",
            "detail": "Realtime client queue overflow; reconnect with the latest cursor.",
            "retryable": True,
        })


class SymbolStreamHub:
    def __init__(self, redis_client, provider):
        self.redis = redis_client
        self.provider = provider
        self.keys = RedisKeyBuilder()
        self.sessions_by_symbol: dict[str, set[StreamSession]] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.last_markers: dict[tuple[str, str], str] = {}

    async def subscribe(self, session: StreamSession) -> None:
        sessions = self.sessions_by_symbol.setdefault(session.symbol, set())
        sessions.add(session)
        if session.symbol not in self.tasks:
            self.tasks[session.symbol] = asyncio.create_task(self._listen_symbol(session.symbol))

    async def unsubscribe(self, session: StreamSession) -> None:
        sessions = self.sessions_by_symbol.get(session.symbol)
        if not sessions:
            return
        sessions.discard(session)
        if sessions:
            return
        task = self.tasks.pop(session.symbol, None)
        if task:
            task.cancel()
        self.sessions_by_symbol.pop(session.symbol, None)

    async def _listen_symbol(self, symbol: str) -> None:
        pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(self.keys.market_events_symbol(symbol), self.keys.market_events())
        try:
            while True:
                message = await asyncio.to_thread(pubsub.get_message, timeout=1.0)
                event = parse_pubsub_event(message)
                if event:
                    await self._broadcast(symbol, event)
                    continue
                await self._broadcast_live_fallback(symbol)
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            return
        finally:
            pubsub.close()

    async def _broadcast_live_fallback(self, symbol: str) -> None:
        live_event = self.provider.redis_provider.live_event(symbol)
        if not live_event:
            return
        marker = event_marker(live_event)
        marker_key = (symbol, live_event.get("interval", "1m"))
        if marker and marker == self.last_markers.get(marker_key):
            return
        self.last_markers[marker_key] = marker
        await self._broadcast(symbol, live_event)

    async def _broadcast(self, symbol: str, event: dict[str, Any]) -> None:
        marker = event_marker(event)
        marker_key = (symbol, event.get("type", ""), event.get("symbol", ""), event.get("interval", ""))
        if marker:
            if marker == self.last_markers.get(marker_key):
                return
            self.last_markers[marker_key] = marker
        sessions = list(self.sessions_by_symbol.get(symbol, set()))
        for session in sessions:
            if should_deliver_to_session(event, session):
                await session.enqueue(event)


def should_deliver_to_session(event: dict[str, Any], session: StreamSession) -> bool:
    if event.get("type") == "MARKET_STATUS_UPDATE":
        return event.get("symbol") in {session.symbol, "_MARKET"}
    return event.get("symbol") == session.symbol and event.get("interval") == session.interval


def parse_pubsub_event(message: dict | None) -> dict | None:
    if not message or message.get("type") != "message":
        return None
    data = message.get("data")
    if not isinstance(data, str):
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


def event_marker(event: dict | None) -> str | None:
    if not event:
        return None
    data = event.get("data") or {}
    if not isinstance(data, dict):
        return None
    return "|".join(
        str(value)
        for value in (
            event.get("eventId"),
            event.get("cursor"),
            event.get("type"),
            event.get("symbol"),
            event.get("interval"),
            data.get("timestamp"),
            data.get("updatedAt") or data.get("createdAt"),
            data.get("close"),
            data.get("volume"),
        )
    )


_hub: SymbolStreamHub | None = None


def get_stream_hub(redis_client, provider) -> SymbolStreamHub:
    global _hub
    if _hub is None:
        _hub = SymbolStreamHub(redis_client, provider)
    return _hub
