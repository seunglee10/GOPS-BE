import json
import socket
import urllib.error
import urllib.request
from typing import Any

from fastapi import HTTPException

from app.core.config import read_dotenv_value

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


def request_agent_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    return request_orchestrator_json("POST", "/analyze", payload)


def get_agent_report(analysis_id: str) -> dict[str, Any]:
    return request_orchestrator_json("GET", f"/reports/{analysis_id}", None)


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
