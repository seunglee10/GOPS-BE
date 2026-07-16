from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.recommendations.professional_v3 import (  # noqa: E402
    BLOCK_KEYS,
    EvidenceContext,
    RELIABILITY_MINIMUM,
    average_rank_percentiles,
    base_rejection_reasons,
    block_scores,
    catalyst_quality,
    process_evidence_preference_events,
    rank_evidence_candidates,
)
from app.recommendations.repository import (  # noqa: E402
    InMemoryRecommendationRepository,
    InvestmentProfileUpsert,
)
from app.recommendations.service import RecommendationService  # noqa: E402


NOW = datetime(2026, 7, 16, 16, 0, tzinfo=timezone.utc)


def test_average_rank_percentiles_use_average_ties_and_inverse_ordering() -> None:
    rows = [
        {"symbol": "AAA", "rawFactors": {"factor": 1}},
        {"symbol": "BBB", "rawFactors": {"factor": 1}},
        {"symbol": "CCC", "rawFactors": {"factor": 3}},
    ]

    normal = average_rank_percentiles(rows, "factor")
    inverse = average_rank_percentiles(rows, "factor", inverse=True)

    assert normal["AAA"] == normal["BBB"]
    assert normal["CCC"] == 100
    assert inverse["CCC"] == 0
    assert inverse["AAA"] == inverse["BBB"]


def test_missing_news_is_neutral_and_negative_or_irrelevant_news_never_adds_bonus() -> None:
    missing, available = catalyst_quality([], NOW)
    negative, _ = catalyst_quality([{
        "articleId": "negative",
        "availableAt": NOW - timedelta(minutes=10),
        "impactDirection": "negative",
        "relevance": 1,
        "novelty": 1,
        "sourceQuality": 1,
    }], NOW)
    irrelevant, _ = catalyst_quality([{
        "articleId": "irrelevant",
        "availableAt": NOW - timedelta(minutes=10),
        "impactDirection": "positive",
        "relevance": 0,
        "novelty": 1,
        "sourceQuality": 1,
    }], NOW)

    assert (missing, available) == (50.0, False)
    assert negative < 50
    assert irrelevant == 50


def test_beneficial_factor_improvement_cannot_reduce_its_block() -> None:
    factors = {
        "currentSessionRelativeStrength": 50,
        "last60MinuteRelativeStrength": 50,
        "oneDayRelativeStrength": 50,
        "fiveDayRelativeStrength": 50,
        "high52WeekProximity": 50,
        "clockAdjustedVolumeRatio": 50,
        "abnormalDollarVolume": 50,
        "closingLocationValue": 50,
        "participationPersistence": 50,
        "confirmedBreakoutSupport": 50,
        "vwapHoldQuality": 50,
        "higherLowQuality": 50,
        "gapAcceptance": 50,
        "catalystQuality": 50,
        "medianDollarVolume": 50,
        "quotedSpreadBps": 50,
        "freshnessScore": 50,
        "realizedVolatility": 50,
        "downsideVolatility": 50,
        "valueQuality": 50,
        "companyQuality": 50,
        "growthQuality": 50,
        "earningsRevisionQuality": 50,
    }
    baseline = block_scores(factors, {})
    improved = block_scores({**factors, "currentSessionRelativeStrength": 80}, {})

    assert improved["trendStrength"] > baseline["trendStrength"]
    assert all(improved[key] == baseline[key] for key in BLOCK_KEYS if key != "trendStrength")


def test_missing_critical_market_evidence_rejects_before_scoring() -> None:
    context = EvidenceContext(
        session_mode="regular",
        now=NOW,
        market_items=[],
        candles_by_symbol={},
        daily_candles_by_symbol={},
        previous_session_candles_by_symbol={},
        news_by_symbol={},
        fundamentals_by_symbol={},
        fundamental_provenance={"status": "unavailable"},
    )

    reasons = base_rejection_reasons(
        "AAA", {"symbol": "AAA", "tradable": True}, context, [], []
    )

    assert "insufficient_session_candles" in reasons
    assert "insufficient_spy_session_candles" in reasons
    assert "insufficient_daily_history" in reasons
    assert "insufficient_spy_daily_history" in reasons


def test_rejected_and_low_reliability_candidates_cannot_be_recovered_by_preference() -> None:
    profile = SimpleNamespace(
        risk_level="balanced",
        recommendation_style="balanced",
        excluded_symbols=set(),
        excluded_sectors=set(),
    )
    preferred = {key: 100.0 for key in BLOCK_KEYS}
    base = {
        "sector": "Information Technology",
        "industry": "Software",
        "changePercent": 1,
        "rawFactors": {"medianDollarVolume": 50_000_000, "quotedSpreadBps": 5},
        "normalizedFactors": {},
        "blockScores": preferred,
        "baseSetupScore": 100,
        "reliabilityComponents": {},
        "dailyReturns60": [],
        "evaluatedAt": NOW,
        "inputDigest": "fixed",
    }
    candidates = [
        {**base, "symbol": "REJECT", "evidenceReliability": 100, "rejectionReasons": ["halted"]},
        {**base, "symbol": "LOW", "evidenceReliability": RELIABILITY_MINIMUM - 0.01, "rejectionReasons": []},
        {**base, "symbol": "OK", "evidenceReliability": 90, "rejectionReasons": []},
    ]
    state = {
        "effectiveWeights": preferred,
        "preferenceConfidence": 1,
    }

    result = rank_evidence_candidates(
        candidates,
        profile=profile,
        preference_state=state,
        risk_state={},
        watchlist_symbols=[],
        portfolio_positions=[],
        portfolio_snapshot=None,
        position_daily_candles={},
        active_symbol=None,
        now=NOW,
        snapshot_id=1,
    )
    replay = rank_evidence_candidates(
        candidates,
        profile=profile,
        preference_state=state,
        risk_state={},
        watchlist_symbols=[],
        portfolio_positions=[],
        portfolio_snapshot=None,
        position_daily_candles={},
        active_symbol=None,
        now=NOW,
        snapshot_id=1,
    )

    assert [item["symbol"] for item in result.items] == ["OK"]
    assert replay == result
    assert result.rejected_by_reason["halted"] == 1
    assert result.rejected_by_reason["evidence_reliability"] == 1
    metrics = result.items[0]["metricsSnapshot"]
    assert result.items[0]["score"] == round(
        metrics["adjustedSetupContribution"]
        + metrics["preferenceContribution"]
        + metrics["portfolioContribution"],
        4,
    )


