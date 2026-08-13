from __future__ import annotations

import argparse
import copy
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager, redirect_stdout
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


LAB_DIR = Path(__file__).resolve().parent
DEFAULT_SCENARIOS_PATH = LAB_DIR / "scenarios.json"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
TERMINAL_REPORT_STATUSES = {"completed", "deep_completed", "failed"}


@dataclass
class OpenAICallRecord:
    label: str
    model: str | None = None
    status: str = "ok"
    totalMs: float = 0.0
    urlopenMs: float = 0.0
    responseId: str | None = None
    serviceTier: str | None = None
    inputTokens: int | None = None
    outputTokens: int | None = None
    cachedTokens: int | None = None
    outputBytes: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass
class RunConfig:
    mode: str
    models: list[str]
    repeat: int
    warmup: int
    stream: bool
    timeout_seconds: float
    kafka_poll_timeout_seconds: float
    kafka_poll_interval_seconds: float
    scenarios_path: Path
    output_path: Path | None = None


class TracedResponse:
    def __init__(
        self,
        response: Any,
        record: OpenAICallRecord,
        started_at: float,
        on_complete,
    ):
        self._response = response
        self._record = record
        self._started_at = started_at
        self._on_complete = on_complete
        self._completed = False

    def __enter__(self):
        entered = self._response.__enter__()
        if entered is not None:
            self._response = entered
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is not None:
            self._complete(status="error", error=f"{exc.__class__.__name__}: {exc}")
        elif not self._completed:
            self._complete()
        return self._response.__exit__(exc_type, exc, tb)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)

    def read(self, *args, **kwargs):
        try:
            body = self._response.read(*args, **kwargs)
        except Exception as exc:
            self._complete(status="error", error=f"{exc.__class__.__name__}: {exc}")
            raise
        self._record.outputBytes = len(body) if isinstance(body, (bytes, bytearray)) else None
        self._complete(body=body)
        return body

    def _complete(self, *, body: bytes | None = None, status: str = "ok", error: str | None = None) -> None:
        if self._completed:
            return
        self._completed = True
        self._record.status = status
        self._record.totalMs = elapsed_ms(self._started_at)
        if error:
            self._record.error = error
        if body:
            apply_openai_response_metadata(self._record, decode_json_bytes(body))
        self._on_complete(self._record)


class OpenAITraceRecorder:
    def __init__(self):
        self.records: list[OpenAICallRecord] = []
        self._original_urlopen = None

    def install(self):
        self._original_urlopen = urllib.request.urlopen

        def traced_urlopen(request, *args, **kwargs):
            url = request_url(request)
            if OPENAI_RESPONSES_URL not in url:
                return self._original_urlopen(request, *args, **kwargs)
            payload = request_payload(request)
            record = OpenAICallRecord(
                label=openai_call_label(payload),
                model=payload.get("model") if isinstance(payload, dict) else None,
            )
            started_at = time.perf_counter()
            try:
                response = self._original_urlopen(request, *args, **kwargs)
            except Exception as exc:
                record.status = "error"
                record.totalMs = elapsed_ms(started_at)
                record.error = f"{exc.__class__.__name__}: {http_error_detail(exc)}"
                self.records.append(record)
                raise
            record.urlopenMs = elapsed_ms(started_at)
            return TracedResponse(response, record, started_at, self.records.append)

        urllib.request.urlopen = traced_urlopen
        return self

    def uninstall(self) -> None:
        if self._original_urlopen is not None:
            urllib.request.urlopen = self._original_urlopen
            self._original_urlopen = None

    def __enter__(self):
        return self.install()

    def __exit__(self, exc_type, exc, tb):
        self.uninstall()
        return False


def main() -> int:
    config = load_config(parse_args())
    scenarios = load_scenarios(config.scenarios_path)
    result = {
        "status": "ok",
        "startedAt": utc_now_iso(),
        "config": public_config(config),
        "runs": [],
    }
    try:
        modes = expand_modes(config.mode)
        for mode in modes:
            if mode == "openai":
                result["runs"].extend(run_openai_mode(config, scenarios.get("openai", [])))
            elif mode == "pipeline-direct":
                result["runs"].extend(run_pipeline_direct_mode(config, scenarios.get("pipeline", [])))
            elif mode == "pipeline-kafka":
                result["runs"].extend(run_pipeline_kafka_mode(config, scenarios.get("pipeline", [])))
            else:
                result["runs"].append({"mode": mode, "status": "error", "error": f"unknown mode: {mode}"})
    except Exception as exc:
        result["status"] = "error"
        result["error"] = f"{exc.__class__.__name__}: {exc}"
    result["summary"] = summarize_runs(result["runs"])
    emit_result(result, config.output_path)
    return 0 if result["status"] == "ok" else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Measure OpenAI and GOPS agent latency without changing runtime code.")
    parser.add_argument("--mode", default=None, help="openai, pipeline-direct, pipeline-kafka, or all")
    parser.add_argument("--scenarios", default=None, help="Path to scenarios JSON")
    parser.add_argument("--output", default=None, help="Optional JSON output path")
    return parser.parse_args()


