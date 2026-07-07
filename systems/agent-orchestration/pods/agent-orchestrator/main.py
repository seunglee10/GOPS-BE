from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException

from gops_agents.orchestrator import AgentOrchestrator
from gops_agents.query_understanding import warm_entity_catalog_cache
from gops_agents.runtime.report_store import build_report_store_from_env
from gops_agents.synthesis import log_synthesis_runtime_diagnostics


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    warm_entity_catalog_cache()
    yield


store = build_report_store_from_env()
orchestrator = AgentOrchestrator(store)
log_synthesis_runtime_diagnostics("agent-orchestrator")
app = FastAPI(title="GOPS Agent Orchestrator", version="0.1.0", lifespan=app_lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "agent-orchestrator"}


@app.post("/analyze")
def analyze(request: dict[str, Any]) -> dict[str, Any]:
    report = orchestrator.analyze(request).to_dict()
    publish_agent_outputs(report)
    return report


@app.post("/layout/resolve")
def resolve_layout(request: dict[str, Any]) -> dict[str, Any]:
    return orchestrator.resolve_layout(request)


@app.get("/reports/{analysis_id}")
def get_report(analysis_id: str) -> dict[str, Any]:
    report = orchestrator.get_report(analysis_id)
    if not report:
        raise HTTPException(status_code=404, detail="Agent analysis report not found.")
    return report.to_dict()


@app.post("/reports/{analysis_id}/cancel")
def cancel_report(analysis_id: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = request or {}
    report = orchestrator.cancel_analysis(
        analysis_id,
        reason="canceled by user",
        user_id=str(payload.get("userId") or "") or None,
    ).to_dict()
    report["cancelAccepted"] = report.get("status") == "canceled"
    publish_agent_outputs(report)
    return report


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
        producer.flush(timeout=5)
    except Exception as exc:
        print(f"Agent output publish skipped: {exc}", flush=True)


def kafka_producer():
    global _producer
    if _producer is None:
        from alfaka.common.kafka_io import create_json_producer

        _producer = create_json_producer(
            os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
            "gops-agent-orchestrator",
        )
    return _producer
