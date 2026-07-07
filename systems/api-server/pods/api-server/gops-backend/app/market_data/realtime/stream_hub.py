from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from alfaka.common.redis_keys import RedisKeyBuilder
from alfaka.serving.closed_watermark import candle_at_or_before_watermark, latest_watermark_value
from alfaka.serving.dto import websocket_event
from alfaka.serving.redis_provider import live_candle_is_fresh

logger = logging.getLogger(__name__)


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
        if event.get("type") in {"LIVE_CANDLE_UPDATE", "LIVE_TRADE_UPDATE", "LIVE_QUOTE_UPDATE"}:
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
            if not dropped and item.get("type") in {"LIVE_CANDLE_UPDATE", "LIVE_TRADE_UPDATE", "LIVE_QUOTE_UPDATE", "VOLUME_PROFILE_BINS_UPDATE", "HEARTBEAT"}:
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
        self.last_markers: dict[tuple[Any, ...], str] = {}
        self.closed_watermarks: dict[tuple[str, str], str] = {}
        self.poll_seconds = read_positive_float_env("REALTIME_REDIS_POLL_SECONDS", 0.25)
        self.error_log_interval_seconds = read_positive_float_env("REALTIME_REDIS_ERROR_LOG_INTERVAL_SECONDS", 30.0)
        self._last_redis_error_log = 0.0

    async def subscribe(self, session: StreamSession) -> None:
        sessions = self.sessions_by_symbol.setdefault(session.symbol, set())
        sessions.add(session)
        if not self.tasks:
            self.tasks["__global__"] = asyncio.create_task(self._listen_global())

    async def unsubscribe(self, session: StreamSession) -> None:
        sessions = self.sessions_by_symbol.get(session.symbol)
        if not sessions:
            return
        sessions.discard(session)
        if sessions:
            return
        self.sessions_by_symbol.pop(session.symbol, None)
        if self.sessions_by_symbol:
            return
        task = self.tasks.pop("__global__", None)
        if task:
            task.cancel()

    async def _listen_global(self) -> None:
        if self.redis is None:
            await self._idle_until_cancelled()
            return
        while True:
            pubsub = None
            try:
                pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(self.keys.market_events())
                while True:
                    message = await asyncio.to_thread(pubsub.get_message, timeout=max(0.1, self.poll_seconds))
                    event = parse_pubsub_event(message)
                    if event:
                        await self._broadcast_event(event)
                    await self._broadcast_latest_redis_live_events()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self._log_redis_error("global_listener", exc)
                await asyncio.sleep(max(0.1, min(self.poll_seconds, 1.0)))
            finally:
                if pubsub is not None:
                    try:
                        pubsub.close()
                    except Exception:
                        pass

    async def _idle_until_cancelled(self) -> None:
        try:
            while True:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            return

    async def _broadcast_latest_redis_live_event(self, symbol: str) -> None:
        if self.redis is not None:
            await self._broadcast_latest_redis_live_events({symbol})
            return
        intervals = sorted({session.interval for session in self.sessions_by_symbol.get(symbol, set())})
        for interval in intervals:
            live_event = self.provider.redis_provider.live_event(symbol, interval)
            if not live_event:
                continue
            marker = event_marker(live_event)
            marker_key = (symbol, live_event.get("interval", interval))
            if marker and marker == self.last_markers.get(marker_key):
                continue
            self.last_markers[marker_key] = marker
            await self._broadcast(symbol, live_event)
        for event in self._live_trade_quote_events(symbol):
            marker = event_marker(event)
            marker_key = (symbol, event.get("type", ""))
            if marker and marker == self.last_markers.get(marker_key):
                continue
            self.last_markers[marker_key] = marker
            await self._broadcast(symbol, event)

    async def _broadcast_latest_redis_live_events(self, symbols: set[str] | None = None) -> None:
        active = {
            symbol: sorted({session.interval for session in sessions})
            for symbol, sessions in self.sessions_by_symbol.items()
            if sessions and (symbols is None or symbol in symbols)
        }
        if not active or self.redis is None:
            return
        try:
            events = await asyncio.to_thread(self._read_latest_live_events_batch, active)
        except Exception as exc:
            self._log_redis_error("live_batch_read", exc)
            return
        for event in events:
            try:
                await self._broadcast_event(event)
            except Exception as exc:
                self._log_redis_error("live_event_broadcast", exc)

    def _read_latest_live_events_batch(self, active: dict[str, list[str]]) -> list[dict[str, Any]]:
        pipeline = self.redis.pipeline()
        operations: list[tuple[str, str, str | None]] = []
        for symbol, intervals in active.items():
            for interval in intervals:
                pipeline.get(self.keys.live_candle(symbol, interval))
                pipeline.get(self.keys.closed_candle_watermark(symbol, interval))
                operations.append(("candle", symbol, interval))
            pipeline.hgetall(self.keys.live_trade(symbol))
            operations.append(("trade", symbol, None))
            pipeline.get(self.keys.live_quote(symbol))
            operations.append(("quote", symbol, None))
        try:
            values = pipeline.execute()
        except Exception as exc:
            self._log_redis_error("live_pipeline_execute", exc)
            return []
        events: list[dict[str, Any]] = []
        value_index = 0
        for operation in operations:
            kind, symbol, interval = operation
            if kind == "candle":
                value = values[value_index] if value_index < len(values) else None
                watermark = values[value_index + 1] if value_index + 1 < len(values) else None
                value_index += 2
            else:
                value = values[value_index] if value_index < len(values) else None
                watermark = None
                value_index += 1
            event = self._event_from_live_value(kind, symbol, interval, value, watermark)
            if event:
                events.append(event)
        return events

    def _event_from_live_value(self, kind: str, symbol: str, interval: str | None, value: Any, watermark: Any = None) -> dict[str, Any] | None:
        if kind == "candle":
            candle = parse_json_value(value)
            if not candle or not live_candle_is_fresh(candle):
                return None
            if candle_at_or_before_watermark(candle, watermark or self.closed_watermarks.get((symbol, interval or candle.get("interval") or "1m"))):
                return None
            return websocket_event("LIVE_CANDLE_UPDATE", symbol, interval or candle.get("interval") or "1m", candle)
        if kind == "trade" and isinstance(value, dict) and value:
            return {"type": "LIVE_TRADE_UPDATE", "symbol": symbol, "interval": "trades", "data": value}
        if kind == "quote":
            quote = parse_json_value(value)
            if quote:
                return {"type": "LIVE_QUOTE_UPDATE", "symbol": symbol, "interval": "quotes", "data": quote}
        return None

    def _live_trade_quote_events(self, symbol: str) -> list[dict[str, Any]]:
        events = []
        live_trade = getattr(self.provider.redis_provider, "live_trade", None)
        live_quote = getattr(self.provider.redis_provider, "live_quote", None)
        trade = live_trade(symbol) if callable(live_trade) else None
        if trade:
            events.append({"type": "LIVE_TRADE_UPDATE", "symbol": symbol, "interval": "trades", "data": trade})
        quote = live_quote(symbol) if callable(live_quote) else None
        if quote:
            events.append({"type": "LIVE_QUOTE_UPDATE", "symbol": symbol, "interval": "quotes", "data": quote})
        return events

    def _log_redis_error(self, operation: str, exc: Exception) -> None:
        now = time.monotonic()
        if now - self._last_redis_error_log < self.error_log_interval_seconds:
            return
        self._last_redis_error_log = now
        logger.warning("Realtime Redis operation skipped: operation=%s error=%s", operation, exc)

    async def _broadcast_event(self, event: dict[str, Any]) -> None:
        event = self._event_after_closed_watermark(event)
        if not event:
            return
        symbol = event.get("symbol")
        if symbol == "_MARKET":
            for subscribed_symbol in list(self.sessions_by_symbol):
                await self._broadcast(subscribed_symbol, event)
            return
        if isinstance(symbol, str):
            await self._broadcast(symbol, event)

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

    def _event_after_closed_watermark(self, event: dict[str, Any] | None) -> dict[str, Any] | None:
        if not event:
            return None
        event_type = event.get("type")
        symbol = event.get("symbol")
        interval = event.get("interval")
        data = event.get("data") or {}
        if not isinstance(symbol, str) or not isinstance(interval, str) or not isinstance(data, dict):
            return event
        marker_key = (symbol, interval)
        if event_type in {"CANDLE_CLOSED", "CANDLE_CORRECTED"}:
            watermark = latest_watermark_value(self.closed_watermarks.get(marker_key), data.get("timestamp"))
            if watermark:
                self.closed_watermarks[marker_key] = watermark
            return event
        if event_type == "LIVE_CANDLE_UPDATE" and candle_at_or_before_watermark(data, self.closed_watermarks.get(marker_key)):
            return None
        return event


def should_deliver_to_session(event: dict[str, Any], session: StreamSession) -> bool:
    if event.get("type") == "MARKET_STATUS_UPDATE":
        return event.get("symbol") in {session.symbol, "_MARKET"}
    if event.get("type") in {"LIVE_TRADE_UPDATE", "LIVE_QUOTE_UPDATE"}:
        return event.get("symbol") == session.symbol
    return event.get("symbol") == session.symbol and event.get("interval") == session.interval


def parse_pubsub_event(message: dict | None) -> dict | None:
    if not message or message.get("type") != "message":
        return None
    data = message.get("data")
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    if not isinstance(data, str):
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


def parse_json_value(value: Any) -> dict[str, Any] | None:
    if not value:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


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
            data.get("price"),
            data.get("size"),
            data.get("bidPrice"),
            data.get("askPrice"),
            data.get("close"),
            data.get("volume"),
        )
    )


def read_positive_float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


_hub: SymbolStreamHub | None = None


def get_stream_hub(redis_client, provider) -> SymbolStreamHub:
    global _hub
    if _hub is None:
        _hub = SymbolStreamHub(redis_client, provider)
    return _hub
