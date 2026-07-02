from __future__ import annotations

import os
import traceback
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from .analysis_queue import AnalysisRequestQueue, build_deep_analysis_request_queue_from_env
from .contracts import AnalysisReport, utc_now_iso
from .orchestrator import AgentOrchestrator
from .report_store import ReportStore, build_report_store_from_env
from .request_envelope import (
    REQUEST_STATUS_COMPLETED,
    REQUEST_STATUS_DEEP_COMPLETED,
    REQUEST_STATUS_DEEP_PENDING,
    REQUEST_STATUS_FAILED,
    REQUEST_STATUS_RUNNING,
    AgentAnalysisRequestEnvelope,
    request_envelope_from_dict,
    status_report_for_envelope,
)


class AgentAnalysisWorker:
    def __init__(
        self,
        *,
        store: ReportStore | None = None,
        orchestrator: AgentOrchestrator | None = None,
        deep_queue: AnalysisRequestQueue | None = None,
    ):
        self.store = store or build_report_store_from_env()
        self.orchestrator = orchestrator or AgentOrchestrator(store=self.store)
        self.deep_queue = deep_queue

    def process_message(self, message: dict[str, Any]) -> dict[str, Any]:
        envelope = request_envelope_from_dict(message)
        if envelope is None:
            raise ValueError("Invalid agent analysis request envelope.")
        return self.process_envelope(envelope).to_dict()

    def process_envelope(self, envelope: AgentAnalysisRequestEnvelope):
        if envelope.idempotency_key:
            self.store.save_idempotency_mapping(envelope.user_id, envelope.idempotency_key, envelope.request_id)
        self._mark_started(envelope)
        try:
            payload = analysis_payload_for_envelope(envelope)
            report = self.orchestrator.analyze(payload)
            apply_worker_diagnostics(report, envelope)
            report = self._apply_deep_analysis_policy(envelope, report)
            publish_agent_outputs(report.to_dict())
            return report
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"
            failed = status_report_for_envelope(
                envelope,
                REQUEST_STATUS_FAILED,
                summary=f"{envelope.payload.get('symbol') or 'UNKNOWN'} analysis failed.",
                error=error,
            )
            failed.agentTrace["traceback"] = traceback.format_exc(limit=8)
            self.store.save(failed)
            publish_agent_outputs(failed.to_dict())
            return failed

    def _mark_started(self, envelope: AgentAnalysisRequestEnvelope) -> None:
        if envelope.mode != "deep":
            self.store.save(status_report_for_envelope(envelope, REQUEST_STATUS_RUNNING))
            return

        existing = self.store.get(envelope.request_id)
        if existing is None:
            self.store.save(status_report_for_envelope(envelope, REQUEST_STATUS_RUNNING))
            return
        existing.agentTrace.setdefault("deepAnalysis", {})
        existing.agentTrace["deepAnalysis"].update({
            "status": REQUEST_STATUS_RUNNING,
            "startedAt": utc_now_iso(),
        })
        self.store.save(existing)

    def _apply_deep_analysis_policy(
        self,
        envelope: AgentAnalysisRequestEnvelope,
        report: AnalysisReport,
    ) -> AnalysisReport:
        if envelope.mode == "deep":
            report.status = REQUEST_STATUS_DEEP_COMPLETED
            report.agentTrace.setdefault("deepAnalysis", {})
            report.agentTrace["deepAnalysis"].update({
                "status": REQUEST_STATUS_DEEP_COMPLETED,
                "completedAt": utc_now_iso(),
            })
            return self.store.save(report)

        if not should_queue_deep_analysis(envelope, report):
            return report

        deep_envelope = deep_envelope_for_hot_report(envelope)
        try:
            queue = self.deep_queue or build_deep_analysis_request_queue_from_env()
            queue.submit(deep_envelope)
        except Exception as exc:
            report.agentTrace.setdefault("deepAnalysis", {})
            report.agentTrace["deepAnalysis"].update({
                "status": "enqueue_failed",
                "error": f"{exc.__class__.__name__}: {exc}",
                "attemptedAt": utc_now_iso(),
            })
            return self.store.save(report)

        report.status = REQUEST_STATUS_DEEP_PENDING
        report.agentTrace.setdefault("deepAnalysis", {})
        report.agentTrace["deepAnalysis"].update({
            "status": REQUEST_STATUS_DEEP_PENDING,
            "requestId": deep_envelope.request_id,
            "queuedAt": utc_now_iso(),
            "mode": deep_envelope.mode,
        })
        return self.store.save(report)


def analysis_payload_for_envelope(envelope: AgentAnalysisRequestEnvelope) -> dict[str, Any]:
    payload = dict(envelope.payload)
    payload["requestId"] = envelope.request_id
    payload["analysisId"] = envelope.request_id
    payload.setdefault("createdAt", envelope.submitted_at)
    payload.setdefault("mode", envelope.mode)
    payload.setdefault("priority", envelope.priority)
    return payload


