from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.recommendations.professional_v2 import (  # noqa: E402
    FACTOR_KEYS,
    bounded_risk_penalty,
    fifo_holding_days,
    infer_risk_state,
    normalize_fundamental_batch,
    process_preference_events,
    resolve_algorithm_version,
    softmax_weights,
    style_prior,
    stable_digest,
)


NOW = datetime(2026, 7, 16, 16, 0, tzinfo=timezone.utc)


def test_v2_prior_and_softmax_are_nonnegative_and_sum_to_one_hundred() -> None:
    prior = style_prior("momentum")
    weights = softmax_weights({key: 0.0 for key in FACTOR_KEYS})

    assert set(prior) == set(FACTOR_KEYS)
    assert min(prior.values()) > 0
    assert abs(sum(prior.values()) - 100.0) < 1e-9
    assert abs(sum(weights.values()) - 100.0) < 1e-9


def test_buy_updates_preference_while_sell_and_missing_feature_are_audited_only() -> None:
    common = {
        "user_sub": "user-1",
        "average_fill_price": 100,
        "incremental_filled_qty": 10,
        "portfolio_equity": 20_000,
        "decision_at": NOW - timedelta(hours=1),
        "candidate_run_id": 1,
    }
    fills = [
        {
            **common,
            "id": 1,
            "order_id": "buy-1",
            "symbol": "FAST",
            "side": "buy",
            "feature_scores": {key: 80 for key in FACTOR_KEYS},
            "candidate_mean_scores": {key: 50 for key in FACTOR_KEYS},
        },
        {**common, "id": 2, "order_id": "sell-1", "symbol": "FAST", "side": "sell"},
        {**common, "id": 3, "order_id": "buy-2", "symbol": "MISS", "side": "buy"},
    ]

    state, events = process_preference_events(None, fills, style="balanced", cutoff=NOW)

    assert events[0]["event_status"] == "applied"
    assert events[1]["skip_reason"] == "sell_excluded"
    assert events[2]["skip_reason"] == "missing_point_in_time_feature"
    assert state["longSampleCount"] > 0
    assert max(state["longTermLogits"].values()) <= 2
    assert abs(sum(state["effectiveWeights"].values()) - 100.0) < 1e-9


def test_partial_fill_strength_is_capped_per_order() -> None:
    fill = {
        "user_sub": "user-1",
        "order_id": "order-1",
        "symbol": "FAST",
        "side": "buy",
        "average_fill_price": 100,
        "incremental_filled_qty": 100,
        "portfolio_equity": 1_000,
        "decision_at": NOW,
        "feature_scores": {key: 100 for key in FACTOR_KEYS},
        "candidate_mean_scores": {key: 0 for key in FACTOR_KEYS},
    }
    _state, events = process_preference_events(
        None,
        [{**fill, "id": 1}, {**fill, "id": 2}],
        style="balanced",
        cutoff=NOW,
    )

    assert events[0]["event_strength"] == 1
    assert events[1]["skip_reason"] == "order_strength_cap_reached"


def test_fundamental_batch_validates_provenance_cutoff_and_quality() -> None:
    payload = {
        "snapshotId": "snapshot-1",
        "schemaVersion": "fundamentals.v1",
        "featureVersion": "features.v1",
        "digest": "abc",
        "sourceAsOf": (NOW - timedelta(minutes=1)).isoformat(),
        "snapshots": {
            "FAST": {
                "value": 80,
                "quality": 70,
                "growth": 60,
                "earningsRevision": 50,
                "coverage": 1,
                "freshness": 0.8,
                "sourceQuality": 0.5,
            }
        },
    }

    rows, provenance = normalize_fundamental_batch(payload, ["FAST", "MISS"], NOW)
    future, future_provenance = normalize_fundamental_batch({**payload, "sourceAsOf": (NOW + timedelta(minutes=1)).isoformat()}, ["FAST"], NOW)

    assert provenance["status"] == "ready"
    assert rows["FAST"]["score"] == 68.0
    assert rows["FAST"]["weight"] == 0.06
    assert "MISS" not in rows
    assert future == {}
    assert future_provenance["status"] == "future_data"


def test_risk_inference_uses_strict_market_value_sample_gate_and_excludes_cost_basis() -> None:
    snapshots = []
    for index in range(20):
        observed = NOW - timedelta(days=38 - index * 2)
        snapshots.append({
            "source_as_of": observed,
            "payload": {
                "valuationBasis": "market_value",
                "totalValue": 10_000 + index * 25,
                "cash": 2_000,
                "positions": [
                    {"symbol": "FAST", "sector": "Information Technology", "marketValueForeign": 4_000},
                    {"symbol": "SAFE", "sector": "Health Care", "marketValueForeign": 4_000},
                ],
            },
        })
    snapshots.append({
        "source_as_of": NOW,
        "payload": {"valuationBasis": "cost_basis", "totalValue": 1, "positions": []},
    })
    fills = [{
        "symbol": "FAST",
        "side": "buy",
        "filled_at": NOW - timedelta(days=2),
        "incremental_filled_qty": 2,
        "average_fill_price": 100,
    }]

    state = infer_risk_state(
        {"risk_level": "balanced", "max_drawdown_pct": 6},
        snapshots,
        fills,
        cutoff=NOW,
    )

    assert state["evidenceStatus"]["portfolioHistoryReady"] is True
    assert state["evidenceStatus"]["marketValueSnapshotCount"] == 20
    assert state["effectiveBudget"]["maxDrawdownPct"] <= 6
    assert "turnover30dPct" in state["observedRisk"]


def test_bounded_risk_penalty_and_algorithm_selection() -> None:
    penalty = bounded_risk_penalty({
        "volatilityUse": 2,
        "turnoverUse": 2,
        "singleStockUse": 2,
        "sectorUse": 2,
    })

    assert penalty == 30
    assert resolve_algorithm_version("continuous-v2", enabled=False, shadow=True) == ("continuous-v2", False)
    assert resolve_algorithm_version(None, enabled=True, shadow=True) == ("professional-v1", True)


def test_turnover_falls_back_without_real_fills_and_empty_portfolio_keeps_five_percent_addition_room() -> None:
    snapshots = []
    for index in range(20):
        snapshots.append({
            "source_as_of": NOW - timedelta(days=38 - index * 2),
            "payload": {
                "valuationBasis": "market_value",
                "totalValue": 10_000,
                "cash": 10_000,
                "positions": [],
            },
        })

    state = infer_risk_state({"risk_level": "balanced"}, snapshots, [], cutoff=NOW)

    assert "turnover30dPct" not in state["observedRisk"]
    assert state["effectiveBudget"]["maximumTurnoverPct"] == 40
    assert state["effectiveBudget"]["maxSingleStockPct"] == 5
    assert state["effectiveBudget"]["maxSectorPct"] == 5


def test_fifo_counts_only_fully_closed_lots() -> None:
    fills = [
        {"symbol": "AAA", "side": "buy", "filled_at": NOW - timedelta(days=10), "incremental_filled_qty": 10},
        {"symbol": "AAA", "side": "sell", "filled_at": NOW - timedelta(days=5), "incremental_filled_qty": 5},
    ]
    assert fifo_holding_days(fills, cutoff=NOW) == []

    fills.append({"symbol": "AAA", "side": "sell", "filled_at": NOW - timedelta(days=2), "incremental_filled_qty": 5})
    assert fifo_holding_days(fills, cutoff=NOW) == [8.0]


def test_stable_digest_is_reproducible_across_key_order() -> None:
    assert stable_digest({"b": 2, "a": 1}) == stable_digest({"a": 1, "b": 2})
