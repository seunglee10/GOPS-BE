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
    build_cautions,
    build_decision,
    build_key_evidence,
    build_sizing,
    decision_explanation,
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


def test_key_evidence_uses_only_available_v3_blocks() -> None:
    item = {
        "action": "buy",
        "metricsSnapshot": {
            "availableBlocks": [
                "trendStrength",
                "participationConfirmation",
                "priceStructure",
                "catalystQuality",
                "executionQuality",
                "qualityStability",
            ],
            "blockScores": {
                "catalystQuality": 75,
                "executionQuality": 65,
                "qualityStability": 80,
            },
            "rawFactors": {
                "currentSessionRelativeStrength": 1.2,
                "last60MinuteRelativeStrength": 0.3,
                "clockAdjustedVolumeRatio": 1.4,
                "latestClose": 105,
                "vwap": 103,
                "quotedSpreadBps": 8,
                "atr": 2,
                "fundamentalAvailable": True,
                "realizedVolatility": 0.02,
            },
        },
    }

    complete = build_key_evidence(item)
    assert [row["code"] for row in complete] == [
        "market_strength",
        "participation",
        "execution_structure",
        "catalyst_quality",
        "execution_quality",
        "quality_stability",
    ]
    assert all(row["interpretation"] for row in complete)
    assert all(not any(character.isdigit() for character in row["interpretation"]) for row in complete)
    assert all(not any(token in row["interpretation"] for token in ("%p", "bp", "/100")) for row in complete)
    assert all(row["metrics"] for row in complete)
    assert all(
        0 <= metric["valuePositionPct"] <= 100
        and 0 <= metric["referencePositionPct"] <= 100
        and metric["value"]
        and metric["comparison"]
        for row in complete
        for metric in row["metrics"]
    )
    assert complete[0]["metrics"][0]["value"] == "+1.20%p"
    assert complete[1]["metrics"][0]["value"] == "1.40배"
    assert "종가 $105.00" in complete[2]["metrics"][0]["comparison"]

    item["metricsSnapshot"]["availableBlocks"] = [
        "trendStrength",
        "participationConfirmation",
        "priceStructure",
        "executionQuality",
    ]
    observed_only = build_key_evidence(item)
    assert [row["code"] for row in observed_only] == [
        "market_strength",
        "participation",
        "execution_structure",
        "execution_quality",
    ]


def test_cautions_are_structured_deduplicated_and_ground_chase_risk_in_prices() -> None:
    decision = {
        "action": "buy",
        "invalidationPrice": 99.5,
        "entryRoutes": [{"type": "breakout", "trigger": 105.0, "chaseLimit": 107.5}],
        "failedConditions": [
            {"code": "material_penalty", "label": "중대 위험 경고"},
            {"code": "last60_relative_strength", "label": "마감 전 상대강도"},
        ],
    }
    item = {
        "riskWarnings": ["가격 강도와 거래 참여 확인이 서로 일치하지 않습니다."],
        "metricsSnapshot": {"softPenalties": {"weakConfirmation": 6.0}},
    }

    cautions = build_cautions(item, decision)

    assert [row["code"] for row in cautions] == [
        "chase_limit",
        "last60_relative_strength",
        "weakConfirmation",
    ]
    assert all(row["severity"] in {"notice", "warning"} for row in cautions)
    assert len({row["code"] for row in cautions}) == len(cautions)
    assert len({row["sentence"] for row in cautions}) == len(cautions)
    assert cautions[0]["sentence"] == (
        "돌파 매수는 $107.50까지만 검토합니다. "
        "상한에서 무효화 기준 $99.50까지의 하락 폭은 주당 $8.00입니다."
    )
    assert not {"decision_scope", "confidence_scope"}.intersection(row["code"] for row in cautions)


def test_action_aware_renderer_v7_keeps_headline_and_body_natural() -> None:
    evidence = [
        {"interpretation": "SPY 대비 상대강도가 양수였습니다.", "metrics": [{"value": "+1.20%p"}]},
        {"interpretation": "동시간 거래량이 기준을 넘었습니다.", "metrics": [{"value": "1.40배"}]},
        {"interpretation": "종가는 $105.00로 VWAP $103.00보다 1.94% 높았습니다."},
        {"interpretation": "호가 스프레드는 8.00bp로 균형형 한도 10.0bp와 비교했습니다."},
    ]
    expected = {
        "buy": "계획된\u00a0가격대에서 매수를 검토",
        "conditional_buy": "남은 조건을 확인",
        "watch": "관찰 대상으로 유지",
        "not_suitable": "현재 계좌 한도",
    }
    for action, headline_part in expected.items():
        item = {
            "action": action,
            "keyEvidence": evidence,
            "counterEvidence": {
                "sentence": "마감 전에는 시장 대비 강도가 약해 추가 확인이 필요합니다."
            },
            "metricsSnapshot": {"evidenceReliability": 80},
        }
        primary = decision_explanation(item, target_session_date="2026-07-15")["primary"]
        assert primary["promptVersion"] == "recommendation-decision-renderer.ko.v7"
        assert headline_part in primary["headline"]
        if action == "conditional_buy":
            assert "거래\u00a0참여는\u00a0확인됐지만" in primary["headline"]
        if action == "buy":
            assert primary["body"] == ""
        else:
            assert primary["body"]
            assert 1 <= primary["body"].count("습니다.") <= 2
