from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.market_data.indices.service import (
    DEFAULT_INTERVAL,
    DEFAULT_PERIOD,
    DEFAULT_UPSTREAM_TIMEOUT_SECONDS,
    MarketIndicesService,
    fetch_yahoo_indices,
    finite_float,
    isoformat_z,
    normalize_index_rows,
    read_positive_int,
    redis_key,
    round_float,
    sorted_valid_rows,
)

# 환율은 10분 캐시. 지수 스냅샷과 같은 Yahoo 조회를 쓰되 키와 TTL만 따로 둔다.
DEFAULT_FX_CACHE_TTL_SECONDS = 600
FX_SOURCE = "yahoo-finance"
SUPPORTED_PAIRS: dict[str, str] = {
    "USDKRW": "KRW=X",
}

Fetcher = Callable[..., Any]
_fx_service: Any | None = None


class FxUnavailable(RuntimeError):
    """Yahoo 조회가 실패했고 캐시도 없을 때."""


def get_fx_service(provider: Any | None = None) -> "FxRateService":
    global _fx_service
    if provider is not None:
        return FxRateService(provider=provider)
    if _fx_service is None:
        _fx_service = FxRateService()
    return _fx_service


def normalize_pair(pair: str | None) -> str:
    normalized = str(pair or "").strip().upper().replace("/", "").replace("-", "")
    if normalized not in SUPPORTED_PAIRS:
        raise ValueError(f"Unsupported fx pair: {pair}")
    return normalized


class FxRateService:
    def __init__(self, provider: Any | None = None, fetcher: Fetcher | None = None) -> None:
        if provider is None:
            from app.services.alfaka_market_data import get_market_data_provider

            provider = get_market_data_provider()
        self.provider = provider
        self.fetcher = fetcher or fetch_yahoo_indices
        # Redis가 없을 때를 위한 프로세스 메모리 캐시: pair -> (만료 시각, payload)
        self._memory: dict[str, tuple[float, dict[str, Any]]] = {}
        # Redis 읽기/쓰기는 지수 서비스와 같은 방식을 그대로 쓴다.
        self._cache = MarketIndicesService(provider=provider, fetcher=self.fetcher)

    def rate(self, pair: str) -> dict[str, Any]:
        normalized = normalize_pair(pair)
        cached = self._cache_get(normalized)
        if cached is not None:
            return dict(cached)
        try:
            payload = self._fetch(normalized)
        except Exception as exc:
            raise FxUnavailable(f"FX upstream failed: {exc.__class__.__name__}") from exc
        if payload is None:
            raise FxUnavailable(f"FX upstream returned no rows for {normalized}")
        self._cache_set(normalized, payload)
        return dict(payload)

    def _fetch(self, pair: str) -> dict[str, Any] | None:
        symbol = SUPPORTED_PAIRS[pair]
        timeout = read_positive_int("MARKET_INDICES_UPSTREAM_TIMEOUT_SECONDS", DEFAULT_UPSTREAM_TIMEOUT_SECONDS)
        raw = self.fetcher(symbols=[symbol], period=DEFAULT_PERIOD, interval=DEFAULT_INTERVAL, timeout=timeout)
        rows = sorted_valid_rows(normalize_index_rows(raw, [symbol]).get(symbol, []))
        if not rows:
            return None
        latest = rows[-1]
        rate = finite_float(latest.get("close"))
        if rate is None:
            return None
        as_of = latest.get("timestamp")
        return {
            "pair": pair,
            "rate": round_float(rate),
            "asOf": isoformat_z(as_of) if isinstance(as_of, datetime) else None,
            "source": FX_SOURCE,
        }

    def _cache_get(self, pair: str) -> dict[str, Any] | None:
        cached = self._cache._cache_get(fx_cache_key(pair))
        if cached is not None:
            return cached
        entry = self._memory.get(pair)
        if entry is None:
            return None
        expires_at, payload = entry
        if time.monotonic() >= expires_at:
            self._memory.pop(pair, None)
            return None
        return payload

    def _cache_set(self, pair: str, payload: dict[str, Any]) -> None:
        ttl = fx_cache_ttl_seconds()
        self._cache._cache_set(fx_cache_key(pair), payload, ttl)
        self._memory[pair] = (time.monotonic() + ttl, dict(payload))


def fx_cache_key(pair: str) -> str:
    return redis_key(f"fx:{pair}")


def fx_cache_ttl_seconds() -> int:
    return read_positive_int("MARKET_FX_CACHE_TTL_SECONDS", DEFAULT_FX_CACHE_TTL_SECONDS)
