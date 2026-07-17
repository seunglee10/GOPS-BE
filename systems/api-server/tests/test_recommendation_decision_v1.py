from __future__ import annotations

import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "systems/api-server/pods/api-server/gops-backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.recommendations.decision_v1 import (  # noqa: E402
    build_decision,
    build_sizing,
)
from app.recommendations.fixed_replay import FixedReplayRecommendationProvider  # noqa: E402
from app.recommendations.repository import InMemoryRecommendationRepository  # noqa: E402


CUTOFF = datetime.fromisoformat("2026-07-14T16:00:00-04:00")


def test_entry_routes_use_fixed_cutoff_levels_and_targets_are_one_point_five_r() -> None:
    jpm = next(item for item in FixedReplayRecommendationProvider.load().payload["items"] if item["symbol"] == "JPM")

    decision = build_decision(jpm, risk_level="balanced", target_session_date="2026-07-15")

    assert decision["action"] == "buy"
    assert decision["forceExitAt"] == "2026-07-15T15:50:00-04:00"
    stop = decision["invalidationPrice"]
    for route in decision["entryRoutes"]:
        worst_entry = route.get("entryHigh") or route.get("chaseLimit")
        target = decision["targetPriceByRoute"][route["type"]]
        assert abs((target - worst_entry) / (worst_entry - stop) - 1.5) < 0.02


def test_sizing_applies_risk_budget_five_percent_cash_and_concentration_caps() -> None:
    jpm = next(item for item in FixedReplayRecommendationProvider.load().payload["items"] if item["symbol"] == "JPM")
    decision = build_decision(jpm, risk_level="balanced", target_session_date="2026-07-15")
    snapshot = {
        "source_as_of": "2026-07-14T15:59:00-04:00",
        "payload": {
            "totalValue": 100_000,
            "cash": 12_000,
            "positions": [{"symbol": "MSFT", "sector": "Information Technology", "marketValueForeign": 20_000}],
        },
    }

    sizing = build_sizing(
        jpm,
        decision=decision,
        risk_level="balanced",
        portfolio_snapshot=snapshot,
        cutoff=CUTOFF,
    )

    assert sizing["status"] == "ready"
    assert sizing["riskBudgetPct"] == 0.5
    assert sizing["recommendedShares"] >= 1
    assert sizing["estimatedNotional"] <= 5_000.01


def test_missing_required_price_evidence_cannot_be_direct_or_conditional_buy() -> None:
    jpm = deepcopy(next(item for item in FixedReplayRecommendationProvider.load().payload["items"] if item["symbol"] == "JPM"))
    del jpm["metricsSnapshot"]["rawFactors"]["last60MinuteLow"]

    decision = build_decision(jpm, risk_level="balanced", target_session_date="2026-07-15")

    assert decision["action"] == "watch"
    assert decision["entryRoutes"] == []
    assert decision["failedConditions"][0]["code"] == "missing_required_price_evidence"


def test_profile_and_preference_context_exclude_rows_after_cutoff() -> None:
    repository = InMemoryRecommendationRepository()
    repository.profile_history = [
        {"id": 1, "user_sub": "user-1", "payload": {"risk_level": "balanced"}, "source_as_of": "2026-07-14T15:00:00-04:00"},
        {"id": 2, "user_sub": "user-1", "payload": {"risk_level": "aggressive"}, "source_as_of": "2026-07-14T17:00:00-04:00"},
    ]
    repository.preference_states = [
        {"id": 1, "user_sub": "user-1", "state_version": 1, "payload": {"asOf": "2026-07-14T15:30:00-04:00", "marker": "eligible"}},
        {"id": 2, "user_sub": "user-1", "state_version": 2, "payload": {"asOf": "2026-07-14T16:30:00-04:00", "marker": "future"}},
    ]

    assert repository.get_profile_at("user-1", CUTOFF)["risk_level"] == "balanced"
    assert repository.get_preference_state_at("user-1", CUTOFF)["marker"] == "eligible"
