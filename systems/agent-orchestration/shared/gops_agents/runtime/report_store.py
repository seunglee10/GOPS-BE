from __future__ import annotations

import json
import os
import hashlib
from typing import Any

from .analysis_cache import agent_finding_from_dict, evidence_item_from_dict, final_answer_citation_from_dict, final_answer_from_dict, intent_route_from_dict
from ..contracts import (
    AgentAnswer,
    AgentSignal,
    AnalysisReport,
    DataSnapshot,
    FinalResponse,
    LatencyStage,
    LatencyTrace,
    LayoutProposal,
    MarketEvent,
    NotificationDecision,
    ResolvedEntity,
    RoutePlan,
    SynthesisInput,
)


DEFAULT_REPORT_KEY_PREFIX = "agent:report"
DEFAULT_REPORT_TTL_SECONDS = 43200
DEFAULT_IDEMPOTENCY_KEY_PREFIX = "agent:request:idempotency"


class ReportStore:
    def save(self, report: AnalysisReport) -> AnalysisReport:
        raise NotImplementedError

    def get(self, analysis_id: str) -> AnalysisReport | None:
        raise NotImplementedError

    def save_idempotency_mapping(self, user_id: str, idempotency_key: str, request_id: str, ttl_seconds: int | None = None) -> None:
        return None

    def get_idempotency_request_id(self, user_id: str, idempotency_key: str) -> str | None:
        return None


class InMemoryReportStore(ReportStore):
    def __init__(self):
        self._reports: dict[str, AnalysisReport] = {}
        self._idempotency: dict[tuple[str, str], str] = {}

    def save(self, report: AnalysisReport) -> AnalysisReport:
        self._reports[report.analysisId] = report
        return report

    def get(self, analysis_id: str) -> AnalysisReport | None:
        return self._reports.get(analysis_id)

    def save_idempotency_mapping(self, user_id: str, idempotency_key: str, request_id: str, ttl_seconds: int | None = None) -> None:
        if user_id and idempotency_key and request_id:
            self._idempotency[(str(user_id), str(idempotency_key))] = str(request_id)

    def get_idempotency_request_id(self, user_id: str, idempotency_key: str) -> str | None:
        return self._idempotency.get((str(user_id), str(idempotency_key)))


class RedisReportStore(ReportStore):
    def __init__(
        self,
        redis_client=None,
        *,
        redis_url: str | None = None,
        ttl_seconds: int | None = None,
        key_prefix: str | None = None,
    ):
        if redis_client is not None:
            self.redis = redis_client
        else:
            import redis

            self.redis = redis.from_url(redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
        self.ttl_seconds = int(ttl_seconds if ttl_seconds is not None else os.getenv("AGENT_REPORT_TTL_SECONDS", str(DEFAULT_REPORT_TTL_SECONDS)))
        self.key_prefix = key_prefix or os.getenv("AGENT_REPORT_KEY_PREFIX", DEFAULT_REPORT_KEY_PREFIX)
        self.idempotency_key_prefix = os.getenv("AGENT_IDEMPOTENCY_KEY_PREFIX", DEFAULT_IDEMPOTENCY_KEY_PREFIX)

    def save(self, report: AnalysisReport) -> AnalysisReport:
        if self.ttl_seconds <= 0:
            return report
        try:
            encoded = serialize_report(report)
            self.redis.setex(self._report_key(report.analysisId), self.ttl_seconds, encoded)
            self.redis.setex(self._latest_key(), self.ttl_seconds, encoded)
            self.redis.setex(self._latest_key(report.symbol), self.ttl_seconds, encoded)
        except Exception as exc:
            report.agentTrace["reportStoreWriteFailed"] = f"{exc.__class__.__name__}: {exc}"
            return report
        return report

    def get(self, analysis_id: str) -> AnalysisReport | None:
        try:
            payload = self.redis.get(self._report_key(analysis_id))
        except Exception:
            return None
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        return deserialize_report(payload) if payload else None

    def save_idempotency_mapping(self, user_id: str, idempotency_key: str, request_id: str, ttl_seconds: int | None = None) -> None:
        ttl = int(ttl_seconds if ttl_seconds is not None else os.getenv("AGENT_IDEMPOTENCY_TTL_SECONDS", str(self.ttl_seconds)))
        if ttl <= 0 or not user_id or not idempotency_key or not request_id:
            return
        try:
            self.redis.setex(self._idempotency_key(user_id, idempotency_key), ttl, request_id)
        except Exception:
            return None

    def get_idempotency_request_id(self, user_id: str, idempotency_key: str) -> str | None:
        if not user_id or not idempotency_key:
            return None
        try:
            payload = self.redis.get(self._idempotency_key(user_id, idempotency_key))
        except Exception:
            return None
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        return str(payload) if payload else None

    def _report_key(self, analysis_id: str) -> str:
        return f"{self.key_prefix}:{analysis_id}"

    def _latest_key(self, symbol: str | None = None) -> str:
        if symbol:
            return f"{self.key_prefix}:latest:{str(symbol).upper()}"
        return f"{self.key_prefix}:latest"

    def _idempotency_key(self, user_id: str, idempotency_key: str) -> str:
        return f"{self.idempotency_key_prefix}:{stable_idempotency_part(user_id)}:{stable_idempotency_part(idempotency_key)}"


def stable_idempotency_part(value: str) -> str:
    return hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:24]


