from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.recommendations.professional import FACTOR_KEYS as V1_FACTOR_KEYS  # noqa: E402
from app.recommendations.professional_v2 import process_preference_events  # noqa: E402
from app.recommendations.repository import (  # noqa: E402
    InMemoryRecommendationRepository,
    RecommendationRunCreate,
    RecommendationStateConflict,
)


NOW = datetime(2026, 7, 16, 16, 0, tzinfo=timezone.utc)


def run_create(user: str, key: str) -> RecommendationRunCreate:
    return RecommendationRunCreate(
        user_sub=user,
        run_key=key,
        slot_start="12:45",
        market_date="2026-07-16",
        status="completed",
        profile_snapshot={},
        market_snapshot_time=NOW.isoformat(),
        summary={"sessionMode": "regular"},
        algorithm_version="continuous-personalization-v2",
    )


def test_commit_v2_is_immutable_and_state_conflict_rolls_back_without_partial_mutation() -> None:
    repository = InMemoryRecommendationRepository()
    first = repository.commit_v2_run(
        run_create("user-a", "slot-1"),
        [{"symbol": "AAA", "rank": 1, "score": 70, "confidence": 0.7}],
        [{"symbol": "AAA", "evaluated_at": NOW}],
        {"preferenceConfidence": 0.2},
        [],
        {"effectiveBudget": {}},
        expected_preference_state_id=None,
    )
    replay = repository.commit_v2_run(
        run_create("user-a", "slot-1"),
        [{"symbol": "CHANGED", "rank": 1, "score": 99, "confidence": 0.9}],
        [],
        {},
        [],
        {},
        expected_preference_state_id=None,
    )

    assert replay["id"] == first["id"]
    assert replay["items"][0]["symbol"] == "AAA"
    before = (len(repository.preference_states), len(repository.risk_states), len(repository.runs), len(repository.candidate_features))
    with pytest.raises(RecommendationStateConflict):
        repository.commit_v2_run(
            run_create("user-a", "slot-2"), [], [], {}, [], {}, expected_preference_state_id=None
        )
    after = (len(repository.preference_states), len(repository.risk_states), len(repository.runs), len(repository.candidate_features))
    assert after == before


def test_v2_context_is_user_isolated_and_best_effort_seeds_only_from_complete_v1_same_run_scores() -> None:
    repository = InMemoryRecommendationRepository()
    scores = {key: 60.0 for key in V1_FACTOR_KEYS}
    other_scores = {key: 99.0 for key in V1_FACTOR_KEYS}
    own = repository.create_or_replace_run(
        run_create("user-a", "v1-a"),
        [
            {"symbol": "AAA", "rank": 1, "score": 60, "confidence": 0.7, "metricsSnapshot": {"professionalFactorScores": scores}},
            {"symbol": "BBB", "rank": 2, "score": 55, "confidence": 0.7, "metricsSnapshot": {"professionalFactorScores": {key: 50.0 for key in V1_FACTOR_KEYS}}},
        ],
    )
    other = repository.create_or_replace_run(run_create("user-b", "v2-b"), [])
    repository.runs[int(own["id"])]["generated_at"] = NOW - timedelta(hours=1)
    repository.runs[int(other["id"])]["generated_at"] = NOW - timedelta(hours=1)
    repository.candidate_features.append({
        "id": 1,
        "run_id": other["id"],
        "symbol": "AAA",
        "evaluated_at": NOW - timedelta(minutes=30),
        "available_factor_scores": other_scores,
        "candidate_mean_scores": other_scores,
    })
    repository.v2_fill_history.extend([
        {
            "id": 1,
            "fill_id": "kis:a",
            "observation_version": 1,
            "user_sub": "user-a",
            "order_id": "a",
            "symbol": "AAA",
            "side": "buy",
            "decision_at": NOW,
            "filled_at": NOW,
            "cumulative_filled_qty": 1,
            "average_fill_price": 100,
        },
        {
            "id": 2,
            "fill_id": "kis:b",
            "observation_version": 1,
            "user_sub": "user-b",
            "order_id": "b",
            "symbol": "AAA",
            "side": "buy",
            "decision_at": NOW,
            "filled_at": NOW,
            "cumulative_filled_qty": 1,
            "average_fill_price": 100,
        },
    ])

    context = repository.get_v2_context("user-a", NOW)

    assert len(context["fills"]) == 1
    assert context["fills"][0]["historical_seed"] is True
    assert context["fills"][0]["feature_scores"] == scores
    state, events = process_preference_events(None, context["fills"], style="balanced", cutoff=NOW)
    assert state["longSampleCount"] > 0
    assert events[0]["provenance"] == {"source": "historical_seed"}