def load_config(args: argparse.Namespace) -> RunConfig:
    mode = str(args.mode or os.getenv("OPENAI_LATENCY_LAB_MODE") or "all").strip().lower()
    models = split_csv(os.getenv("OPENAI_LATENCY_LAB_MODELS")) or [os.getenv("OPENAI_MODEL", "gpt-5.2")]
    repeat = env_int("OPENAI_LATENCY_LAB_REPEAT", 5)
    warmup = env_int("OPENAI_LATENCY_LAB_WARMUP", 1)
    timeout_seconds = env_float("OPENAI_LATENCY_LAB_TIMEOUT_SECONDS", env_float("OPENAI_TIMEOUT_SECONDS", 20.0))
    scenarios_path = Path(args.scenarios or os.getenv("OPENAI_LATENCY_LAB_SCENARIOS_PATH") or DEFAULT_SCENARIOS_PATH)
    output_raw = args.output or os.getenv("OPENAI_LATENCY_LAB_OUTPUT_PATH")
    return RunConfig(
        mode=mode,
        models=models,
        repeat=max(1, repeat),
        warmup=max(0, warmup),
        stream=env_bool("OPENAI_LATENCY_LAB_STREAM", False),
        timeout_seconds=max(0.1, timeout_seconds),
        kafka_poll_timeout_seconds=max(1.0, env_float("OPENAI_LATENCY_LAB_KAFKA_POLL_TIMEOUT_SECONDS", 120.0)),
        kafka_poll_interval_seconds=max(0.05, env_float("OPENAI_LATENCY_LAB_KAFKA_POLL_INTERVAL_SECONDS", 0.25)),
        scenarios_path=scenarios_path,
        output_path=Path(output_raw) if output_raw else None,
    )