def build_report_store_from_env() -> ReportStore:
    backend = os.getenv("AGENT_REPORT_STORE_BACKEND", "auto").strip().lower()
    if backend in {"off", "none", "memory", "false", "0"}:
        return InMemoryReportStore()
    if backend == "redis" or (backend == "auto" and os.getenv("REDIS_URL")):
        try:
            return RedisReportStore()
        except Exception:
            return InMemoryReportStore()
    return InMemoryReportStore()


def serialize_report(report: AnalysisReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, separators=(",", ":"), default=str)


def deserialize_report(payload: str | bytes | None) -> AnalysisReport | None:
    if not payload:
        return None
    try:
        decoded = json.loads(payload.decode("utf-8") if isinstance(payload, bytes) else payload)
    except Exception:
        return None
    return analysis_report_from_dict(decoded)


def analysis_report_from_dict(value: Any) -> AnalysisReport | None:
    if not isinstance(value, dict):
        return None
    analysis_id = str(value.get("analysisId") or "").strip()
    symbol = str(value.get("symbol") or "").strip()
    status = str(value.get("status") or "").strip()
    if not analysis_id or not symbol or not status:
        return None
    return AnalysisReport(
        analysisId=analysis_id,
        symbol=symbol,
        intent=str(value.get("intent") or ""),
        status=status,
        createdAt=str(value.get("createdAt") or ""),
        summary=str(value.get("summary") or ""),
        rationale=str(value.get("rationale") or ""),
        findings=[item for item in (agent_finding_from_dict(item) for item in value.get("findings", [])) if item],
        marketEvents=[market_event_from_dict(item) for item in value.get("marketEvents", []) if isinstance(item, dict)],
        providerEvidence=[item for item in (evidence_item_from_dict(item) for item in value.get("providerEvidence", [])) if item],
        route=intent_route_from_dict(value.get("route")),
        finalAnswer=final_answer_from_dict(value.get("finalAnswer")),
        notificationDecision=notification_decision_from_dict(value.get("notificationDecision")),
        layoutProposal=layout_proposal_from_dict(value.get("layoutProposal")),
        chartProposal=value.get("chartProposal") if isinstance(value.get("chartProposal"), dict) else None,
        dailySummaries=[item for item in value.get("dailySummaries", []) if isinstance(item, dict)],
        timing=dict(value.get("timing") or {}),
        routePlan=route_plan_from_dict(value.get("routePlan")),
        resolvedEntities=[item for item in (resolved_entity_from_dict(item) for item in value.get("resolvedEntities", [])) if item],
        snapshots=[item for item in (data_snapshot_from_dict(item) for item in value.get("snapshots", [])) if item],
        synthesisInput=synthesis_input_from_dict(value.get("synthesisInput")),
        finalResponse=final_response_from_dict(value.get("finalResponse")),
        latencyTrace=latency_trace_from_dict(value.get("latencyTrace")),
        agentAnswers=[item for item in (agent_answer_from_dict(item) for item in value.get("agentAnswers", [])) if item],
        agentTrace=dict(value.get("agentTrace") or {}),
    )


def market_event_from_dict(value: dict[str, Any]) -> MarketEvent:
    return MarketEvent.from_dict(value)


