#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import time
from urllib.parse import urlparse


GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure ORDER_FLOW_BINS_UPDATE WebSocket fanout load.")
    parser.add_argument("--url", default="ws://localhost:8000/ws/charts?symbol=NVDA&interval=1m")
    parser.add_argument("--clients", type=int, default=50)
    parser.add_argument("--seconds", type=float, default=60.0)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run_load(args.url, max(1, args.clients), max(1.0, args.seconds))), sort_keys=True))
    return 0


async def run_load(url: str, clients: int, seconds: float) -> dict[str, object]:
    started = time.monotonic()
    results = await asyncio.gather(*(client_task(url, seconds) for _ in range(clients)))
    elapsed = max(time.monotonic() - started, 0.001)
    total_events = sum(item["events"] for item in results)
    max_gap = max((item["maxInterEventGapSeconds"] for item in results), default=0.0)
    error_closes = sum(item["errorCloses"] for item in results)
    failed_clients = sum(1 for item in results if item.get("connectError"))
    return {
        "url": url,
        "clients": clients,
        "seconds": seconds,
        "elapsedSeconds": elapsed,
        "totalEvents": total_events,
        "eventsPerSecondPerClient": total_events / elapsed / clients,
        "maxInterEventGapSeconds": max_gap,
        "errorCloseCount": error_closes,
        "failedClientCount": failed_clients,
        "clientSamples": results[:5],
    }


async def client_task(url: str, seconds: float) -> dict[str, object]:
    deadline = time.monotonic() + seconds
    events = 0
    error_closes = 0
    last_event = None
    max_gap = 0.0
    try:
        reader, writer = await open_ws(url)
        while time.monotonic() < deadline:
            timeout = max(0.05, deadline - time.monotonic())
            try:
                opcode, payload = await asyncio.wait_for(read_frame(reader), timeout=timeout)
            except asyncio.TimeoutError:
                break
            if opcode == 8:
                if b"ERROR" in payload.upper():
                    error_closes += 1
                break
            if opcode != 1:
                continue
            now = time.monotonic()
            if last_event is not None:
                max_gap = max(max_gap, now - last_event)
            last_event = now
            events += 1
            try:
                if json.loads(payload.decode("utf-8")).get("type") == "ERROR":
                    error_closes += 1
            except Exception:
                pass
        writer.close()
        await writer.wait_closed()
        return {"events": events, "maxInterEventGapSeconds": max_gap, "errorCloses": error_closes}
    except Exception as exc:
        return {"events": events, "maxInterEventGapSeconds": max_gap, "errorCloses": error_closes, "connectError": str(exc)}


async def open_ws(url: str):
    parsed = urlparse(url)
    if parsed.scheme != "ws":
        raise ValueError("Only ws:// URLs are supported by the stdlib load tool.")
    host = parsed.hostname or "localhost"
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    reader, writer = await asyncio.open_connection(host, port)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    writer.write(request.encode("ascii"))
    await writer.drain()
    headers = await reader.readuntil(b"\r\n\r\n")
    if b" 101 " not in headers.split(b"\r\n", 1)[0]:
        raise RuntimeError(headers.decode("latin1", errors="replace").splitlines()[0])
    expected = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
    if expected.lower() not in headers.decode("latin1", errors="ignore").lower():
        raise RuntimeError("WebSocket accept key mismatch")
    return reader, writer


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    first = await reader.readexactly(2)
    opcode = first[0] & 0x0F
    length = first[1] & 0x7F
    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), "big")
    elif length == 127:
        length = int.from_bytes(await reader.readexactly(8), "big")
    payload = await reader.readexactly(length)
    return opcode, payload


if __name__ == "__main__":
    raise SystemExit(main())
