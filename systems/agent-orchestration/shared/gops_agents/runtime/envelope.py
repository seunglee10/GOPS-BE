from __future__ import annotations

import uuid
import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

from ..contracts import AnalysisReport, stable_id, utc_now_iso
from ..security import sanitize_value


REQUEST_STATUS_ACCEPTED = "accepted"
REQUEST_STATUS_QUEUED = "queued"
REQUEST_STATUS_RUNNING = "running"
REQUEST_STATUS_COMPLETED = "completed"
REQUEST_STATUS_DEEP_PENDING = "deep_pending"
REQUEST_STATUS_DEEP_COMPLETED = "deep_completed"
REQUEST_STATUS_FAILED = "failed"
REQUEST_STATUS_CANCELED = "canceled"


@dataclass
class QuotaPolicy:
    llm_budget_owner: str = "platform"
    max_llm_calls: int = 1
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DeliveryPolicy:
    response_mode: str = "poll"
    callback_channel: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentAnalysisRequestEnvelope:
    request_id: str
    user_id: str
    submitted_at: str
    payload: dict[str, Any]
    schema_version: str = "agent-analysis-request.v2"
    user_sub: str | None = None
    app_user_id: str | None = None
    instrument_id: str | None = None
    idempotency_key: str | None = None
    mode: str = "hot"
    priority: str = "interactive"
    quota_policy: QuotaPolicy = field(default_factory=QuotaPolicy)
    delivery: DeliveryPolicy = field(default_factory=DeliveryPolicy)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "schema_version": self.schema_version,
            "user_id": self.user_id,
            "user_sub": self.user_sub or self.user_id,
            "app_user_id": self.app_user_id,
            "instrument_id": self.instrument_id,
            "idempotency_key": self.idempotency_key,
            "submitted_at": self.submitted_at,
            "mode": self.mode,
            "priority": self.priority,
            "quota_policy": self.quota_policy.to_dict(),
            "delivery": self.delivery.to_dict(),
            "payload": dict(self.payload),
        }


def build_request_envelope(
    payload: dict[str, Any],
    *,
    user_id: str | None = None,
    app_user_id: str | None = None,
    idempotency_key: str | None = None,
    request_id: str | None = None,
) -> AgentAnalysisRequestEnvelope:
    clean_payload = {key: value for key, value in dict(payload or {}).items() if value is not None}
    clean_payload.pop("userId", None)
    clean_payload.pop("submittedAt", None)
    user = str(user_id or "anonymous")
    submitted_at = utc_now_iso()
    idem_key = clean_string(idempotency_key) or clean_string(clean_payload.pop("idempotencyKey", None))
    mode = normalize_choice(clean_payload.pop("mode", None), {"hot", "deep", "background-refresh"}, "hot")
    priority = normalize_choice(clean_payload.pop("priority", None), {"interactive", "normal", "batch"}, "interactive")
    response_mode = normalize_choice(clean_payload.pop("responseMode", None), {"immediate", "poll", "stream"}, "poll")
    clean_payload.pop("maxLlmCalls", None)
    clean_payload.pop("maxInputTokens", None)
    clean_payload.pop("maxOutputTokens", None)
    clean_payload.pop("llmBudgetOwner", None)
    max_llm_calls = 1
    max_input_tokens = None
    max_output_tokens = None
    budget_owner = "platform"
    resolved_request_id = clean_string(request_id) or clean_string(clean_payload.pop("requestId", None))
    payload_result = sanitize_value(clean_payload)
    clean_payload = payload_result.value if isinstance(payload_result.value, dict) else {}
    instrument_id = instrument_id_for_symbol(clean_string(clean_payload.get("symbol")))
    if not resolved_request_id:
        resolved_request_id = request_id_for_payload(user, idem_key, clean_payload, submitted_at)
    return AgentAnalysisRequestEnvelope(
        request_id=resolved_request_id,
        user_id=user,
        user_sub=user,
        app_user_id=clean_string(app_user_id),
        instrument_id=instrument_id,
        idempotency_key=idem_key,
        submitted_at=submitted_at,
        mode=mode,
        priority=priority,
        quota_policy=QuotaPolicy(
            llm_budget_owner=budget_owner,
            max_llm_calls=max_llm_calls,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
        ),
        delivery=DeliveryPolicy(response_mode=response_mode),
        payload=clean_payload,
    )