def notification_decision_from_dict(value: Any) -> NotificationDecision | None:
    if not isinstance(value, dict):
        return None
    try:
        return NotificationDecision(
            decisionId=str(value.get("decisionId") or ""),
            analysisId=str(value.get("analysisId") or ""),
            symbol=str(value.get("symbol") or "UNKNOWN"),
            level=str(value.get("level") or "none"),
            showToast=bool(value.get("showToast")),
            title=str(value.get("title") or ""),
            message=str(value.get("message") or ""),
            reason=str(value.get("reason") or ""),
            eventId=value.get("eventId") if isinstance(value.get("eventId"), str) else None,
            createdAt=str(value.get("createdAt") or ""),
            expiresAt=value.get("expiresAt") if isinstance(value.get("expiresAt"), str) else None,
            channels=[str(item) for item in value.get("channels", []) if isinstance(item, (str, int, float))],
        )
    except Exception:
        return None


def layout_proposal_from_dict(value: Any) -> LayoutProposal | None:
    if not isinstance(value, dict):
        return None
    try:
        return LayoutProposal(
            title=str(value.get("title") or ""),
            rationale=str(value.get("rationale") or ""),
            commands=[item for item in value.get("commands", []) if isinstance(item, dict)],
            autoApply=bool(value.get("autoApply", True)),
            panelPriorities=[item for item in value.get("panelPriorities", []) if isinstance(item, dict)],
            createdAt=str(value.get("createdAt") or ""),
            id=str(value.get("id") or ""),
        )
    except Exception:
        return None


def route_plan_from_dict(value: Any) -> RoutePlan | None:
    if not isinstance(value, dict):
        return None
    try:
        return RoutePlan(
            run_id=str(value.get("run_id") or ""),
            intent=str(value.get("intent") or ""),
            route_confidence=float(value.get("route_confidence") or 0.0),
            entity_candidates=[str(item) for item in value.get("entity_candidates", []) if isinstance(item, (str, int, float))],
            snapshot_bundle=[str(item) for item in value.get("snapshot_bundle", []) if isinstance(item, (str, int, float))],
            execution_mode=str(value.get("execution_mode") or "parallel_snapshots"),
            llm_calls_allowed=int(value.get("llm_calls_allowed") or 0),
        )
    except Exception:
        return None


def resolved_entity_from_dict(value: Any) -> ResolvedEntity | None:
    if not isinstance(value, dict):
        return None
    try:
        return ResolvedEntity(
            raw_name=str(value.get("raw_name") or ""),
            canonical_name=str(value.get("canonical_name") or ""),
            ticker=value.get("ticker") if isinstance(value.get("ticker"), str) else None,
            market=str(value.get("market") or "US"),
            asset_type=str(value.get("asset_type") or "stock"),
            graph_node_id=value.get("graph_node_id") if isinstance(value.get("graph_node_id"), str) else None,
            aliases=[str(item) for item in value.get("aliases", []) if isinstance(item, (str, int, float))],
            confidence=float(value.get("confidence") if isinstance(value.get("confidence"), (int, float)) else 0.5),
        )
    except Exception:
        return None


def agent_signal_from_dict(value: Any) -> AgentSignal | None:
    if not isinstance(value, dict):
        return None
    return AgentSignal(
        target=str(value.get("target") or ""),
        direction=str(value.get("direction") or "unknown"),
        horizon=str(value.get("horizon") or "unknown"),
        strength=str(value.get("strength") or "low"),
        reasoning=str(value.get("reasoning") or ""),
    )


def data_snapshot_from_dict(value: Any) -> DataSnapshot | None:
    if not isinstance(value, dict):
        return None
    try:
        return DataSnapshot(
            snapshot_id=str(value.get("snapshot_id") or ""),
            run_id=str(value.get("run_id") or ""),
            snapshot_type=str(value.get("snapshot_type") or ""),
            status=str(value.get("status") or "partial"),
            source=str(value.get("source") or "computed"),
            cache_hit=bool(value.get("cache_hit")),
            freshness=dict(value.get("freshness") or {}),
            summary=str(value.get("summary") or ""),
            signals=[item for item in (agent_signal_from_dict(item) for item in value.get("signals", [])) if item],
            evidence=[item for item in (evidence_item_from_dict(item) for item in value.get("evidence", [])) if item],
            data_quality=str(value.get("data_quality") or "low"),
            confidence=float(value.get("confidence") if isinstance(value.get("confidence"), (int, float)) else 0.5),
            latency_ms=float(value.get("latency_ms") if isinstance(value.get("latency_ms"), (int, float)) else 0.0),
            warnings=[str(item) for item in value.get("warnings", []) if isinstance(item, (str, int, float))],
        )
    except Exception:
        return None


