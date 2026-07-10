from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from typing import Any, Callable

from alfaka.serving.chart_derived_data import (
    read_json_cache,
    redis_ttl_seconds,
    with_derived_metadata,
    write_json_cache,
)


_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: dict[str, Future] = {}


class DerivedCalculationService:
    def __init__(self, *, canonical_query, redis_client=None):
        self.canonical_query = canonical_query
        self.redis = redis_client
        self._metric_lock = threading.Lock()
        self._metrics: Counter[str] = Counter()

    def resolve(self, request: dict[str, Any], calculate: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        return self._resolve_inline(request, calculate)

    def query_candles(self, *args, **kwargs) -> dict[str, Any]:
        self._increment("provider_read")
        return self.canonical_query.query(*args, **kwargs)

    def metrics(self) -> dict[str, int]:
        with self._metric_lock:
            return dict(self._metrics)

    def _resolve_inline(self, request: dict[str, Any], calculate: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        cached = self._cached(request)
        if cached is not None:
            self._increment("cache_hit")
            return cached

        request_hash = request["requestHash"]
        owner = False
        with _INFLIGHT_LOCK:
            future = _INFLIGHT.get(request_hash)
            if future is None:
                future = Future()
                _INFLIGHT[request_hash] = future
                owner = True
        if not owner:
            self._increment("singleflight_wait")
            try:
                return future.result(timeout=inline_wait_seconds())
            except FutureTimeoutError:
                return self._calculate(request, calculate, write_cache=True)

        try:
            result = self._resolve_distributed(request, calculate)
            future.set_result(result)
            return result
        except Exception as exc:
            future.set_exception(exc)
            self._increment("failure")
            raise
        finally:
            with _INFLIGHT_LOCK:
                if _INFLIGHT.get(request_hash) is future:
                    _INFLIGHT.pop(request_hash, None)

    def _resolve_distributed(self, request: dict[str, Any], calculate: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        owner_token = self._acquire_lock(request)
        if owner_token is False:
            self._increment("singleflight_wait")
            deadline = time.monotonic() + inline_wait_seconds()
            while time.monotonic() < deadline:
                cached = self._cached(request)
                if cached is not None:
                    self._increment("cache_hit")
                    return cached
                time.sleep(0.025)
            return self._calculate(request, calculate, write_cache=True)
        try:
            return self._calculate(request, calculate, write_cache=True)
        finally:
            self._release_lock(request, owner_token)

    def _calculate(self, request: dict[str, Any], calculate: Callable[[], dict[str, Any]], *, write_cache: bool) -> dict[str, Any]:
        self._increment("calculate")
        try:
            payload = with_derived_metadata(
                calculate(),
                request,
                state="ready",
                source="api-compute",
                artifact_stored=False,
            )
        except Exception:
            self._increment("failure")
            raise
        payload["cache"] = {
            **(payload.get("cache") if isinstance(payload.get("cache"), dict) else {}),
            "hit": False,
            "ttlSeconds": redis_ttl_seconds(request["kind"]),
            "keyVersion": request["calculationVersion"],
        }
        if write_cache:
            write_json_cache(self.redis, request["cacheKey"], payload, redis_ttl_seconds(request["kind"]))
        return payload

    def _cached(self, request: dict[str, Any]) -> dict[str, Any] | None:
        payload = read_json_cache(self.redis, request["cacheKey"])
        if not payload:
            return None
        payload["cache"] = {
            **(payload.get("cache") if isinstance(payload.get("cache"), dict) else {}),
            "hit": True,
            "ttlSeconds": redis_ttl_seconds(request["kind"]),
            "keyVersion": request["calculationVersion"],
        }
        return with_derived_metadata(
            payload,
            request,
            state="ready",
            source="redis",
            artifact_stored=False,
        )

    def _acquire_lock(self, request: dict[str, Any]) -> str | bool | None:
        if self.redis is None:
            return None
        token = uuid.uuid4().hex
        try:
            acquired = self.redis.set(
                request["lockKey"],
                token,
                nx=True,
                ex=max(1, int(os.getenv("CHART_DERIVED_INLINE_LOCK_TTL_SECONDS", "30"))),
            )
        except (AttributeError, TypeError):
            return None
        except Exception:
            return None
        return token if acquired else False

    def _release_lock(self, request: dict[str, Any], owner_token: str | bool | None) -> None:
        if not isinstance(owner_token, str) or self.redis is None:
            return
        try:
            current = self.redis.get(request["lockKey"])
            if isinstance(current, bytes):
                current = current.decode("utf-8")
            if current == owner_token:
                self.redis.delete(request["lockKey"])
        except Exception:
            return

    def _increment(self, name: str) -> None:
        with self._metric_lock:
            self._metrics[name] += 1


def inline_wait_seconds() -> float:
    try:
        milliseconds = max(0, min(500, int(os.getenv("CHART_DERIVED_INLINE_WAIT_MS", "500"))))
    except (TypeError, ValueError):
        milliseconds = 500
    return milliseconds / 1000


def normalized_payload(payload: dict[str, Any]) -> str:
    value = json.loads(json.dumps(payload))
    value.pop("cache", None)
    derived = value.pop("derived", None)
    if isinstance(derived, dict):
        derived.pop("generatedAt", None)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
