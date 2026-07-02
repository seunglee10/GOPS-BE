from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Iterator


class ProviderBulkheadRejected(RuntimeError):
    pass


_LOCK = threading.Lock()
_SEMAPHORES: dict[tuple[str, int], threading.BoundedSemaphore] = {}


@contextmanager
def provider_bulkhead(provider: str) -> Iterator[None]:
    limit = provider_limit(provider)
    semaphore = semaphore_for(provider, limit)
    timeout_seconds = max(0.0, env_int("AGENT_PROVIDER_BULKHEAD_ACQUIRE_TIMEOUT_MS", 25) / 1000)
    acquired = semaphore.acquire(timeout=timeout_seconds)
    if not acquired:
        raise ProviderBulkheadRejected(f"{provider}_provider_bulkhead_rejected")
    try:
        yield
    finally:
        semaphore.release()


def provider_limit(provider: str) -> int:
    key = provider.strip().upper().replace("-", "_")
    specific = env_int(f"AGENT_PROVIDER_BULKHEAD_{key}_MAX_CONCURRENCY", 0)
    if specific > 0:
        return specific
    defaults = {
        "MARKET": 100,
        "NEWS": 100,
        "RELATIONSHIP": 20,
        "OPENAI": 20,
        "CLICKHOUSE": 50,
        "REDIS": 100,
        "GRAPHDB": 10,
    }
    return env_int("AGENT_PROVIDER_BULKHEAD_DEFAULT_MAX_CONCURRENCY", defaults.get(key, 50))


def semaphore_for(provider: str, limit: int) -> threading.BoundedSemaphore:
    key = (provider, limit)
    with _LOCK:
        semaphore = _SEMAPHORES.get(key)
        if semaphore is None:
            semaphore = threading.BoundedSemaphore(limit)
            _SEMAPHORES[key] = semaphore
        return semaphore


def env_int(name: str, default: int) -> int:
    try:
        parsed = int(os.getenv(name, str(default)))
    except Exception:
        return default
    return parsed if parsed > 0 else default
