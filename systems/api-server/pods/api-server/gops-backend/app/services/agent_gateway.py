import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any

from fastapi import HTTPException

from app.core.config import read_dotenv_value
from gops_agents.runtime.admission import AdmissionPolicy, admit_analysis_request
from gops_agents.runtime.envelope import (
    REQUEST_STATUS_COMPLETED,
    REQUEST_STATUS_DEEP_COMPLETED,
    REQUEST_STATUS_QUEUED,
    accepted_response_for_report,
    build_request_envelope,
    status_report_for_envelope,
)
from gops_agents.runtime.queues import build_analysis_request_queue_from_env
from gops_agents.runtime.report_store import build_report_store_from_env

DEFAULT_ORCHESTRATOR_TIMEOUT_SECONDS = 60.0


def orchestrator_base_url() -> str:
    return (read_dotenv_value("AGENT_ORCHESTRATOR_URL") or "http://agent-orchestrator:8100").rstrip("/")


def orchestrator_timeout_seconds() -> float:
    raw_value = read_dotenv_value("AGENT_ORCHESTRATOR_TIMEOUT_SECONDS")
    if not raw_value:
        return DEFAULT_ORCHESTRATOR_TIMEOUT_SECONDS
    try:
        timeout = float(raw_value)
    except ValueError:
        return DEFAULT_ORCHESTRATOR_TIMEOUT_SECONDS
    return timeout if timeout > 0 else DEFAULT_ORCHESTRATOR_TIMEOUT_SECONDS


def request_agent_analysis(payload: dict[str, Any], *, idempotency_key: str | None = None, user_id: str | None = None) -> dict[str, Any]:
    if async_analysis_enabled():
        response = submit_agent_analysis(payload, idempotency_key=idempotency_key, user_id=user_id)
        if sync_compat_wait_enabled() and int(response.get("_status_code") or 202) == 202:
            return wait_for_agent_report(str(response.get("request_id") or response.get("analysisId") or ""))
        return response
    return request_orchestrator_json("POST", "/analyze", payload)


def get_agent_report(analysis_id: str) -> dict[str, Any]:
    if async_analysis_enabled() or shared_report_store_enabled():
        report = build_report_store_from_env().get(analysis_id)
        if report:
            return report.to_dict()
    return request_orchestrator_json("GET", f"/reports/{analysis_id}", None)


def submit_agent_analysis(payload: dict[str, Any], *, idempotency_key: str | None = None, user_id: str | None = None) -> dict[str, Any]:
    store = build_report_store_from_env()
    envelope = build_request_envelope(payload, idempotency_key=idempotency_key, user_id=user_id)
    if envelope.idempotency_key:
        existing_request_id = store.get_idempotency_request_id(envelope.user_id, envelope.idempotency_key)
        if existing_request_id:
            existing = store.get(existing_request_id)
            if existing:
                return response_for_report(existing)
    queue = build_analysis_request_queue_from_env()
    queue_metrics = queue.metrics()
    admission = admit_analysis_request(envelope, queue_metrics, policy=admission_policy_from_config())
    if not admission.accepted:
        rejected = status_report_for_envelope(envelope, "failed", error=admission.reason)
        rejected.agentTrace["admission"] = admission.to_dict()
        store.save(rejected)
        raise HTTPException(status_code=admission.status_code, detail=admission.reason)
    accepted = status_report_for_envelope(envelope, REQUEST_STATUS_QUEUED)
    accepted.agentTrace["admission"] = admission.to_dict()
    store.save(accepted)
    if envelope.idempotency_key:
        store.save_idempotency_mapping(envelope.user_id, envelope.idempotency_key, envelope.request_id)
    try:
        queue.submit(envelope)
    except Exception as exc:
        failed = status_report_for_envelope(envelope, "failed", error=f"{exc.__class__.__name__}: {exc}")
        failed.agentTrace["admission"] = admission.to_dict()
        failed.agentTrace["queue"] = {"error": f"{exc.__class__.__name__}: {exc}"}
        store.save(failed)
        raise HTTPException(status_code=503, detail="Agent analysis queue is unavailable.") from exc
    return accepted_response_for_report(accepted, status_code=202)


