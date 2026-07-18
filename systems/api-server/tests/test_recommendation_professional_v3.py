from __future__ import annotations

import json
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
    evidence_reliability_components,
    rank_evidence_candidates,
    _rsi14,
)
from app.recommendations.score_profiles import system_score_profile  # noqa: E402
from app.recommendations.professional import completed_daily  # noqa: E402
from app.recommendations.explanations import compose_explanations, deterministic_explanation  # noqa: E402
from app.recommendations.narrative_context import build_narrative_context  # noqa: E402
from app.recommendations.repository import (  # noqa: E402
    InMemoryRecommendationRepository,
    InvestmentProfileUpsert,
)
from app.recommendations.service import RecommendationService  # noqa: E402


NOW = datetime(2026, 7, 16, 16, 0, tzinfo=timezone.utc)


def test_rsi14_uses_the_latest_fourteen_daily_changes() -> None:
    rising = [float(value) for value in range(100, 115)]
    flat = [100.0] * 15

    assert _rsi14(rising) == 100.0
    assert _rsi14(flat) == 50.0
    assert _rsi14(rising[:14]) is None


def test_evidence_reliability_confirmation_measures_present_corroboration_not_bullishness() -> None:
    raw = {
        "currentSessionRelativeStrength": -2,
        "last60MinuteRelativeStrength": -1,
        "clockAdjustedVolumeRatio": 0.8,
        "abnormalDollarVolume": -0.2,
        "confirmedBreakoutSupport": 0,
        "vwapHoldQuality": -0.5,
        "medianDollarVolume": 100_000_000,
        "quotedSpreadBps": 5,
        "freshnessScore": 100,
        "sourceQuality": 90,
    }
    weak_blocks = {key: 20.0 for key in BLOCK_KEYS}

    components = evidence_reliability_components(raw, {}, weak_blocks)

    assert components["confirmation"] == 100


def test_completed_daily_includes_explicitly_closed_same_day_only_after_market_close() -> None:
    row = {
        "timestamp": "2026-07-14T04:00:00Z",
        "close": 100,
        "isClosed": True,
    }
    before_close = datetime.fromisoformat("2026-07-14T15:59:00-04:00")
    at_close = datetime.fromisoformat("2026-07-14T16:00:00-04:00")

    assert completed_daily([row], before_close) == []
    assert completed_daily([row], at_close) == [row]
    assert completed_daily([{**row, "isClosed": False}], at_close) == []


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
    assert "insufficient_previous_session_candles" in reasons
    assert "insufficient_spy_previous_session_candles" in reasons
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
    score_profile = system_score_profile("balanced", "balanced")
    score_profile["blockWeights"] = preferred

    result = rank_evidence_candidates(
        candidates,
        profile=profile,
        score_profile=score_profile,
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
        score_profile=score_profile,
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
        + metrics["portfolioContribution"],
        4,
    )


def test_saved_score_profile_changes_rank_without_bypassing_hard_gate() -> None:
    profile = SimpleNamespace(
        risk_level="balanced",
        recommendation_style="balanced",
        excluded_symbols=(),
        excluded_sectors=(),
    )
    trend_factors = _factor_fixture(trend=95, quality=5)
    quality_factors = _factor_fixture(trend=5, quality=95)
    candidates = [
        _rank_candidate("TREND", trend_factors),
        _rank_candidate("QUALITY", quality_factors),
        {**_rank_candidate("BLOCKED", trend_factors), "rejectionReasons": ["halted"]},
    ]
    trend_profile = system_score_profile("balanced", "balanced")
    trend_profile["blockWeights"] = dict.fromkeys(BLOCK_KEYS, 0.0)
    trend_profile["blockWeights"]["trendStrength"] = 100.0
    quality_profile = system_score_profile("balanced", "balanced")
    quality_profile["blockWeights"] = dict.fromkeys(BLOCK_KEYS, 0.0)
    quality_profile["blockWeights"]["qualityStability"] = 100.0
    common = dict(
        profile=profile,
        watchlist_symbols=[],
        portfolio_positions=[],
        portfolio_snapshot=None,
        position_daily_candles={},
        active_symbol=None,
        now=NOW,
        snapshot_id=11,
    )

    trend_rank = rank_evidence_candidates(candidates, score_profile=trend_profile, **common)
    quality_rank = rank_evidence_candidates(candidates, score_profile=quality_profile, **common)

    assert [row["symbol"] for row in trend_rank.items] == ["TREND", "QUALITY"]
    assert [row["symbol"] for row in quality_rank.items] == ["QUALITY", "TREND"]
    assert "BLOCKED" not in {row["symbol"] for row in trend_rank.items + quality_rank.items}
    assert trend_rank.items[0]["metricsSnapshot"]["canonicalRankScore"] != trend_rank.items[0]["score"]


