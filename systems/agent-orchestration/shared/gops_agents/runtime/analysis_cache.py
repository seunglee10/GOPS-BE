from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

from ..contracts import AgentFinding, EvidenceItem, FinalAnswer, FinalAnswerCitation, FinalAnswerSection, IntentRoute


DEFAULT_ANALYSIS_CACHE_PREFIX = "gops:agent:analysis"


@dataclass
class CachedAgentAnalysis:
    route: IntentRoute
    findings: list[AgentFinding]
    providerEvidence: list[EvidenceItem]
    finalAnswer: FinalAnswer
    summary: str
    dailySummaries: list[dict[str, Any]] | None = None


class AgentAnalysisCache:
    def get(self, key: str) -> CachedAgentAnalysis | None:
        raise NotImplementedError

    def set(self, key: str, payload: CachedAgentAnalysis, ttl_seconds: int) -> None:
        raise NotImplementedError


class NullAgentAnalysisCache(AgentAnalysisCache):
    def get(self, key: str) -> CachedAgentAnalysis | None:
        return None

    def set(self, key: str, payload: CachedAgentAnalysis, ttl_seconds: int) -> None:
        return None


@dataclass
class MemoryAnalysisCacheEntry:
    expires_at: float
    payload: str


class MemoryAgentAnalysisCache(AgentAnalysisCache):
    def __init__(self, *, now_fn: Callable[[], float] | None = None):
        self._items: dict[str, MemoryAnalysisCacheEntry] = {}
        self._now_fn = now_fn or time.time

    def get(self, key: str) -> CachedAgentAnalysis | None:
        entry = self._items.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._now_fn():
            self._items.pop(key, None)
            return None
        payload = deserialize_cached_analysis(entry.payload)
        if payload is None:
            self._items.pop(key, None)
        return payload

    def set(self, key: str, payload: CachedAgentAnalysis, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        self._items[key] = MemoryAnalysisCacheEntry(
            expires_at=self._now_fn() + ttl_seconds,
            payload=serialize_cached_analysis(payload),
        )


class RedisAgentAnalysisCache(AgentAnalysisCache):
    def __init__(self, redis_client=None, *, redis_url: str | None = None):
        if redis_client is not None:
            self.redis = redis_client
        else:
            import redis

            self.redis = redis.from_url(redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)

    def get(self, key: str) -> CachedAgentAnalysis | None:
        try:
            payload = self.redis.get(key)
            return deserialize_cached_analysis(payload) if payload else None
        except Exception:
            return None

    def set(self, key: str, payload: CachedAgentAnalysis, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        try:
            self.redis.setex(key, ttl_seconds, serialize_cached_analysis(payload))
        except Exception:
            return None


def build_analysis_cache_from_env() -> AgentAnalysisCache:
    backend = os.getenv("AGENT_ANALYSIS_CACHE_BACKEND", "auto").strip().lower()
    if backend in {"off", "none", "null", "false", "0"}:
        return NullAgentAnalysisCache()
    if backend == "memory":
        return MemoryAgentAnalysisCache()
    if backend == "redis" or (backend == "auto" and os.getenv("REDIS_URL")):
        try:
            return RedisAgentAnalysisCache()
        except Exception:
            if backend == "redis":
                return NullAgentAnalysisCache()
    return MemoryAgentAnalysisCache()


def analysis_cache_key(*, symbol: str, payload: dict[str, Any], prefix: str | None = None) -> str:
    normalized_symbol = str(symbol or "UNKNOWN").strip().upper() or "UNKNOWN"
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str, separators=(",", ":"))
    digest = hashlib.sha1(encoded.encode("utf-8")).hexdigest()[:24]
    return f"{prefix or os.getenv('AGENT_ANALYSIS_CACHE_KEY_PREFIX', DEFAULT_ANALYSIS_CACHE_PREFIX)}:result:v1:{normalized_symbol}:{digest}"


def serialize_cached_analysis(payload: CachedAgentAnalysis) -> str:
    return json.dumps({
        "route": payload.route.to_dict(),
        "findings": [item.to_dict() for item in payload.findings],
        "providerEvidence": [item.to_dict() for item in payload.providerEvidence],
        "finalAnswer": payload.finalAnswer.to_dict(),
        "summary": payload.summary,
        "dailySummaries": list(payload.dailySummaries or []),
    }, ensure_ascii=False, separators=(",", ":"))


def deserialize_cached_analysis(payload: str) -> CachedAgentAnalysis | None:
    try:
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            return None
        route = intent_route_from_dict(decoded.get("route"))
        final_answer = final_answer_from_dict(decoded.get("finalAnswer"))
        if route is None or final_answer is None:
            return None
        return CachedAgentAnalysis(
            route=route,
            findings=[item for item in (agent_finding_from_dict(value) for value in decoded.get("findings", [])) if item],
            providerEvidence=[item for item in (evidence_item_from_dict(value) for value in decoded.get("providerEvidence", [])) if item],
            finalAnswer=final_answer,
            summary=str(decoded.get("summary") or final_answer.summary),
            dailySummaries=[item for item in decoded.get("dailySummaries", []) if isinstance(item, dict)],
        )
    except Exception:
        return None


def evidence_item_from_dict(value: Any) -> EvidenceItem | None:
    if not isinstance(value, dict):
        return None
    provider = str(value.get("provider") or "").strip()
    status = str(value.get("status") or "").strip()
    if not provider or not status:
        return None
    raw = value.get("raw")
    return EvidenceItem(
        provider=provider,
        status=status,
        title=str(value.get("title") or ""),
        summary=str(value.get("summary") or ""),
        observedAt=str(value.get("observedAt") or ""),
        url=value.get("url") if isinstance(value.get("url"), str) else None,
        raw=raw if isinstance(raw, dict) else {},
    )


def agent_finding_from_dict(value: Any) -> AgentFinding | None:
    if not isinstance(value, dict):
        return None
    agent_id = str(value.get("agentId") or "").strip()
    role = str(value.get("role") or "").strip()
    summary = str(value.get("summary") or "").strip()
    if not agent_id or not role or not summary:
        return None
    confidence = value.get("confidence")
    return AgentFinding(
        agentId=agent_id,
        role=role,
        summary=summary,
        rationale=str(value.get("rationale") or ""),
        confidence=float(confidence) if isinstance(confidence, (int, float)) else 0.5,
        evidence=[item for item in (evidence_item_from_dict(item) for item in value.get("evidence", [])) if item],
        tags=[str(item) for item in value.get("tags", []) if isinstance(item, (str, int, float))],
    )


def intent_route_from_dict(value: Any) -> IntentRoute | None:
    if not isinstance(value, dict):
        return None
    source = str(value.get("source") or "").strip()
    intent_type = str(value.get("intentType") or "").strip()
    if not source or not intent_type:
        return None
    confidence = value.get("confidence")
    return IntentRoute(
        source=source,
        intentType=intent_type,
        selectedRoles=[str(item) for item in value.get("selectedRoles", []) if isinstance(item, (str, int, float))],
        confidence=float(confidence) if isinstance(confidence, (int, float)) else 0.5,
        reason=str(value.get("reason") or ""),
    )


def final_answer_from_dict(value: Any) -> FinalAnswer | None:
    if not isinstance(value, dict):
        return None
    title = str(value.get("title") or "").strip()
    summary = str(value.get("summary") or "").strip()
    if not title or not summary:
        return None
    return FinalAnswer(
        title=title,
        summary=summary,
        sections=[section for section in (final_answer_section_from_dict(item) for item in value.get("sections", [])) if section],
        citations=[citation for citation in (final_answer_citation_from_dict(item) for item in value.get("citations", [])) if citation],
        limitations=[str(item) for item in value.get("limitations", []) if isinstance(item, (str, int, float))],
    )


def final_answer_section_from_dict(value: Any) -> FinalAnswerSection | None:
    if not isinstance(value, dict):
        return None
    title = str(value.get("title") or "").strip()
    if not title:
        return None
    return FinalAnswerSection(
        title=title,
        bullets=[str(item) for item in value.get("bullets", []) if isinstance(item, (str, int, float))],
    )


def final_answer_citation_from_dict(value: Any) -> FinalAnswerCitation | None:
    if not isinstance(value, dict):
        return None
    provider = str(value.get("provider") or "").strip()
    title = str(value.get("title") or "").strip()
    if not provider or not title:
        return None
    return FinalAnswerCitation(
        provider=provider,
        title=title,
        url=value.get("url") if isinstance(value.get("url"), str) else None,
        publishedAt=value.get("publishedAt") if isinstance(value.get("publishedAt"), str) else None,
    )
