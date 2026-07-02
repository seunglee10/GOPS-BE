from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Any

from .envelope import AgentAnalysisRequestEnvelope
from .queues import AnalysisQueueMetrics


@dataclass
class AdmissionPolicy:
    enabled: bool = True
    max_queue_depth: int | None = None
    max_producer_buffered: int | None = None
    allow_deep_mode: bool = False
    degrade_stream_to_poll: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass
class AdmissionDecision:
    accepted: bool
    reason: str = "accepted"
    status_code: int = 202
    degraded: bool = False
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "statusCode": self.status_code,
            "degraded": self.degraded,
            "metrics": dict(self.metrics),
        }


def admission_policy_from_env() -> AdmissionPolicy:
    return AdmissionPolicy(
        enabled=bool_env("AGENT_ADMISSION_ENABLED", True),
        max_queue_depth=positive_int_or_none(os.getenv("AGENT_ADMISSION_MAX_QUEUE_DEPTH")),
        max_producer_buffered=positive_int_or_none(os.getenv("AGENT_ADMISSION_MAX_PRODUCER_BUFFERED")),
        allow_deep_mode=bool_env("AGENT_DEEP_ANALYSIS_ENABLED", False),
        degrade_stream_to_poll=bool_env("AGENT_ADMISSION_DEGRADE_STREAM_TO_POLL", True),
    )


def admit_analysis_request(
    envelope: AgentAnalysisRequestEnvelope,
    queue_metrics: AnalysisQueueMetrics | None = None,
    *,
    policy: AdmissionPolicy | None = None,
) -> AdmissionDecision:
    policy = policy or admission_policy_from_env()
    metrics = {
        "policy": policy.to_dict(),
        "queue": queue_metrics.to_dict() if queue_metrics is not None else {},
    }
    if queue_metrics is not None:
        metrics["analysisQueueDepth"] = queue_metrics.queue_depth
        metrics["analysisConsumerLag"] = queue_metrics.consumer_lag
        metrics["analysisQueueBackend"] = queue_metrics.backend
    if not policy.enabled:
        return AdmissionDecision(accepted=True, reason="admission_disabled", metrics=metrics)

    if envelope.mode == "deep" and not policy.allow_deep_mode:
        return AdmissionDecision(
            accepted=False,
            reason="deep_mode_disabled",
            status_code=429,
            metrics=metrics,
        )

    queue_depth = queue_metrics.queue_depth if queue_metrics is not None else None
    if policy.max_queue_depth is not None and queue_depth is not None and queue_depth >= policy.max_queue_depth:
        return AdmissionDecision(
            accepted=False,
            reason="analysis_queue_backpressure",
            status_code=429,
            metrics=metrics,
        )

    producer_buffered = queue_metrics.producer_buffered if queue_metrics is not None else None
    if policy.max_producer_buffered is not None and producer_buffered is not None and producer_buffered >= policy.max_producer_buffered:
        return AdmissionDecision(
            accepted=False,
            reason="analysis_producer_backpressure",
            status_code=429,
            metrics=metrics,
        )

    if envelope.delivery.response_mode == "stream" and policy.degrade_stream_to_poll and queue_depth is not None and queue_depth > 0:
        envelope.delivery.response_mode = "poll"
        return AdmissionDecision(
            accepted=True,
            reason="stream_delivery_degraded_to_poll",
            degraded=True,
            metrics=metrics,
        )

    return AdmissionDecision(accepted=True, metrics=metrics)


def bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def positive_int_or_none(value: str | None) -> int | None:
    try:
        parsed = int(value) if value is not None and str(value).strip() else 0
    except Exception:
        return None
    return parsed if parsed > 0 else None
