from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.recommendations.professional import (  # noqa: E402
    STYLE_WEIGHTS,
    WEIGHTS_VERSION,
    ProfessionalContext,
    apply_professional_personalization,
    personalization_digest,
    resolve_weight_set,
)


NOW = datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc)


def test_professional_style_priors_are_nonnegative_and_sum_to_one_hundred() -> None:
    assert WEIGHTS_VERSION == "professional-personalization-v1"
    assert set(STYLE_WEIGHTS) == {"momentum", "balanced", "stable"}
    assert all(sum(weights.values()) == 100 for weights in STYLE_WEIGHTS.values())
    assert all(weight >= 0 for weights in STYLE_WEIGHTS.values() for weight in weights.values())
    assert STYLE_WEIGHTS["momentum"]["oneDayRelativeStrength"] == 25
    assert STYLE_WEIGHTS["stable"]["lowVolatilityQuality"] == 30


def test_style_changes_factor_ranking_without_changing_factor_set() -> None:
    daily = {
        "FAST": daily_rows("FAST", last_return=10.0, previous_return=3.0, base_volume=2_000_000, spike=4.0, volatile=True),
        "SAFE": daily_rows("SAFE", last_return=1.2, previous_return=0.8, base_volume=12_000_000, spike=1.05, volatile=False),
        "SPY": daily_rows("SPY", last_return=0.2, previous_return=0.1, base_volume=20_000_000, spike=1.0, volatile=False),
    }
    minutes = {
        "FAST": minute_rows(100, 104),
        "SAFE": minute_rows(100, 100.6),
        "SPY": minute_rows(100, 100.1),
    }
    items = [candidate("FAST"), candidate("SAFE")]
    common = dict(
        risk_level="balanced",
        daily_candles_by_symbol=daily,
        previous_session_candles_by_symbol=minutes,
        news_by_symbol={"FAST": [{"id": "n1", "sentiment": "positive", "publishedAt": "2026-07-14T20:00:00Z"}]},
        portfolio_snapshot=None,
        now=NOW,
    )

    momentum = apply_professional_personalization(
        items, context=ProfessionalContext(style="momentum", **common), shadow=False
    )
    stable = apply_professional_personalization(
        items, context=ProfessionalContext(style="stable", **common), shadow=False
    )

    assert momentum[0]["symbol"] == "FAST"
    assert stable[0]["symbol"] == "SAFE"
    assert set(momentum[0]["metricsSnapshot"]["professionalFactorScores"]) == set(
        stable[0]["metricsSnapshot"]["professionalFactorScores"]
    )
    assert momentum[0]["metricsSnapshot"]["scoring"]["weightsVersion"] == WEIGHTS_VERSION
    assert momentum[0]["metricsSnapshot"]["scoring"]["alphaWeight"] == 1.0
    assert momentum[0]["metricsSnapshot"]["scoring"]["portfolioWeight"] == 0.0
    assert momentum[0]["metricsSnapshot"]["predictedExcessReturnPct"] > 0


def test_personalization_digest_changes_with_style_or_portfolio_history() -> None:
    first = personalization_digest(
        profile={"risk_level": "balanced", "recommendation_style": "balanced", "updated_at": "a"},
        portfolio_snapshot={"id": 10, "source_as_of": "2026-07-14T20:00:00Z"},
        shadow=True,
    )
    changed_style = personalization_digest(
        profile={"risk_level": "balanced", "recommendation_style": "stable", "updated_at": "b"},
        portfolio_snapshot={"id": 10, "source_as_of": "2026-07-14T20:00:00Z"},
        shadow=True,
    )
    changed_portfolio = personalization_digest(
        profile={"risk_level": "balanced", "recommendation_style": "balanced", "updated_at": "a"},
        portfolio_snapshot={"id": 11, "source_as_of": "2026-07-14T20:05:00Z"},
        shadow=True,
    )

    assert len({first, changed_style, changed_portfolio}) == 3


def test_learned_weight_registry_requires_oos_approval_and_bounded_drift() -> None:
    styles = {style: dict(weights) for style, weights in STYLE_WEIGHTS.items()}
    styles["momentum"]["oneDayRelativeStrength"] -= 5
    styles["momentum"]["liquidityQuality"] += 5
    approved = resolve_weight_set({
        "version": "professional-2026-07",
        "trainingCutoff": "2026-06-30T20:00:00Z",
        "styles": styles,
        "validation": {"approved": True, "outOfSampleImprovement": True},
    })

    assert approved.version == "professional-2026-07"
    assert sum(approved.styles["momentum"].values()) == 100
    with pytest.raises(ValueError, match="out-of-sample"):
        resolve_weight_set({
            "version": "rejected",
            "trainingCutoff": "2026-06-30T20:00:00Z",
            "styles": styles,
            "validation": {"approved": True, "outOfSampleImprovement": False},
        })


def candidate(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "rank": 0,
        "score": 50,
        "confidence": 0.8,
        "sector": "Information Technology",
        "reasons": [],
        "riskWarnings": [],
        "metricsSnapshot": {},
    }


def daily_rows(
    symbol: str,
    *,
    last_return: float,
    previous_return: float,
    base_volume: float,
    spike: float,
    volatile: bool,
) -> list[dict]:
    count = 260
    start = datetime(2026, 7, 14, tzinfo=timezone.utc) - timedelta(days=count - 1)
    rows = []
    close = 100.0
    for index in range(count):
        opened = 100.0
        move = (2.0 if index % 2 else -2.0) if volatile else 0.05
        if index == count - 2:
            move = previous_return
        if index == count - 1:
            move = last_return
        close = opened * (1 + move / 100.0)
        rows.append({
            "symbol": symbol,
            "timestamp": (start + timedelta(days=index)).isoformat(),
            "open": opened,
            "high": max(opened, close) * 1.002,
            "low": min(opened, close) * 0.998,
            "close": close,
            "volume": base_volume * (spike if index == count - 1 else 1.0),
            "is_closed": True,
        })
    return rows


def minute_rows(start: float, end: float) -> list[dict]:
    rows = []
    for index in range(60):
        close = start + (end - start) * index / 59
        rows.append({
            "timestamp": f"2026-07-14T{19 + index // 60:02d}:{index % 60:02d}:00Z",
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 100_000,
        })
    return rows