def _factor_fixture(*, trend: float, quality: float) -> dict[str, float]:
    factors = {
        "currentSessionRelativeStrength": trend,
        "last60MinuteRelativeStrength": trend,
        "oneDayRelativeStrength": trend,
        "fiveDayRelativeStrength": trend,
        "high52WeekProximity": trend,
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
    }
    factors.update({
        key: quality for key in (
            "realizedVolatility", "downsideVolatility", "valueQuality",
            "companyQuality", "growthQuality", "earningsRevisionQuality",
        )
    })
    return factors


def _rank_candidate(symbol: str, factors: dict[str, float]) -> dict:
    raw = {
        "medianDollarVolume": 500_000_000,
        "quotedSpreadBps": 2,
        "realizedVolatility": 0.02,
        "downsideVolatility": 0.01,
        "valueQuality": 70,
        "companyQuality": 70,
        "growthQuality": 70,
        "earningsRevisionQuality": 70,
        "catalystAvailable": True,
    }
    return {
        "symbol": symbol,
        "sector": "Information Technology",
        "normalizedFactors": factors,
        "rawFactors": raw,
        "blockScores": block_scores(factors, raw),
        "availableBlocks": list(BLOCK_KEYS),
        "baseSetupScore": 50,
        "evidenceReliability": 95,
        "reliabilityComponents": {},
        "rejectionReasons": [],
        "dailyReturns60": [],
        "evaluatedAt": NOW,
        "inputDigest": symbol,
    }


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
                for change, symbol in enumerate(
                    ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH",
                     "III", "JJJ", "KKK", "LLL", "MMM", "NNN", "OOO", "PPP"),
                    start=1,
                )
            ]

        def candles(self, symbol, now):
            start = now - timedelta(minutes=179)
            base = 500 if symbol == "SPY" else 100
            strength = 0.08 + (sum(ord(value) for value in symbol) % 10) / 100
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
            rows = self.candles(symbol, now - timedelta(days=1))
            first = rows[0]
            start = datetime.fromisoformat(first["timestamp"]) - timedelta(minutes=210)
            return [
                {
                    **first,
                    "timestamp": (start + timedelta(minutes=index)).isoformat(),
                    "close": float(first["close"]) + index * 0.01,
                    "volume": 10_000 + index * 100,
                }
                for index in range(390)
            ]

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

    assert first["summary"]["universeCount"] == 16
    assert first["summary"]["candidateCount"] == 16
    assert len(repository.evidence_snapshots) == 1
    assert all(item["symbol"] != "AAA" for item in first["items"])
    assert all(item["confidence"] >= 0.70 for item in first["items"])
    assert len(first["items"]) == 15
    assert all(item["explanation"]["version"] == "recommendation-explanation.v1" for item in first["items"])
    assert replay["idempotentReplay"] is True


def test_deterministic_explanation_describes_support_limit_and_data_quality() -> None:
    item = {
        "symbol": "AAA", "rank": 1, "score": 71, "confidence": 0.82,
        "metricsSnapshot": {
            "algorithmVersion": "deterministic-evidence-v3",
            "ruleSetVersion": "deterministic-evidence-v3.1",
            "blockScores": {key: score for key, score in zip(BLOCK_KEYS, (82, 74, 66, 50, 38, 61), strict=True)},
            "blockContributions": {key: value for key, value in zip(BLOCK_KEYS, (20.5, 14.8, 9.9, 5, 5.7, 9.15), strict=True)},
            "softPenalties": {"weakConfirmation": 6},
            "missingOptionalFactors": ["growthQuality"],
            "evidenceReliability": 82,
            "cutoff": NOW.isoformat(), "evidenceSnapshotId": 7, "inputDigest": "abc",
        },
    }
    explanation = deterministic_explanation(item)
    assert "뒷받침" in explanation["deterministic"]["summary"]
    assert "제한" in explanation["deterministic"]["summary"]
    assert "수익 성공 확률이 아니라" in explanation["deterministic"]["dataQuality"]["sentence"]
    assert explanation["deterministic"]["dataQuality"]["missingFactors"] == ["growthQuality"]