def apply_worker_diagnostics(report: AnalysisReport, envelope: AgentAnalysisRequestEnvelope) -> None:
    report.timing["queueWaitMs"] = queue_wait_ms(envelope.submitted_at)
    report.timing["workerMode"] = envelope.mode
    report.timing.setdefault("hotWorkerSaturation", False)
    report.timing.setdefault("deepWorkerSaturation", False)
    report.timing.setdefault("providerBulkheadRejected", 0)


def queue_wait_ms(submitted_at: str) -> float:
    try:
        submitted = datetime.fromisoformat(str(submitted_at).replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return round(max(0.0, (now - submitted).total_seconds() * 1000), 3)
    except Exception:
        return 0.0


def should_queue_deep_analysis(envelope: AgentAnalysisRequestEnvelope, report: AnalysisReport) -> bool:
    if envelope.mode != "hot" or report.status != REQUEST_STATUS_COMPLETED:
        return False
    if os.getenv("AGENT_DEEP_ANALYSIS_ENABLED", "false").strip().lower() not in {"1", "true", "yes", "on"}:
        return False
    payload = envelope.payload if isinstance(envelope.payload, dict) else {}
    if str(payload.get("deepAnalysis") or payload.get("deep_analysis") or "").strip().lower() in {"0", "false", "no", "off"}:
        return False
    return True


def deep_envelope_for_hot_report(envelope: AgentAnalysisRequestEnvelope) -> AgentAnalysisRequestEnvelope:
    payload = dict(envelope.payload)
    payload["requestId"] = envelope.request_id
    payload["analysisId"] = envelope.request_id
    payload["deepParentRequestId"] = envelope.request_id
    return replace(
        envelope,
        mode="deep",
        priority="normal" if envelope.priority == "interactive" else envelope.priority,
        payload=payload,
    )


_producer = None


def publish_agent_outputs(report: dict[str, Any]) -> None:
    if os.getenv("AGENT_PUBLISH_TO_KAFKA", "true").lower() not in {"1", "true", "yes"}:
        return
    try:
        producer = kafka_producer()
        analysis_topic = os.getenv("AGENT_ANALYSIS_RESULTS_TOPIC", "agents.analysis-results.v1")
        notification_topic = os.getenv("AGENT_NOTIFICATION_DECISIONS_TOPIC", "agents.notification-decisions.v1")
        symbol = report.get("symbol") or "UNKNOWN"
        producer.send(analysis_topic, key=str(symbol), value=report)
        decision = report.get("notificationDecision")
        if isinstance(decision, dict):
            producer.send(notification_topic, key=str(symbol), value=decision)
        producer.flush(timeout=float(os.getenv("AGENT_OUTPUT_KAFKA_FLUSH_SECONDS", "5")))
    except Exception as exc:
        print(f"Agent output publish skipped: {exc}", flush=True)


def kafka_producer():
    global _producer
    if _producer is None:
        from alfaka.common.kafka_io import create_json_producer

        _producer = create_json_producer(
            os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
            os.getenv("AGENT_ANALYSIS_WORKER_PRODUCER_CLIENT_ID", "gops-agent-analysis-worker"),
        )
    return _producer


def run_kafka_worker(
    *,
    topic_env: str = "AGENT_ANALYSIS_REQUESTS_TOPIC",
    default_topic: str = "agents.analysis-requests.v1",
    group_env: str = "AGENT_ANALYSIS_WORKER_GROUP_ID",
    default_group: str = "gops-agent-analysis-worker",
    client_env: str = "AGENT_ANALYSIS_WORKER_CLIENT_ID",
    default_client: str = "gops-agent-analysis-worker",
) -> None:
    from alfaka.common.kafka_io import create_json_consumer

    topic = os.getenv(topic_env, default_topic)
    group_id = os.getenv(group_env, default_group)
    consumer = create_json_consumer(
        [topic],
        os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
        group_id,
        os.getenv(client_env, default_client),
        enable_auto_commit=False,
        max_poll_records=os.getenv("AGENT_ANALYSIS_WORKER_MAX_POLL_RECORDS", "1"),
        max_poll_interval_ms=os.getenv("AGENT_ANALYSIS_WORKER_MAX_POLL_INTERVAL_MS", "300000"),
    )
    worker = AgentAnalysisWorker()
    max_messages = positive_int_or_none(os.getenv("AGENT_ANALYSIS_WORKER_MAX_MESSAGES"))
    processed = 0
    try:
        for message in consumer:
            worker.process_message(message.value)
            consumer.commit()
            processed += 1
            if max_messages is not None and processed >= max_messages:
                break
    finally:
        consumer.close()


def positive_int_or_none(value: str | None) -> int | None:
    try:
        parsed = int(value) if value is not None else 0
    except Exception:
        return None
    return parsed if parsed > 0 else None


def run_deep_analysis_worker() -> None:
    run_kafka_worker(
        topic_env="AGENT_DEEP_ANALYSIS_REQUESTS_TOPIC",
        default_topic="agents.deep-analysis-requests.v1",
        group_env="AGENT_DEEP_ANALYSIS_WORKER_GROUP_ID",
        default_group="gops-agent-deep-analysis-worker",
        client_env="AGENT_DEEP_ANALYSIS_WORKER_CLIENT_ID",
        default_client="gops-agent-deep-analysis-worker",
    )
