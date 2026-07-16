from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable


DEFAULT_COMPANY_COMPARE_CACHE_PREFIX = "gops:agent:company-compare:v1"


class CompanyCompareNarrativeCache:
    def get(self, key: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def set(self, key: str, payload: dict[str, Any], ttl_seconds: int) -> None:
        raise NotImplementedError


class NullCompanyCompareNarrativeCache(CompanyCompareNarrativeCache):
    def get(self, key: str) -> dict[str, Any] | None:
        return None

    def set(self, key: str, payload: dict[str, Any], ttl_seconds: int) -> None:
        return None


@dataclass
class MemoryCompanyCompareCacheEntry:
    expires_at: float
    payload: str


class MemoryCompanyCompareNarrativeCache(CompanyCompareNarrativeCache):
    def __init__(self, *, now_fn: Callable[[], float] | None = None):
        self._items: dict[str, MemoryCompanyCompareCacheEntry] = {}
        self._now_fn = now_fn or time.time

    def get(self, key: str) -> dict[str, Any] | None:
        entry = self._items.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._now_fn():
            self._items.pop(key, None)
            return None
        payload = deserialize_narrative(entry.payload)
        if payload is None:
            self._items.pop(key, None)
        return payload

    def set(self, key: str, payload: dict[str, Any], ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        self._items[key] = MemoryCompanyCompareCacheEntry(
            expires_at=self._now_fn() + ttl_seconds,
            payload=serialize_narrative(payload),
        )


class RedisCompanyCompareNarrativeCache(CompanyCompareNarrativeCache):
    def __init__(self, redis_client=None, *, redis_url: str | None = None):
        if redis_client is not None:
            self.redis = redis_client
            return
        import redis

        self.redis = redis.from_url(
            redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
            socket_connect_timeout=float(os.getenv("REDIS_CONNECT_TIMEOUT_SECONDS", "0.2")),
            socket_timeout=float(os.getenv("REDIS_SOCKET_TIMEOUT_SECONDS", "0.2")),
            health_check_interval=int(os.getenv("REDIS_HEALTH_CHECK_INTERVAL_SECONDS", "30")),
        )

    def get(self, key: str) -> dict[str, Any] | None:
        try:
            payload = self.redis.get(key)
            return deserialize_narrative(payload) if payload else None
        except Exception:
            return None

    def set(self, key: str, payload: dict[str, Any], ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        try:
            self.redis.setex(key, ttl_seconds, serialize_narrative(payload))
        except Exception:
            return None


def build_company_compare_cache_from_env() -> CompanyCompareNarrativeCache:
    backend = os.getenv("AGENT_COMPANY_COMPARE_CACHE_BACKEND", "auto").strip().lower()
    if backend in {"off", "none", "null", "false", "0"}:
        return NullCompanyCompareNarrativeCache()
    if backend == "memory":
        return MemoryCompanyCompareNarrativeCache()
    if backend == "redis" or (backend == "auto" and os.getenv("REDIS_URL")):
        try:
            return RedisCompanyCompareNarrativeCache()
        except Exception:
            if backend == "redis":
                return NullCompanyCompareNarrativeCache()
    return MemoryCompanyCompareNarrativeCache()


def company_compare_cache_key(
    payload: dict[str, Any],
    *,
    prefix: str | None = None,
) -> str:
    base_symbol = normalize_symbol(payload.get("baseSymbol")) or "UNKNOWN"
    symbols = sorted({
        base_symbol,
        *(normalize_symbol(value) for value in payload.get("compareSymbols") or []),
    } - {""})
    sources = [item for item in payload.get("sources") or [] if isinstance(item, dict)]

    financial_versions = sorted(
        f"{normalize_symbol(source.get('symbol'))}-{source.get('asOf') or 'none'}-{source.get('accession') or 'none'}"
        for source in sources
        if str(source.get("id") or "").startswith(("financial:", "earnings:"))
    )
    ten_k_versions = sorted(
        f"{normalize_symbol(source.get('symbol'))}-{source.get('accession') or source.get('asOf') or 'none'}"
        for source in sources
        if str(source.get("id") or "").startswith("tenk:")
    )
    news_versions = sorted(
        f"{source.get('id') or 'news'}-{source.get('asOf') or 'none'}"
        for source in sources
        if str(source.get("id") or "").startswith("news:")
    )
    relationship_facts = stable_relationship_facts(payload.get("qualitative"))
    revision = {
        "baseSymbol": base_symbol,
        "symbols": symbols,
        "financial": financial_versions,
        "tenK": ten_k_versions,
        "news": news_versions,
        "relationship": relationship_facts,
        "question": str(payload.get("question") or "").strip(),
    }
    encoded = json.dumps(revision, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    digest = hashlib.sha1(encoded.encode("utf-8")).hexdigest()[:24]
    symbol_token = ",".join(symbols) or "UNKNOWN"
    financial_token = sanitize_key_component("_".join(financial_versions) or "none", max_length=96)
    ten_k_token = sanitize_key_component("_".join(ten_k_versions) or "none", max_length=96)
    cache_prefix = prefix or os.getenv("AGENT_COMPANY_COMPARE_CACHE_KEY_PREFIX", DEFAULT_COMPANY_COMPARE_CACHE_PREFIX)
    return (
        f"{cache_prefix}:{symbol_token}:base={base_symbol}:"
        f"asof={financial_token}:10k={ten_k_token}:rev={digest}"
    )


def stable_relationship_facts(qualitative: Any) -> list[dict[str, Any]]:
    if not isinstance(qualitative, dict):
        return []
    facts: list[dict[str, Any]] = []
    for section in qualitative.get("sections") or []:
        if not isinstance(section, dict) or section.get("id") != "relationship":
            continue
        for item in section.get("items") or []:
            if not isinstance(item, dict):
                continue
            facts.append({
                "kind": item.get("kind"),
                "symbol": item.get("symbol"),
                "title": item.get("title"),
                "summary": item.get("summary"),
                "details": item.get("details") or [],
            })
    return facts


def serialize_narrative(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def deserialize_narrative(payload: str) -> dict[str, Any] | None:
    try:
        decoded = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def sanitize_key_component(value: str, *, max_length: int) -> str:
    compact = re.sub(r"[^A-Za-z0-9._=-]+", "-", value).strip("-") or "none"
    if len(compact) <= max_length:
        return compact
    digest = hashlib.sha1(compact.encode("utf-8")).hexdigest()[:12]
    return f"{compact[: max_length - 13]}-{digest}"
