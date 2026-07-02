from __future__ import annotations

import json
import os
import sys

from gops_agents.analysis_queue import InProcessAnalysisRequestQueue
from gops_agents.analysis_worker import AgentAnalysisWorker
from gops_agents.report_store import InMemoryReportStore
from gops_agents.request_envelope import build_request_envelope


def main() -> int:
    os.environ.setdefault("AGENT_PUBLISH_TO_KAFKA", "false")
    store = InMemoryReportStore()
    queue = InProcessAnalysisRequestQueue()
    envelope = build_request_envelope(
        {"symbol": "NVDA", "intent": "뉴스 보여줘", "agentIds": ["agent-02"]},
        request_id="agent-queue-smoke",
        user_id="smoke-user",
        idempotency_key="agent-queue-smoke",
    )
    queue.submit(envelope)
    queued_metrics = queue.metrics().to_dict()
    message = queue.pop()
    if message is None:
        print(json.dumps({"status": "failed", "reason": "queue message missing"}))
        return 1
    popped_metrics = queue.metrics().to_dict()
    worker = AgentAnalysisWorker(store=store)
    report = worker.process_message(message)
    stored = store.get("agent-queue-smoke")
    idempotency_request_id = store.get_idempotency_request_id("smoke-user", "agent-queue-smoke")
    ok = (
        stored is not None
        and stored.status == "completed"
        and report.get("status") == "completed"
        and idempotency_request_id == "agent-queue-smoke"
    )
    print(json.dumps({
        "status": "ok" if ok else "failed",
        "requestId": "agent-queue-smoke",
        "reportStatus": report.get("status"),
        "storedStatus": stored.status if stored else None,
        "storedSymbol": stored.symbol if stored else None,
        "idempotencyRequestId": idempotency_request_id,
        "queuedMetrics": queued_metrics,
        "poppedMetrics": popped_metrics,
    }, ensure_ascii=False, separators=(",", ":")))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