def test_v2_preference_logits_migrate_to_blocks_and_new_blocks_start_neutral() -> None:
    previous = {
        "longTermLogits": {"oneDayRelativeStrength": 0.8, "newsImpact": -0.4},
        "sessionLogits": {"oneDayRelativeStrength": 0.2},
        "longSampleCount": 2,
        "sessionSampleCount": 1,
        "asOf": NOW,
    }

    state, events = process_evidence_preference_events(
        previous, [], style="balanced", cutoff=NOW
    )

    assert events == []
    assert state["longTermLogits"]["trendStrength"] == 0.8
    assert state["longTermLogits"]["catalystQuality"] == -0.4
    assert state["longTermLogits"]["priceStructure"] == 0


def test_service_builds_one_shared_full_universe_snapshot(monkeypatch) -> None:
    monkeypatch.setenv("RECOMMENDATION_ALGORITHM_VERSION", "deterministic-evidence-v3")
    repository = InMemoryRecommendationRepository()
    repository.upsert_profile(InvestmentProfileUpsert(
        user_sub="user-1",
        risk_level="balanced",
        recommendation_style="balanced",
        horizon="intraday",
        max_drawdown_pct=6,
        preferred_sectors=[],
        excluded_sectors=[],
        excluded_symbols=[],
    ))

    class DataSource:
        def watchlist_symbols(self, _user_sub):
            return ["AAA"]

        def portfolio_positions(self, _user_sub):
            return []

        def market_items(self):
            return [
                {
                    "symbol": symbol,
                    "sector": "Technology",
                    "industry": "Software",
                    "changePercent": change,
                    "sessionDollarVolume": 100_000_000,
                    "quotedSpreadBps": 5,
                    "tradable": True,
                    "priceSource": "canonical",
                }
                for symbol, change in (("AAA", 3), ("BBB", 2), ("CCC", 1))
            ]

        def candles(self, symbol, now):
            start = now - timedelta(minutes=179)
            base = 500 if symbol == "SPY" else 100
            strength = 0.2 if symbol == "AAA" else 0.15 if symbol == "BBB" else 0.1
            return [
                {
                    "timestamp": (start + timedelta(minutes=index)).isoformat(),
                    "open": base + strength * index - 0.02,
                    "high": base + strength * index + 0.1,
                    "low": base + strength * index - 0.1,
                    "close": base + strength * index,
                    "volume": 10_000 + index * 100,
                    "sourceClass": "canonical",
                }
                for index in range(180)
            ]

        def daily_candles(self, symbol, now):
            base = 500 if symbol == "SPY" else 100
            rows = []
            close = base
            for index in range(260):
                close *= 1 + (0.002 if index % 3 else -0.001)
                rows.append({
                    "timestamp": (now - timedelta(days=260 - index)).isoformat(),
                    "open": close * 0.999,
                    "high": close * 1.003,
                    "low": close * 0.997,
                    "close": close,
                    "volume": 5_000_000 + index * 1_000,
                    "is_closed": True,
                    "sourceClass": "canonical",
                })
            return rows

        def previous_session_candles(self, symbol, now):
            return self.candles(symbol, now - timedelta(days=1))

        def news_for_symbols(self, symbols, _now):
            return {symbol: [] for symbol in symbols}

    class FundamentalProvider:
        def snapshots_as_of(self, symbols, cutoff):
            return {
                "snapshotId": "fundamental-v3",
                "schemaVersion": "fundamentals.v1",
                "featureVersion": "features.v1",
                "digest": "fixed",
                "sourceAsOf": (cutoff - timedelta(minutes=1)).isoformat(),
                "snapshots": {
                    symbol: {
                        "value": 70,
                        "quality": 80,
                        "growth": 60,
                        "earningsRevision": 50,
                        "coverage": 1,
                        "freshness": 1,
                        "sourceQuality": 1,
                    }
                    for symbol in symbols
                },
            }

    app = SimpleNamespace(state=SimpleNamespace(
        recommendation_fundamental_provider=FundamentalProvider()
    ))
    service = RecommendationService(repository=repository, data_source=DataSource(), app=app)

    first = service.refresh("user-1", now=NOW)
    replay = service.refresh("user-1", now=NOW)

    assert first["summary"]["universeCount"] == 3
    assert first["summary"]["candidateCount"] == 3
    assert len(repository.evidence_snapshots) == 1
    assert all(item["symbol"] != "AAA" for item in first["items"])
    assert all(item["confidence"] >= 0.70 for item in first["items"])
    assert replay["idempotentReplay"] is True
