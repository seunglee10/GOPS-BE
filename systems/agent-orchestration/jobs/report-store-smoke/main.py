from __future__ import annotations

import json
import sys

from gops_agents.runtime.envelope import REQUEST_STATUS_QUEUED, build_request_envelope, status_report_for_envelope
from gops_agents.runtime.report_store import build_report_store_from_env


def main() -> int:
    store = build_report_store_from_env()
    envelope = build_request_envelope(
        {"symbol": "NVDA", "intent": "report store smoke"},
        request_id="agent-report-store-smoke",
        user_id="smoke-user",
        idempotency_key="report-store-smoke",
    )
    report = status_report_for_envelope(envelope, REQUEST_STATUS_QUEUED)
    store.save(report)
    store.save_idempotency_mapping(envelope.user_id, envelope.idempotency_key or "", envelope.request_id)
    stored = store.get(envelope.request_id)
    mapped = store.get_idempotency_request_id(envelope.user_id, envelope.idempotency_key or "")
    ok = stored is not None and mapped == envelope.request_id
    print(json.dumps({
        "status": "ok" if ok else "failed",
        "backendReportFound": stored is not None,
        "idempotencyRequestId": mapped,
        "requestId": envelope.request_id,
    }, ensure_ascii=False, separators=(",", ":")))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