def test_invalid_llm_narrative_falls_back_without_changing_rank(monkeypatch) -> None:
    monkeypatch.setenv("RECOMMENDATION_NARRATIVE_PROVIDER", "openai")
    monkeypatch.setenv("RECOMMENDATION_NARRATIVE_MODEL", "test-model")
    item = {
        "symbol": "AAA", "rank": 3, "score": 71, "confidence": 0.82,
        "metricsSnapshot": {
            "algorithmVersion": "deterministic-evidence-v3", "ruleSetVersion": "deterministic-evidence-v3.1",
            "blockScores": {key: 65 for key in BLOCK_KEYS},
            "blockContributions": {key: 10 for key in BLOCK_KEYS},
            "softPenalties": {}, "missingOptionalFactors": [], "evidenceReliability": 82,
            "cutoff": NOW.isoformat(), "evidenceSnapshotId": 7, "inputDigest": "abc",
        },
    }
    provider = lambda _request: {"output_text": json.dumps({"narratives": [{
        "symbol": "AAA", "headline": "새로운 전망", "body": "성공 확률은 99%입니다. 지금 매수하세요."
    }]}, ensure_ascii=False)}
    result = compose_explanations([item], provider=provider)
    assert result[0]["rank"] == 3
    assert result[0]["score"] == 71
    assert result[0]["explanation"]["primary"]["source"] == "deterministic"


def test_narrative_context_rejects_future_profile_and_news() -> None:
    context = build_narrative_context(
        symbol="AAA",
        market_item={"symbol": "AAA", "name": "Alpha", "sector": "Technology", "industry": "Software"},
        company_profile={
            "companyName": "Alpha",
            "filingDate": "2026-07-17",
            "businessModel": "미래 filing",
            "revenueDrivers": ["미래 근거"],
        },
        news=[
            {"id": "future", "headline": "미래 뉴스", "publishedAt": "2026-07-17T00:00:00Z"},
            {"id": "late", "headline": "늦게 수신된 뉴스", "publishedAt": "2026-07-16T14:00:00Z", "receivedAt": "2026-07-17T00:00:00Z"},
            {"id": "past", "headline": "기준시각 이전 뉴스", "publishedAt": "2026-07-16T15:00:00Z"},
        ],
        raw_factors={"companyQuality": 72},
        cutoff=NOW,
    )
    assert context["status"] == "partial"
    assert context["tenK"] is None
    assert [row["id"] for row in context["catalysts"]] == ["past"]
    assert context["fundamentals"] == {"companyQuality": 72.0}


def test_llm_atoms_are_validated_per_item_and_keep_unique_company_copy(monkeypatch) -> None:
    monkeypatch.setenv("RECOMMENDATION_NARRATIVE_PROVIDER", "openai")
    monkeypatch.setenv("RECOMMENDATION_NARRATIVE_MODEL", "test-model")

    def item(symbol: str, company_name: str) -> dict:
        return {
            "symbol": symbol,
            "rank": 1 if symbol == "AAA" else 2,
            "score": 71,
            "confidence": 0.82,
            "narrativeContext": {
                "version": "recommendation-narrative-context.v1",
                "status": "partial",
                "digest": symbol.lower(),
                "company": {"symbol": symbol, "companyName": company_name, "industry": "Software"},
                "tenK": None,
                "catalysts": [],
                "fundamentals": {},
            },
            "metricsSnapshot": {
                "algorithmVersion": "deterministic-evidence-v3",
                "ruleSetVersion": "deterministic-evidence-v3.1",
                "blockScores": {key: 65 for key in BLOCK_KEYS},
                "blockContributions": {key: 10 for key in BLOCK_KEYS},
                "softPenalties": {},
                "missingOptionalFactors": [],
                "evidenceReliability": 82,
                "cutoff": NOW.isoformat(),
                "evidenceSnapshotId": 7,
                "inputDigest": symbol.lower(),
            },
        }

    provider = lambda _request: {"output_text": json.dumps({"narratives": [
        {
            "symbol": "AAA", "action": "watch",
            "headlineBase": "AAA 알파의 소프트웨어 사업과 추세 강도",
            "businessSentence": "알파는 소프트웨어 사업을 운영하는 기업입니다.",
            "setupSentence": "이번 관찰에서는 추세 강도가 가장 두드러졌습니다.",
            "companyRiskSentence": "소프트웨어 업종의 경쟁 위험은 별도로 확인해야 합니다.",
            "companyRefs": ["company.industry"], "evidenceRefs": ["trend_strength"],
        },
        {
            "symbol": "BBB", "action": "buy",
            "headlineBase": "잘못된 action", "businessSentence": "베타는 기업입니다.",
            "setupSentence": "근거가 있습니다.", "companyRiskSentence": "위험이 있습니다.",
            "companyRefs": ["company.industry"], "evidenceRefs": ["trend_strength"],
        },
    ]}, ensure_ascii=False)}
    result = compose_explanations([item("AAA", "알파"), item("BBB", "베타")], provider=provider)

    assert result[0]["explanation"]["primary"]["source"] == "llm"
    assert result[1]["explanation"]["primary"]["source"] == "deterministic"
    assert result[0]["rank"] == 1 and result[1]["rank"] == 2
    assert result[0]["explanation"]["primary"]["headline"] != result[1]["explanation"]["primary"]["headline"]
    assert result[0]["explanation"]["primary"]["listSummary"] != result[1]["explanation"]["primary"]["listSummary"]
