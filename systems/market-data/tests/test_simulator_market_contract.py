from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SHARED = ROOT / "systems" / "market-data" / "shared"
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

from market_data.common.market_messages import build_raw_envelope
from market_data.storage.clickhouse_loader import clickhouse_actions_for_payload
from market_data.streaming.transforms import normalize_quote, normalize_trade


def simulator_payload(message_type: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "T": message_type,
        "S": "NVDA",
        "t": "2026-07-14T15:00:01Z",
        "simulator": {
            "source": "gops-simulator",
            "datasetId": "sp500-full-20260715-kst-v3",
            "runId": "sim-test",
            "marketSession": "regular",
        },
    }
    if message_type == "t":
        payload.update({"i": 1, "p": 200.0, "s": 10})
    else:
        payload.update({"bp": 199.99, "bs": 8, "ap": 200.01, "as": 9})
    return payload


def test_simulation_metadata_survives_trade_normalization():
    envelope = build_raw_envelope(simulator_payload("t"), "sip", feed_profile="sip")
    trade = normalize_trade(envelope)

    assert envelope["marketSession"] == "regular"
    assert envelope["simulation"]["runId"] == "sim-test"
    assert trade["marketSession"] == "regular"
    assert trade["simulation"]["datasetId"] == "sp500-full-20260715-kst-v3"


def test_simulation_metadata_survives_quote_normalization_for_paper_matching():
    envelope = build_raw_envelope(simulator_payload("q"), "sip", feed_profile="sip")
    quote = normalize_quote(envelope)

    assert quote["bidPrice"] == 199.99
    assert quote["askPrice"] == 200.01
    assert quote["simulation"]["source"] == "gops-simulator"


def test_simulation_ticks_are_not_written_to_durable_clickhouse_tables():
    envelope = build_raw_envelope(simulator_payload("t"), "sip", feed_profile="sip")
    trade = normalize_trade(envelope)

    assert clickhouse_actions_for_payload(trade, load_trades=True) == []


def test_untrusted_payload_cannot_override_the_market_session():
    payload = simulator_payload("t")
    payload["t"] = "2026-07-18T03:00:00Z"
    payload["simulator"] = {"source": "unknown-client", "marketSession": "regular"}

    envelope = build_raw_envelope(payload, "sip", feed_profile="sip")
    trade = normalize_trade(envelope)

    assert envelope["marketSession"] == "closed"
    assert "simulation" not in envelope
    assert "simulation" not in trade