def request_envelope_from_dict(value: Any) -> AgentAnalysisRequestEnvelope | None:
    if not isinstance(value, dict):
        return None
    request_id = clean_string(value.get("request_id") or value.get("requestId"))
    payload = value.get("payload")
    if not request_id or not isinstance(payload, dict):
        return None
    quota = value.get("quota_policy") if isinstance(value.get("quota_policy"), dict) else {}
    delivery = value.get("delivery") if isinstance(value.get("delivery"), dict) else {}
    return AgentAnalysisRequestEnvelope(
        request_id=request_id,
        user_id=str(value.get("user_id") or value.get("userId") or "anonymous"),
        schema_version=str(value.get("schema_version") or value.get("schemaVersion") or "agent-analysis-request.v1"),
        user_sub=clean_string(value.get("user_sub") or value.get("userSub") or value.get("user_id") or value.get("userId")),
        app_user_id=clean_string(value.get("app_user_id") or value.get("appUserId")),
        instrument_id=clean_string(value.get("instrument_id") or value.get("instrumentId")),
        idempotency_key=clean_string(value.get("idempotency_key") or value.get("idempotencyKey")),
        submitted_at=str(value.get("submitted_at") or value.get("submittedAt") or utc_now_iso()),
        mode=normalize_choice(value.get("mode"), {"hot", "deep", "background-refresh"}, "hot"),
        priority=normalize_choice(value.get("priority"), {"interactive", "normal", "batch"}, "interactive"),
        quota_policy=QuotaPolicy(
            llm_budget_owner=normalize_choice(quota.get("llm_budget_owner") or quota.get("llmBudgetOwner"), {"platform", "user"}, "platform"),
            max_llm_calls=parse_positive_int(quota.get("max_llm_calls") or quota.get("maxLlmCalls"), default=1),
            max_input_tokens=parse_optional_positive_int(quota.get("max_input_tokens") or quota.get("maxInputTokens")),
            max_output_tokens=parse_optional_positive_int(quota.get("max_output_tokens") or quota.get("maxOutputTokens")),
        ),
        delivery=DeliveryPolicy(
            response_mode=normalize_choice(delivery.get("response_mode") or delivery.get("responseMode"), {"immediate", "poll", "stream"}, "poll"),
            callback_channel=clean_string(delivery.get("callback_channel") or delivery.get("callbackChannel")),
        ),
        payload=dict(payload),
    )


def instrument_id_for_symbol(symbol: str | None) -> str | None:
    if not symbol:
        return None
    canonical = symbol.strip().upper().replace("-", ".")
    if not canonical:
        return None
    digest = hashlib.md5(f"gops-instrument:{canonical}".encode("utf-8"), usedforsecurity=False).hexdigest()
    return str(uuid.UUID(digest))


def status_report_for_envelope(
    envelope: AgentAnalysisRequestEnvelope,
    status: str,
    *,
    summary: str | None = None,
    error: str | None = None,
) -> AnalysisReport:
    payload_result = sanitize_value(envelope.payload if isinstance(envelope.payload, dict) else {})
    payload = payload_result.value if isinstance(payload_result.value, dict) else {}
    symbol = str(payload.get("symbol") or "UNKNOWN").strip().upper() or "UNKNOWN"
    intent = str(payload.get("intent") or payload.get("prompt") or "analysis")
    trace = {
        "requestEnvelope": {
            "requestId": envelope.request_id,
            "mode": envelope.mode,
            "priority": envelope.priority,
            "delivery": envelope.delivery.to_dict(),
        }
    }
    if payload_result.warnings:
        trace["inputGuardrail"] = {"warnings": list(payload_result.warnings)}
    if error:
        trace["error"] = error
    return AnalysisReport(
        analysisId=envelope.request_id,
        symbol=symbol,
        intent=intent,
        status=status,
        createdAt=utc_now_iso(),
        summary=summary or status_summary(status, symbol),
        rationale="The analysis request has been accepted for asynchronous processing.",
        findings=[],
        marketEvents=[],
        providerEvidence=[],
        dailySummaries=[],
        timing={},
        agentTrace=trace,
    )


def accepted_response_for_report(report: AnalysisReport, *, status_code: int = 202) -> dict[str, Any]:
    return {
        "_status_code": status_code,
        "request_id": report.analysisId,
        "analysisId": report.analysisId,
        "status": report.status,
        "status_url": f"/api/agents/reports/{report.analysisId}",
        "stream_url": f"/api/agents/reports/{report.analysisId}/stream",
        "report": report.to_dict(),
    }


def request_id_for_payload(user_id: str, idempotency_key: str | None, payload: dict[str, Any], submitted_at: str) -> str:
    if idempotency_key:
        return stable_id("agent-request", {"userId": user_id, "idempotencyKey": idempotency_key})
    return f"agent-request-{uuid.uuid4().hex[:16]}"


def status_summary(status: str, symbol: str) -> str:
    if status == REQUEST_STATUS_ACCEPTED:
        return f"{symbol} analysis request accepted."
    if status == REQUEST_STATUS_QUEUED:
        return f"{symbol} analysis request queued."
    if status == REQUEST_STATUS_RUNNING:
        return f"{symbol} analysis is running."
    if status == REQUEST_STATUS_FAILED:
        return f"{symbol} analysis failed."
    if status == REQUEST_STATUS_CANCELED:
        return f"{symbol} analysis was canceled."
    if status == REQUEST_STATUS_DEEP_PENDING:
        return f"{symbol} hot analysis is complete and deep analysis is queued."
    if status == REQUEST_STATUS_DEEP_COMPLETED:
        return f"{symbol} deep analysis is complete."
    return f"{symbol} analysis status is {status}."


def clean_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_choice(value: Any, allowed: set[str], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def parse_positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed if parsed > 0 else default


def parse_optional_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except Exception:
        return None
    return parsed if parsed > 0 else None
