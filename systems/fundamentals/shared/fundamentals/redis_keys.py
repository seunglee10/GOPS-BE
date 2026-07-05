from __future__ import annotations


FUNDAMENTALS_REDIS_PREFIX = "gops:fundamentals"


def fundamentals_summary_key(symbol: str) -> str:
    return f"{FUNDAMENTALS_REDIS_PREFIX}:summary:v1:{normalize_symbol(symbol)}"


def fundamentals_peer_latest_key(symbol: str) -> str:
    return f"{FUNDAMENTALS_REDIS_PREFIX}:peer:v1:{normalize_symbol(symbol)}:latest"


def fundamentals_peer_key(symbol: str, frame_period: str) -> str:
    return f"{FUNDAMENTALS_REDIS_PREFIX}:peer:v1:{normalize_symbol(symbol)}:{str(frame_period or '').strip().upper()}"


def normalize_symbol(symbol: str) -> str:
    return str(symbol or "UNKNOWN").strip().upper() or "UNKNOWN"