def load_scenarios(path: Path) -> dict[str, list[dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("scenario file must contain a JSON object")
    return {
        "openai": [item for item in data.get("openai", []) if isinstance(item, dict)],
        "pipeline": [item for item in data.get("pipeline", []) if isinstance(item, dict)],
    }


def run_openai_mode(config: RunConfig, scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not os.getenv("OPENAI_API_KEY"):
        return [{"mode": "openai", "status": "skipped", "error": "OPENAI_API_KEY is not configured"}]
    rows = []
    for model in config.models:
        for scenario in scenarios:
            for index, warmup in iteration_plan(config):
                row = {
                    "mode": "openai",
                    "scenarioId": scenario_id(scenario),
                    "model": model,
                    "iteration": index,
                    "warmup": warmup,
                }
                try:
                    row.update(call_openai_scenario(scenario, model=model, config=config))
                except Exception as exc:
                    row.update({"status": "error", "error": f"{exc.__class__.__name__}: {http_error_detail(exc)}"})
                rows.append(row)
    return rows


def call_openai_scenario(scenario: dict[str, Any], *, model: str, config: RunConfig) -> dict[str, Any]:
    payload = copy.deepcopy(scenario.get("payload") if isinstance(scenario.get("payload"), dict) else {})
    if not payload:
        raise ValueError(f"scenario {scenario_id(scenario)} does not define payload")
    payload["model"] = model
    if config.stream:
        payload["stream"] = True
    request = urllib.request.Request(
        OPENAI_RESPONSES_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started_at = time.perf_counter()
    if config.stream:
        response_data, ttfb = read_streaming_response(request, config.timeout_seconds)
    else:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            response_data = json.loads(response.read().decode("utf-8"))
        ttfb = None
    total_ms = elapsed_ms(started_at)
    text = extract_response_text(response_data)
    parsed_json = decode_json_text(text)
    usage = response_data.get("usage") if isinstance(response_data, dict) else {}
    return {
        "status": str(response_data.get("status") or "completed") if isinstance(response_data, dict) else "unknown",
        "totalMs": round(total_ms, 3),
        "ttfbMs": round(ttfb, 3) if isinstance(ttfb, (int, float)) else None,
        "responseId": response_data.get("id") if isinstance(response_data, dict) else None,
        "serviceTier": response_data.get("service_tier") if isinstance(response_data, dict) else None,
        "inputTokens": token_count(usage, "input_tokens"),
        "outputTokens": token_count(usage, "output_tokens"),
        "cachedTokens": cached_token_count(usage),
        "outputBytes": len(text.encode("utf-8")) if isinstance(text, str) else 0,
        "parseOk": parsed_json is not None if scenario.get("expectJson", True) else bool(text),
    }


def read_streaming_response(request: urllib.request.Request, timeout_seconds: float) -> tuple[dict[str, Any], float | None]:
    first_payload_ms = None
    response_data: dict[str, Any] = {}
    started_at = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        while True:
            line = response.readline()
            if not line:
                break
            stripped = line.strip()
            if not stripped:
                continue
            if first_payload_ms is None:
                first_payload_ms = elapsed_ms(started_at)
            if not stripped.startswith(b"data:"):
                continue
            raw = stripped[len(b"data:") :].strip()
            if raw == b"[DONE]":
                break
            event = decode_json_bytes(raw)
            if not isinstance(event, dict):
                continue
            if event.get("type") == "response.completed" and isinstance(event.get("response"), dict):
                response_data = event["response"]
            elif event.get("type") == "response.failed" and isinstance(event.get("response"), dict):
                response_data = event["response"]
    return response_data, first_payload_ms


def run_pipeline_direct_mode(config: RunConfig, scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ensure_repo_paths()
    from gops_agents.orchestrator import AgentOrchestrator

    rows = []
    for scenario in scenarios:
        for index, warmup in iteration_plan(config):
            row = {
                "mode": "pipeline-direct",
                "scenarioId": scenario_id(scenario),
                "iteration": index,
                "warmup": warmup,
            }
            with temporary_env(scenario_env(scenario)):
                started_at = time.perf_counter()
                recorder = OpenAITraceRecorder()
                try:
                    with recorder:
                        with captured_runtime_stdout() as runtime_logs:
                            report = AgentOrchestrator().analyze(scenario_request(scenario))
                    row.update(report_row(report.to_dict()))
                    row["wallMs"] = round(elapsed_ms(started_at), 3)
                    row["endToEndMs"] = row["wallMs"]
                    row["openaiCalls"] = [item.to_dict() for item in recorder.records]
                    row["openaiCallCount"] = len(recorder.records)
                    if runtime_logs:
                        row["runtimeLogs"] = runtime_logs
                except Exception as exc:
                    row.update({"status": "error", "wallMs": round(elapsed_ms(started_at), 3), "error": f"{exc.__class__.__name__}: {exc}"})
                    row["openaiCalls"] = [item.to_dict() for item in recorder.records]
            rows.append(row)
    return rows


def run_pipeline_kafka_mode(config: RunConfig, scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ensure_repo_paths()
    if not os.getenv("KAFKA_BOOTSTRAP_SERVERS"):
        return [{"mode": "pipeline-kafka", "status": "skipped", "error": "KAFKA_BOOTSTRAP_SERVERS is not configured"}]
    from gops_agents.runtime.envelope import build_request_envelope, status_report_for_envelope
    from gops_agents.runtime.queues import build_analysis_request_queue_from_env
    from gops_agents.runtime.report_store import build_report_store_from_env

    rows = []
    for scenario in scenarios:
        for index, warmup in iteration_plan(config):
            row = {
                "mode": "pipeline-kafka",
                "scenarioId": scenario_id(scenario),
                "iteration": index,
                "warmup": warmup,
            }
            with temporary_env(scenario_env(scenario)):
                started_at = time.perf_counter()
                request_id = f"latency-lab-{scenario_id(scenario)}-{int(time.time() * 1000)}-{index}"
                try:
                    store = build_report_store_from_env()
                    queue = build_analysis_request_queue_from_env()
                    metrics_before = queue.metrics().to_dict()
                    if metrics_before.get("backend") != "kafka":
                        row.update({"status": "error", "error": f"queue backend is {metrics_before.get('backend')}, not kafka"})
                        rows.append(row)
                        continue
                    envelope = build_request_envelope(
                        scenario_request(scenario),
                        user_id="openai-latency-lab",
                        request_id=request_id,
                    )
                    store.save(status_report_for_envelope(envelope, "queued"))
                    queue.submit(envelope)
                    submitted_ms = elapsed_ms(started_at)
                    report = poll_report(store, request_id, config)
                    row.update(report_row(report.to_dict()))
                    row["submittedMs"] = round(submitted_ms, 3)
                    row["wallMs"] = round(elapsed_ms(started_at), 3)
                    row["endToEndMs"] = row["wallMs"]
                    row["queueMetricsBefore"] = metrics_before
                    row["queueMetricsAfter"] = queue.metrics().to_dict()
                except Exception as exc:
                    row.update({"status": "error", "wallMs": round(elapsed_ms(started_at), 3), "error": f"{exc.__class__.__name__}: {exc}"})
            rows.append(row)
    return rows


def poll_report(store: Any, request_id: str, config: RunConfig):
    deadline = time.monotonic() + config.kafka_poll_timeout_seconds
    latest = None
    while time.monotonic() <= deadline:
        latest = store.get(request_id)
        if latest is not None and latest.status in TERMINAL_REPORT_STATUSES:
            return latest
        time.sleep(config.kafka_poll_interval_seconds)
    if latest is not None:
        return latest
    raise TimeoutError(f"report {request_id} was not available within {config.kafka_poll_timeout_seconds:.1f}s")


def report_row(report: dict[str, Any]) -> dict[str, Any]:
    timing = report.get("timing") if isinstance(report.get("timing"), dict) else {}
    latency_trace = report.get("latencyTrace") if isinstance(report.get("latencyTrace"), dict) else None
    return {
        "status": str(report.get("status") or "unknown"),
        "analysisId": report.get("analysisId"),
        "symbol": report.get("symbol"),
        "intent": report.get("intent"),
        "route": report.get("route"),
        "timing": timing,
        "latencyTrace": latency_trace,
        "llmCalls": int(timing.get("llmCalls") or 0),
        "llmCallLabels": list(timing.get("llmCallLabels") or []),
        "totalMs": float(timing.get("totalMs") or 0.0),
        "queueWaitMs": float(timing.get("queueWaitMs") or 0.0),
        "queryUnderstandingMs": float(timing.get("queryUnderstandingMs") or 0.0),
        "intentClassifierMs": float(timing.get("intentClassifierMs") or 0.0),
        "finalAnswerMs": float(timing.get("finalAnswerMs") or 0.0),
    }


def scenario_request(scenario: dict[str, Any]) -> dict[str, Any]:
    request = scenario.get("request")
    if not isinstance(request, dict):
        raise ValueError(f"scenario {scenario_id(scenario)} does not define request")
    return copy.deepcopy(request)


def scenario_env(scenario: dict[str, Any]) -> dict[str, str]:
    env = scenario.get("env")
    if not isinstance(env, dict):
        return {}
    return {str(key): str(value) for key, value in env.items() if value is not None}


def iteration_plan(config: RunConfig) -> Iterable[tuple[int, bool]]:
    for index in range(config.warmup):
        yield index, True
    for index in range(config.repeat):
        yield index, False


def summarize_runs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[float]] = {}
    errors: dict[tuple[str, str, str], int] = {}
    for row in rows:
        mode = str(row.get("mode") or "unknown")
        scenario = str(row.get("scenarioId") or "unknown")
        model = str(row.get("model") or "")
        key = (mode, scenario, model)
        if row.get("warmup"):
            continue
        if row.get("status") in {"error", "skipped"}:
            errors[key] = errors.get(key, 0) + 1
            continue
        value = row.get("wallMs") if mode.startswith("pipeline-") else row.get("totalMs")
        if not value:
            value = row.get("totalMs") or row.get("wallMs")
        if isinstance(value, (int, float)) and value > 0:
            groups.setdefault(key, []).append(float(value))
    summary = []
    for key in sorted(set(groups) | set(errors)):
        values = groups.get(key, [])
        mode, scenario, model = key
        summary.append({
            "mode": mode,
            "scenarioId": scenario,
            "model": model or None,
            "count": len(values),
            "errors": errors.get(key, 0),
            "p50Ms": percentile(values, 50),
            "p95Ms": percentile(values, 95),
            "minMs": round(min(values), 3) if values else 0.0,
            "maxMs": round(max(values), 3) if values else 0.0,
        })
    return summary


def percentile(values: list[float], pct: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    index = min(len(ordered) - 1, max(0, round((pct / 100) * (len(ordered) - 1))))
    return round(ordered[index], 3)


def apply_openai_response_metadata(record: OpenAICallRecord, data: Any) -> None:
    if not isinstance(data, dict):
        return
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    record.responseId = data.get("id") if isinstance(data.get("id"), str) else None
    record.serviceTier = data.get("service_tier") if isinstance(data.get("service_tier"), str) else None
    record.inputTokens = token_count(usage, "input_tokens")
    record.outputTokens = token_count(usage, "output_tokens")
    record.cachedTokens = cached_token_count(usage)


def extract_response_text(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    chunks = []
    for output in data.get("output", []):
        if not isinstance(output, dict):
            continue
        for content in output.get("content", []):
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                chunks.append(content["text"])
    return "".join(chunks)


def request_url(request: Any) -> str:
    if isinstance(request, urllib.request.Request):
        return str(request.full_url)
    return str(request)


def request_payload(request: Any) -> dict[str, Any]:
    data = getattr(request, "data", None)
    if isinstance(data, bytes):
        return decode_json_bytes(data) or {}
    return {}


def openai_call_label(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return "openai"
    text = json.dumps(payload.get("input", ""), ensure_ascii=False)
    lowered = text.lower()
    if "parse one korean stock-app query" in lowered:
        return "intent-classifier"
    if "route a stock analysis request" in lowered:
        return "router"
    if "single role agent" in lowered:
        return "role-answer"
    if "stock-analysis answers" in lowered:
        return "synthesis"
    return "openai"


def decode_json_bytes(value: bytes | bytearray | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(bytes(value).decode("utf-8"))
    except Exception:
        return None


def decode_json_text(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def token_count(usage: Any, key: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    value = usage.get(key)
    return int(value) if isinstance(value, (int, float)) else None


def cached_token_count(usage: Any) -> int | None:
    if not isinstance(usage, dict):
        return None
    details = usage.get("input_tokens_details")
    if isinstance(details, dict) and isinstance(details.get("cached_tokens"), (int, float)):
        return int(details["cached_tokens"])
    return None


def http_error_detail(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = ""
        return f"HTTP {exc.code}: {body[:500] or exc.reason}"
    return str(exc)


def elapsed_ms(started_at: float) -> float:
    return (time.perf_counter() - started_at) * 1000


def expand_modes(mode: str) -> list[str]:
    if mode == "all":
        return ["openai", "pipeline-direct", "pipeline-kafka"]
    return [item for item in split_csv(mode) if item]


def split_csv(value: str | None) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@contextmanager
def temporary_env(values: dict[str, str]):
    backup = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def captured_runtime_stdout():
    buffer = io.StringIO()
    logs: list[str] = []
    with redirect_stdout(buffer):
        yield logs
    if env_bool("OPENAI_LATENCY_LAB_CAPTURE_RUNTIME_LOGS", False):
        lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
        logs.extend(lines[-20:])


def ensure_repo_paths() -> None:
    root = repo_root()
    for path in [
        root / "systems" / "agent-orchestration" / "shared",
        root / "systems" / "market-data" / "shared",
        root / "systems" / "api-server" / "pods" / "api-server",
    ]:
        text = str(path)
        if text not in sys.path:
            sys.path.insert(0, text)


def repo_root() -> Path:
    for parent in [LAB_DIR, *LAB_DIR.parents]:
        if (parent / "systems").is_dir() and (parent / "docker-compose.yml").exists():
            return parent
    return LAB_DIR.parents[3]


def scenario_id(scenario: dict[str, Any]) -> str:
    return str(scenario.get("id") or "unnamed")


def public_config(config: RunConfig) -> dict[str, Any]:
    return {
        "mode": config.mode,
        "models": config.models,
        "repeat": config.repeat,
        "warmup": config.warmup,
        "stream": config.stream,
        "timeoutSeconds": config.timeout_seconds,
        "kafkaPollTimeoutSeconds": config.kafka_poll_timeout_seconds,
        "scenariosPath": str(config.scenarios_path),
        "outputPath": str(config.output_path) if config.output_path else None,
    }


def emit_result(result: dict[str, Any], output_path: Path | None) -> None:
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
    print(encoded, flush=True)
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(encoded + "\n", encoding="utf-8")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
