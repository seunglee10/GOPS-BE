#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse


repo_root = Path(__file__).resolve().parents[2]
shared_path = repo_root / "systems" / "market-data" / "shared"
if str(shared_path) not in sys.path:
    sys.path.insert(0, str(shared_path))

from alfaka.common.redis_keys import RedisKeyBuilder  # noqa: E402
from alfaka.orderflow.redis_model import order_flow_blob_to_bins, parse_order_flow_minute_blob  # noqa: E402
from alfaka.storage.clickhouse_loader import ClickHouseHttpClient  # noqa: E402


SIDES = ("ask", "bid", "unknown")


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare Redis live order-flow session totals with EOD ClickHouse rows.")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--date", required=True, help="Session date in YYYY-MM-DD.")
    parser.add_argument("--redis-url", default=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    args = parser.parse_args()
    symbol = args.symbol.strip().upper()
    try:
        live_bins = redis_order_flow_bins(args.redis_url, RedisKeyBuilder(), symbol)
        live_totals = side_totals(row for row in live_bins if row.get("sessionDate") == args.date)
        eod_totals = eod_side_totals(symbol, args.date)
        payload = diff_payload(symbol, args.date, live_totals, eod_totals)
        for side, item in payload["diffs"].items():
            if item["relativeDiff"] > 0.10:
                print(
                    f"WARN {side} relativeDiff={item['relativeDiff']:.4f} exceeds 0.10; revisit ORDER_FLOW_QUOTE_REFRESH_MS.",
                    file=sys.stderr,
                )
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    except Exception as exc:
        print(f"ERROR order-flow live-vs-eod verification failed: {exc}", file=sys.stderr)
        print(json.dumps({"symbol": symbol, "sessionDate": args.date, "error": str(exc)}, sort_keys=True))
    return 0


def redis_order_flow_bins(redis_url: str, keys: RedisKeyBuilder, symbol: str) -> list[dict[str, object]]:
    parsed = urlparse(redis_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 6379
    password = parsed.password
    db = parsed.path.strip("/") or "0"
    with socket.create_connection((host, port), timeout=5) as sock:
        if password:
            redis_command(sock, "AUTH", password)
        if db:
            redis_command(sock, "SELECT", db)
        closed = redis_command(sock, "ZRANGEBYSCORE", keys.order_flow_minutes(symbol), "-inf", "+inf")
        live = redis_command(sock, "GET", keys.order_flow_live_minute(symbol))
    blobs_by_minute = {}
    for value in [*(closed if isinstance(closed, list) else []), live]:
        blob = parse_order_flow_minute_blob(value)
        if blob and blob.get("eventMinute"):
            blobs_by_minute[str(blob["eventMinute"])] = blob
    bins = []
    for minute in sorted(blobs_by_minute):
        bins.extend(order_flow_blob_to_bins(blobs_by_minute[minute]))
    return bins


def redis_command(sock: socket.socket, *parts: str) -> object:
    encoded = [str(part).encode("utf-8") for part in parts]
    request = b"*" + str(len(encoded)).encode() + b"\r\n"
    for part in encoded:
        request += b"$" + str(len(part)).encode() + b"\r\n" + part + b"\r\n"
    sock.sendall(request)
    return read_resp(sock)


def read_resp(sock: socket.socket) -> object:
    prefix = sock.recv(1)
    if prefix == b"+":
        return read_line(sock)
    if prefix == b"-":
        raise RuntimeError(read_line(sock))
    if prefix == b":":
        return int(read_line(sock))
    if prefix == b"$":
        length = int(read_line(sock))
        if length < 0:
            return None
        data = read_exact(sock, length)
        read_exact(sock, 2)
        return data.decode("utf-8")
    if prefix == b"*":
        length = int(read_line(sock))
        return [read_resp(sock) for _ in range(max(0, length))]
    raise RuntimeError(f"unexpected redis response prefix {prefix!r}")


def read_line(sock: socket.socket) -> str:
    data = bytearray()
    while not data.endswith(b"\r\n"):
        data.extend(sock.recv(1))
    return data[:-2].decode("utf-8")


def read_exact(sock: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise RuntimeError("redis connection closed")
        data.extend(chunk)
    return bytes(data)


def eod_side_totals(symbol: str, session_date: str) -> dict[str, float]:
    client = ClickHouseHttpClient(
        url=os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
        database=os.getenv("CLICKHOUSE_DATABASE", "market_data"),
        user=os.getenv("CLICKHOUSE_USER", "alfaka"),
        password=os.getenv("CLICKHOUSE_PASSWORD", "alfaka"),
    )
    rows = client.query_json_each_row(
        """
        SELECT ask_volume, bid_volume, unknown_volume
        FROM order_flow_profile_daily
        WHERE symbol = {symbol:String} AND session_date = {session_date:Date}
        """,
        {"symbol": symbol, "session_date": session_date},
    )
    return side_totals(rows)


def side_totals(rows) -> dict[str, float]:
    totals = {side: 0.0 for side in SIDES}
    for row in rows:
        totals["ask"] += float(row.get("askVolume", row.get("ask_volume", 0)) or 0)
        totals["bid"] += float(row.get("bidVolume", row.get("bid_volume", 0)) or 0)
        totals["unknown"] += float(row.get("unknownVolume", row.get("unknown_volume", 0)) or 0)
    return totals


def diff_payload(symbol: str, session_date: str, live: dict[str, float], eod: dict[str, float]) -> dict[str, object]:
    diffs = {}
    for side in SIDES:
        absolute = abs(live.get(side, 0.0) - eod.get(side, 0.0))
        denominator = max(abs(eod.get(side, 0.0)), 1.0)
        diffs[side] = {
            "live": live.get(side, 0.0),
            "eod": eod.get(side, 0.0),
            "absoluteDiff": absolute,
            "relativeDiff": absolute / denominator,
        }
    return {"symbol": symbol, "sessionDate": session_date, "diffs": diffs}


if __name__ == "__main__":
    raise SystemExit(main())
