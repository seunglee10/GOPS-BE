from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SHARED = ROOT / "systems" / "market-data" / "shared"
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

from alfaka.common.market_messages import build_raw_envelope
from alfaka.streaming.transforms import normalize_quote, normalize_trade


def simulator_payload(message_type: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "T": message_type,
        "S": "AMD",
        "t": "2026-07-18T03:00:00Z",
        "simulator": {
            "source": "gops-simulator",
            "scenarioId": "saturday-demo-amd-iff-oke",
            "runId": "sim-test",
            "phase": "market-open",
            "marketSession": "regular",
        },
    }
    if message_type == "t":
        payload.update({"i": 1, "p": 200.0, "s": 10})
    else:
        payload.update({"bp": 199.99, "bs": 8, "ap": 200.01, "as": 9})
    return payload


def test_simulation_metadata_overrides_the_weekend_session_and_survives_trade_normalization():
    envelope = build_raw_envelope(simulator_payload("t"), "sip", feed_profile="sip")
    trade = normalize_trade(envelope)

    assert envelope["marketSession"] == "regular"
    assert envelope["simulation"]["runId"] == "sim-test"
    assert trade["marketSession"] == "regular"
    assert trade["simulation"]["scenarioId"] == "saturday-demo-amd-iff-oke"


def test_simulation_metadata_survives_quote_normalization_for_paper_matching():
    envelope = build_raw_envelope(simulator_payload("q"), "sip", feed_profile="sip")
    quote = normalize_quote(envelope)

    assert quote["bidPrice"] == 199.99
    assert quote["askPrice"] == 200.01
    assert quote["simulation"]["source"] == "gops-simulator"
