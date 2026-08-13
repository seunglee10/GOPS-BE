from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SHARED = ROOT / "systems" / "agent-orchestration" / "shared"
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

from gops_agents.runtime.envelope import build_request_envelope, request_envelope_from_dict


def test_v2_envelope_carries_internal_user_and_instrument_without_changing_payload_symbol() -> None:
    envelope = build_request_envelope(
        {"symbol": "BRK-B", "intent": "analysis"},
        user_id="google-sub",
        app_user_id="11111111-1111-4111-8111-111111111111",
        request_id="agent-request-1",
    )
    payload = envelope.to_dict()

    assert payload["schema_version"] == "agent-analysis-request.v2"
    assert payload["user_sub"] == "google-sub"
    assert payload["app_user_id"] == "11111111-1111-4111-8111-111111111111"
    assert payload["instrument_id"]
    assert payload["payload"]["symbol"] == "BRK-B"


def test_v1_envelope_remains_readable_during_compatibility_window() -> None:
    envelope = request_envelope_from_dict({
        "schema_version": "agent-analysis-request.v1",
        "request_id": "legacy-request",
        "user_id": "legacy-sub",
        "submitted_at": "2026-08-13T00:00:00Z",
        "payload": {"symbol": "AAPL"},
    })

    assert envelope is not None
    assert envelope.schema_version == "agent-analysis-request.v1"
    assert envelope.user_sub == "legacy-sub"
    assert envelope.app_user_id is None