def synthesis_input_from_dict(value: Any) -> SynthesisInput | None:
    if not isinstance(value, dict):
        return None
    return SynthesisInput(
        run_id=str(value.get("run_id") or ""),
        original_prompt=str(value.get("original_prompt") or ""),
        intent=str(value.get("intent") or ""),
        entities=[item for item in (resolved_entity_from_dict(item) for item in value.get("entities", [])) if item],
        snapshots=[item for item in (data_snapshot_from_dict(item) for item in value.get("snapshots", [])) if item],
        crossSignals=[item for item in value.get("crossSignals", []) if isinstance(item, dict)],
        missing_data=[str(item) for item in value.get("missing_data", []) if isinstance(item, (str, int, float))],
        risk_warnings=[str(item) for item in value.get("risk_warnings", []) if isinstance(item, (str, int, float))],
        output_policy=dict(value.get("output_policy") or {}),
    )


def final_response_from_dict(value: Any) -> FinalResponse | None:
    if not isinstance(value, dict):
        return None


def agent_answer_from_dict(value: Any) -> AgentAnswer | None:
    if not isinstance(value, dict):
        return None
    try:
        return AgentAnswer(
            agentId=str(value.get("agentId") or ""),
            role=str(value.get("role") or ""),
            title=str(value.get("title") or ""),
            content=str(value.get("content") or ""),
            confidence=float(value.get("confidence") if isinstance(value.get("confidence"), (int, float)) else 0.5),
            citations=[item for item in (final_answer_citation_from_dict(item) for item in value.get("citations", [])) if item],
            createdAt=str(value.get("createdAt") or ""),
        )
    except Exception:
        return None
    try:
        return FinalResponse(
            run_id=str(value.get("run_id") or ""),
            answer_type=str(value.get("answer_type") or "general_answer"),
            summary=str(value.get("summary") or ""),
            key_points=[str(item) for item in value.get("key_points", []) if isinstance(item, (str, int, float))],
            bullish_points=[str(item) for item in value.get("bullish_points", []) if isinstance(item, (str, int, float))],
            bearish_points=[str(item) for item in value.get("bearish_points", []) if isinstance(item, (str, int, float))],
            relationship_impacts=[str(item) for item in value.get("relationship_impacts", []) if isinstance(item, (str, int, float))],
            risk_warnings=[str(item) for item in value.get("risk_warnings", []) if isinstance(item, (str, int, float))],
            data_freshness_warnings=[str(item) for item in value.get("data_freshness_warnings", []) if isinstance(item, (str, int, float))],
            partial_data_used=bool(value.get("partial_data_used")),
            confidence=float(value.get("confidence") if isinstance(value.get("confidence"), (int, float)) else 0.5),
            final_stance=str(value.get("final_stance") or "not_applicable"),
            latency_ms=float(value.get("latency_ms") if isinstance(value.get("latency_ms"), (int, float)) else 0.0),
            llm_calls_used=int(value.get("llm_calls_used") if isinstance(value.get("llm_calls_used"), (int, float)) else 0),
        )
    except Exception:
        return None


def latency_trace_from_dict(value: Any) -> LatencyTrace | None:
    if not isinstance(value, dict):
        return None
    return LatencyTrace(
        run_id=str(value.get("run_id") or ""),
        total_latency_ms=float(value.get("total_latency_ms") if isinstance(value.get("total_latency_ms"), (int, float)) else 0.0),
        llm_calls_used=int(value.get("llm_calls_used") if isinstance(value.get("llm_calls_used"), (int, float)) else 0),
        stages=[stage for stage in (latency_stage_from_dict(item) for item in value.get("stages", [])) if stage],
    )


def latency_stage_from_dict(value: Any) -> LatencyStage | None:
    if not isinstance(value, dict):
        return None
    return LatencyStage(
        stage=str(value.get("stage") or ""),
        latency_ms=float(value.get("latency_ms") if isinstance(value.get("latency_ms"), (int, float)) else 0.0),
        status=str(value.get("status") or "success"),
        cache_hit=value.get("cache_hit") if isinstance(value.get("cache_hit"), bool) else None,
        input_tokens=value.get("input_tokens") if isinstance(value.get("input_tokens"), int) else None,
        output_tokens=value.get("output_tokens") if isinstance(value.get("output_tokens"), int) else None,
        cached_tokens=value.get("cached_tokens") if isinstance(value.get("cached_tokens"), int) else None,
    )