def response_for_report(report) -> dict[str, Any]:
    if report.status in {REQUEST_STATUS_COMPLETED, REQUEST_STATUS_DEEP_COMPLETED}:
        payload = report.to_dict()
        payload["_status_code"] = 200
        return payload
    return accepted_response_for_report(report, status_code=202)


def async_analysis_enabled() -> bool:
    return bool_config("AGENT_ASYNC_ANALYSIS_ENABLED", False)


def sync_compat_wait_enabled() -> bool:
    return bool_config("AGENT_SYNC_COMPAT_WAIT_ENABLED", False)


def shared_report_store_enabled() -> bool:
    return bool_config("AGENT_SHARED_REPORT_STORE_ENABLED", False)


def bool_config(name: str, default: bool) -> bool:
    value = read_dotenv_value(name)
    if value is None:
        value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def wait_for_agent_report(request_id: str) -> dict[str, Any]:
    if not request_id:
        raise HTTPException(status_code=500, detail="Agent request id is missing.")
    timeout_seconds = positive_float_config("AGENT_SYNC_COMPAT_WAIT_TIMEOUT_SECONDS", 3.0)
    poll_seconds = positive_float_config("AGENT_SYNC_COMPAT_WAIT_POLL_SECONDS", 0.1)
    deadline = time.monotonic() + timeout_seconds
    store = build_report_store_from_env()
    latest = None
    terminal_statuses = {REQUEST_STATUS_COMPLETED, REQUEST_STATUS_DEEP_COMPLETED, "failed"}
    while time.monotonic() <= deadline:
        report = store.get(request_id)
        if report is not None:
            latest = report
            if report.status in terminal_statuses:
                return response_for_report(report)
        time.sleep(poll_seconds)
    if latest is not None:
        return accepted_response_for_report(latest, status_code=202)
    raise HTTPException(status_code=504, detail="Agent analysis result was not available before the compatibility wait timeout.")


def positive_float_config(name: str, default: float) -> float:
    value = read_dotenv_value(name)
    if value is None:
        value = os.getenv(name)
    try:
        parsed = float(value) if value is not None else default
    except Exception:
        return default
    return parsed if parsed > 0 else default


def admission_policy_from_config() -> AdmissionPolicy:
    return AdmissionPolicy(
        enabled=bool_config("AGENT_ADMISSION_ENABLED", True),
        max_queue_depth=positive_int_config("AGENT_ADMISSION_MAX_QUEUE_DEPTH"),
        max_producer_buffered=positive_int_config("AGENT_ADMISSION_MAX_PRODUCER_BUFFERED"),
        allow_deep_mode=bool_config("AGENT_DEEP_ANALYSIS_ENABLED", False),
        degrade_stream_to_poll=bool_config("AGENT_ADMISSION_DEGRADE_STREAM_TO_POLL", True),
    )


def positive_int_config(name: str) -> int | None:
    value = read_dotenv_value(name)
    if value is None:
        value = os.getenv(name)
    try:
        parsed = int(value) if value is not None and str(value).strip() else 0
    except Exception:
        return None
    return parsed if parsed > 0 else None


def request_orchestrator_json(method: str, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    url = f"{orchestrator_base_url()}{path}"
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=orchestrator_timeout_seconds()) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=exc.code, detail=read_error_detail(exc)) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise HTTPException(status_code=504, detail="Agent orchestrator request timed out.") from exc
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
            raise HTTPException(status_code=504, detail="Agent orchestrator request timed out.") from exc
        raise HTTPException(status_code=503, detail="Agent orchestrator is unavailable.") from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="Agent orchestrator returned invalid JSON.") from exc


def read_error_detail(error: urllib.error.HTTPError) -> str:
    try:
        body = error.read().decode("utf-8")
    except Exception:
        return f"Agent orchestrator failed with HTTP {error.code}."
    if not body.strip():
        return f"Agent orchestrator failed with HTTP {error.code}."
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return body[:600]
    detail = parsed.get("detail") if isinstance(parsed, dict) else None
    return str(detail or body[:600])
