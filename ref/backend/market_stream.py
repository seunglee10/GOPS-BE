from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from backend.dummy_market import DummyMarketData, utc_now_iso


async def market_websocket(websocket: WebSocket, market: DummyMarketData) -> None:
    await websocket.accept()
    sequence = 1
    symbols: list[str] = []
    timeframe = "1m"
    subscribed = False
    snapshot_limit = 300
    request_id = ""
    next_event_at = asyncio.get_running_loop().time() + 1.0

    try:
        while True:
            timeout = 0.1 if subscribed else None
            try:
                message = await asyncio.wait_for(websocket.receive_json(), timeout=timeout)
                if not isinstance(message, dict):
                    await _send_error(websocket, None, "invalid_subscribe_message", "Message must be an object.")
                    continue
                message_type = message.get("type")
                if message_type == "subscribe":
                    request_id = str(message.get("requestId") or "")
                    raw_symbols = message.get("symbols") or ["AAPL"]
                    raw_timeframe = str(message.get("timeframe") or "1m")
                    snapshot_limit = int(message.get("snapshotLimit") or 300)
                    try:
                        symbols = market.validate_symbols(list(raw_symbols))
                        timeframe = market.validate_timeframe(raw_timeframe)
                    except ValueError as error:
                        code = "unsupported_timeframe" if "timeframe" in str(error).lower() else "unsupported_symbol"
                        await _send_error(websocket, request_id, code, str(error))
                        continue
                    subscribed = True
                    await websocket.send_json(
                        {
                            "type": "subscription",
                            "requestId": request_id,
                            "provider": "dummy",
                            "symbols": symbols,
                            "timeframe": timeframe,
                            "subscribedAt": utc_now_iso(),
                        }
                    )
                    snapshot = market.snapshot(symbols, timeframe, snapshot_limit)
                    await websocket.send_json(
                        {
                            "type": "snapshot",
                            "requestId": request_id,
                            "provider": "dummy",
                            "timeframe": snapshot.timeframe,
                            "candlesBySymbol": {
                                symbol: [candle.model_dump() for candle in candles]
                                for symbol, candles in snapshot.candlesBySymbol.items()
                            },
                            "generatedAt": snapshot.generatedAt,
                        }
                    )
                    next_event_at = asyncio.get_running_loop().time() + 1.0
                elif message_type == "unsubscribe":
                    unsub_symbols = set(message.get("symbols") or [])
                    symbols = [symbol for symbol in symbols if symbol not in unsub_symbols]
                    subscribed = bool(symbols)
                else:
                    await _send_error(websocket, message.get("requestId"), "invalid_subscribe_message", "Unsupported message type.")
            except TimeoutError:
                pass

            now = asyncio.get_running_loop().time()
            if subscribed and now >= next_event_at:
                events = market.next_event_batch(symbols, timeframe)
                await websocket.send_json(
                    {
                        "type": "events",
                        "provider": "dummy",
                        "sequence": sequence,
                        "events": events,
                        "sentAt": utc_now_iso(),
                    }
                )
                sequence += 1
                next_event_at = now + 1.0
    except WebSocketDisconnect:
        return


async def _send_error(websocket: WebSocket, request_id: str | None, code: str, message: str) -> None:
    payload: dict[str, Any] = {
        "type": "error",
        "requestId": request_id,
        "code": code,
        "message": message,
        "sentAt": utc_now_iso(),
    }
    await websocket.send_json(payload)
