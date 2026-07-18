from __future__ import annotations

import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
ORDER_SHARED = ROOT / "systems" / "order" / "shared"
AGENT_SHARED = ROOT / "systems" / "agent-orchestration" / "shared"
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend"
for path in (str(MARKET_SHARED), str(ORDER_SHARED), str(AGENT_SHARED), str(BACKEND), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

sys.modules.setdefault(
    "redis",
    types.SimpleNamespace(
        from_url=lambda *args, **kwargs: None,
        exceptions=types.SimpleNamespace(TimeoutError=TimeoutError),
    ),
)

try:
    from fastapi.testclient import TestClient

    from app.alerts.notifications import InMemoryNotificationBroker
    from app.alerts.repository import InMemoryAlertRepository
    from app.main import create_app
    from app.recommendations.repository import InMemoryRecommendationRepository, RecommendationRunCreate, RecommendationSchemaUnavailable, _json_ready
    from app.recommendations.service import RecommendationDataSource
    from app.recommendations.worker import RecommendationWorker
except Exception as exc:  # pragma: no cover - dependency guard for lean envs
    pytest.skip(f"FastAPI recommendation route tests are unavailable: {exc}", allow_module_level=True)


REGULAR_MARKET_TIME = datetime(2026, 7, 7, 16, 0, tzinfo=timezone.utc)
PRE_MARKET_TIME = datetime(2026, 7, 7, 13, 15, tzinfo=timezone.utc)
MARKET_CLOSED_TIME = datetime(2026, 7, 7, 22, 0, tzinfo=timezone.utc)


@pytest.fixture
def recommendation_app(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_HOST", raising=False)
    app = create_app()
    app.state.recommendation_repository = InMemoryRecommendationRepository()
    app.state.alert_repository = InMemoryAlertRepository()
    app.state.alert_notification_broker = InMemoryNotificationBroker()
    app.state.recommendation_now_provider = lambda: REGULAR_MARKET_TIME
    app.state.recommendation_watchlist_provider = lambda user_sub: ["AAPL"]
    app.state.recommendation_portfolio_provider = lambda user_sub: []
    app.state.recommendation_market_provider = lambda: [
        {
            "symbol": "AAPL",
            "sector": "Technology",
            "industry": "Consumer Electronics",
            "sessionDollarVolume": 250_000_000,
            "changePercent": 4.1,
            "lastPrice": 104,
        },
        {
            "symbol": "MSFT",
            "sector": "Technology",
            "industry": "Software",
            "sessionDollarVolume": 120_000_000,
            "changePercent": 3.2,
            "lastPrice": 401,
        },
        {
            "symbol": "AVGO",
            "sector": "Technology",
            "industry": "Semiconductors",
            "sessionDollarVolume": 140_000_000,
            "changePercent": 2.8,
            "lastPrice": 1500,
        },
    ]
    app.state.recommendation_candles_provider = fake_candles
    app.state.recommendation_news_provider = lambda symbol, now=None: [
        {"sentiment": "positive", "publishedAt": (now or REGULAR_MARKET_TIME).isoformat()}
    ] if symbol in {"MSFT", "AVGO"} else []
    return app


def test_recommendations_require_profile(recommendation_app) -> None:
    response = TestClient(recommendation_app).get("/api/recommendations/stocks/latest")

    assert response.status_code == 200
    assert response.json() == {"status": "profile_required", "items": []}


def test_portfolio_snapshot_history_keeps_changes_and_ignores_poll_observation_timestamps() -> None:
    repository = InMemoryRecommendationRepository()
    first_payload = {
        "asOf": "2026-07-14T01:00:00Z",
        "positions": [{"symbol": "NVDA", "quantity": "10"}],
        "cash": "1000",
    }

    first = repository.upsert_portfolio_snapshot("user-1", first_payload)
    replay_payload = {**first_payload, "asOf": "2026-07-14T01:01:00Z"}
    replay = repository.upsert_portfolio_snapshot("user-1", replay_payload)

    assert replay["payload"] == replay_payload
    assert replay["updated_at"] >= first["updated_at"]
    assert len(repository.portfolio_snapshot_history) == 1

    changed_payload = {
        **first_payload,
        "positions": [{"symbol": "NVDA", "quantity": "12"}],
        "cash": "600",
    }
    changed = repository.upsert_portfolio_snapshot("user-1", changed_payload)

    assert changed["payload"] == changed_payload
    assert len(repository.portfolio_snapshot_history) == 2
    assert [row["payload"] for row in repository.portfolio_snapshot_history] == [
        first_payload,
        changed_payload,
    ]


def test_profile_crud_and_intraday_refresh_returns_new_buy_recommendation(recommendation_app) -> None:
    client = TestClient(recommendation_app)
    profile = client.put(
        "/api/recommendations/profile",
        json={
            "riskLevel": "balanced",
            "horizon": "intraday",
            "maxDrawdownPct": 6,
            "preferredSectors": ["Technology"],
            "excludedSectors": [],
            "excludedSymbols": [],
        },
    )
    refresh = client.post("/api/recommendations/stocks/refresh", json={"activeSymbol": "AAPL"})

    assert profile.status_code == 200
    assert profile.json()["profile"]["riskLevel"] == "balanced"
    assert profile.json()["profile"]["recommendationStyle"] == "balanced"
    assert profile.json()["profile"]["preferredSectors"] == ["Information Technology"]
    assert refresh.status_code == 200
    payload = refresh.json()
    assert payload["status"] == "completed"
    assert payload["items"][0]["symbol"] != "AAPL"
    assert payload["items"][0]["symbol"] in {"MSFT", "AVGO"}
    assert payload["items"][0]["action"] == "buy"
    assert payload["items"][0]["sector"] == "Information Technology"
    assert payload["items"][0]["sectorLabelKo"] == "정보기술"
    assert payload["items"][0]["score"] >= 75
    assert len(payload["items"][0]["reasons"]) >= 2
    assert payload["summary"]["excludedWatchlistCount"] == 1


def test_named_score_profile_crud_validation_and_active_fallback(recommendation_app) -> None:
    client = TestClient(recommendation_app)
    client.put(
        "/api/recommendations/profile",
        json={"riskLevel": "balanced", "recommendationStyle": "balanced", "horizon": "intraday"},
    )
    catalog = client.get("/api/recommendations/score-profiles").json()
    assert [row["name"] for row in catalog["presets"]] == ["모멘텀", "균형", "안정"]
    assert catalog["active"]["name"] == "균형"
    balanced = catalog["presets"][1]
    body = {
        "name": "My Balance",
        "blockWeights": balanced["blockWeights"],
        "factorWeights": balanced["factorWeights"],
        "portfolioWeight": balanced["portfolioWeight"],
        "portfolioFactorWeights": balanced["portfolioFactorWeights"],
    }

    created_response = client.post("/api/recommendations/score-profiles", json=body)
    assert created_response.status_code == 200
    created = created_response.json()["profile"]
    assert created["revision"] == 1
    assert client.post("/api/recommendations/score-profiles", json={**body, "name": "my balance"}).status_code == 409

    invalid = {**body, "name": "잘못된 합계", "blockWeights": {**body["blockWeights"], "trendStrength": 99}}
    assert client.post("/api/recommendations/score-profiles", json=invalid).status_code == 422
    unknown = {**body, "name": "알 수 없는 키", "blockWeights": {**body["blockWeights"], "unknown": 0}}
    assert client.post("/api/recommendations/score-profiles", json=unknown).status_code == 422
    for name, value in (("음수", -1), ("초과", 101), ("NaN", "NaN"), ("소수 초과", 25.001)):
        bad_number = {
            **body,
            "name": name,
            "blockWeights": {**body["blockWeights"], "trendStrength": value},
        }
        assert client.post("/api/recommendations/score-profiles", json=bad_number).status_code == 422

    activated = client.put(
        "/api/recommendations/score-profiles/active",
        json={"type": "custom", "profileId": created["id"]},
    )
    assert activated.status_code == 200
    assert activated.json()["profile"]["activeScoreProfile"]["name"] == "My Balance"
    active_revision = activated.json()["profile"]["profileRevision"]
    repeated_activation = client.put(
        "/api/recommendations/score-profiles/active",
        json={"type": "custom", "profileId": created["id"]},
    ).json()["profile"]
    assert repeated_activation["profileRevision"] == active_revision

    unchanged = client.put(
        f"/api/recommendations/score-profiles/{created['id']}",
        json=body,
    ).json()["profile"]
    assert unchanged["revision"] == 1

    updated = client.put(
        f"/api/recommendations/score-profiles/{created['id']}",
        json={**body, "name": "내 안정"},
    ).json()["profile"]
    assert updated["revision"] == 2
    repository = recommendation_app.state.recommendation_repository
    assert repository.delete_score_profile("another-user", created["id"]) is False
    assert repository.activate_score_profile("another-user", created["id"], preset_style="balanced") is None
    assert client.delete(f"/api/recommendations/score-profiles/{created['id']}").status_code == 200
    after_delete = client.get("/api/recommendations/score-profiles").json()
    assert after_delete["active"]["name"] == "균형"

    for index in range(20):
        response = client.post(
            "/api/recommendations/score-profiles",
            json={**body, "name": f"프로필 {index + 1}"},
        )
        assert response.status_code == 200
    assert client.post(
        "/api/recommendations/score-profiles",
        json={**body, "name": "프로필 21"},
    ).status_code == 409


def test_score_profile_prompt_rag_proposes_valid_unsaved_profile_with_snapshot_provenance(
    recommendation_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = TestClient(recommendation_app)
    client.put(
        "/api/recommendations/profile",
        json={"riskLevel": "balanced", "recommendationStyle": "balanced", "horizon": "intraday"},
    )
    repository = recommendation_app.state.recommendation_repository
    snapshot = repository.create_evidence_snapshot({
        "snapshotKey": "suggestion-snapshot",
        "slotStart": REGULAR_MARKET_TIME.isoformat(),
        "marketDate": "2026-07-07",
        "sessionMode": "regular",
        "cutoff": REGULAR_MARKET_TIME.isoformat(),
        "universe": ["AAPL", "MSFT", "AVGO"],
        "ruleSetVersion": "deterministic-evidence-v3.1",
        "sourceDigests": {"news": "news-digest"},
        "sourceStatus": {"news": "ready", "market": "ready"},
        "status": "completed",
        "inputDigest": "snapshot-digest",
    }, [{
        "symbol": "MSFT",
        "sector": "Information Technology",
        "industry": "Software",
        "changePercent": 3.2,
        "rawFactors": {"catalystQuality": 80},
        "normalizedFactors": {},
        "blockScores": {"trendStrength": 82, "catalystQuality": 76},
        "baseSetupScore": 79,
        "evidenceReliability": 84,
        "reliabilityComponents": {},
        "rejectionReasons": [],
        "dailyReturns60": [],
        "marketItem": {"symbol": "MSFT"},
        "narrativeContext": {"catalysts": [{"headline": "Cloud guidance raised", "publishedAt": REGULAR_MARKET_TIME.isoformat()}]},
        "inputDigest": "candidate-digest",
    }])
    repository.create_or_replace_run(RecommendationRunCreate(
        user_sub="dev-auth-disabled",
        run_key="suggestion-run",
        slot_start=REGULAR_MARKET_TIME.isoformat(),
        market_date="2026-07-07",
        status="completed",
        profile_snapshot={},
        market_snapshot_time=REGULAR_MARKET_TIME.isoformat(),
        summary={},
        evidence_snapshot_id=snapshot["id"],
    ), [{
        "symbol": "MSFT", "rank": 1, "score": 84, "customRankScore": 84,
        "confidence": 0.84, "sector": "Information Technology", "metricsSnapshot": {"blockScores": {"trendStrength": 82}},
    }])

    response = client.post(
        "/api/recommendations/score-profiles/suggestions",
        json={"query": "거래대금이 강하고 실적 뉴스가 있는 종목을 우선해줘"},
    )

    assert response.status_code == 200
    suggestion = response.json()["suggestion"]
    assert suggestion["schemaVersion"] == "recommendation-score-suggestion.v1"
    assert suggestion["profile"]["type"] == "custom"
    assert suggestion["profile"]["id"] is None
    assert suggestion["provenance"]["evidenceSnapshotId"] == snapshot["id"]
    assert suggestion["provenance"]["source"] == "deterministic"
    assert "거래대금" in suggestion["intent"]["matchedKeywords"]
    assert sum(suggestion["profile"]["blockWeights"].values()) == pytest.approx(100)
    for weights in suggestion["profile"]["factorWeights"].values():
        assert sum(weights.values()) == pytest.approx(100)
    assert sum(suggestion["profile"]["portfolioFactorWeights"].values()) == pytest.approx(100)
    assert repository.list_score_profiles("dev-auth-disabled") == [], "a suggestion must not be saved before user confirmation"


def test_score_profile_prompt_uses_llm_structured_output_after_retrieval(recommendation_app) -> None:
    client = TestClient(recommendation_app)
    client.put(
        "/api/recommendations/profile",
        json={"riskLevel": "balanced", "recommendationStyle": "balanced", "horizon": "intraday"},
    )
    balanced = client.get("/api/recommendations/score-profiles").json()["presets"][1]
    captured: dict[str, object] = {}

    def provider(payload):
        captured.update(payload)
        return {
            "name": "뉴스 유동성 로직",
            "rationale": "최신 뉴스 촉매와 거래 참여를 함께 확인했습니다.",
            "confidence": 0.81,
            "evidenceRefs": [],
            "profile": {
                "blockWeights": balanced["blockWeights"],
                "factorWeights": balanced["factorWeights"],
                "portfolioWeight": balanced["portfolioWeight"],
                "portfolioFactorWeights": balanced["portfolioFactorWeights"],
            },
        }

    recommendation_app.state.recommendation_profile_suggestion_provider = provider
    response = client.post(
        "/api/recommendations/score-profiles/suggestions",
        json={"query": "뉴스와 거래량을 같이 보는 로직"},
    )

    assert response.status_code == 200
    suggestion = response.json()["suggestion"]
    assert suggestion["name"] == "뉴스 유동성 로직"
    assert suggestion["provenance"]["source"] == "llm"
    assert captured["text"]["format"]["type"] == "json_schema"
    context = json.loads(captured["input"])
    assert context["retrievedIntentDocuments"]
    assert "latestEvidence" in context
    assert client.post("/api/recommendations/score-profiles/suggestions", json={"query": "x"}).status_code == 422


def test_refresh_is_idempotent_within_same_market_slot(recommendation_app) -> None:
    client = TestClient(recommendation_app)
    client.put(
        "/api/recommendations/profile",
        json={"riskLevel": "aggressive", "horizon": "intraday", "maxDrawdownPct": 10},
    )

    first = client.post("/api/recommendations/stocks/refresh", json={}).json()
    second = client.post("/api/recommendations/stocks/refresh", json={}).json()

    assert first["runKey"] == second["runKey"]
    assert second["idempotentReplay"] is True


def test_professional_refresh_persists_versioned_scores_and_recomputes_when_style_changes(
    recommendation_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECOMMENDATION_PERSONALIZATION_ENABLED", "true")
    monkeypatch.setenv("RECOMMENDATION_PERSONALIZATION_SHADOW", "false")
    recommendation_app.state.recommendation_daily_candles_provider = professional_daily_candles
    recommendation_app.state.recommendation_previous_session_candles_provider = fake_candles
    client = TestClient(recommendation_app)
    client.put(
        "/api/recommendations/profile",
        json={
            "riskLevel": "balanced",
            "recommendationStyle": "momentum",
            "horizon": "intraday",
            "maxDrawdownPct": 6,
        },
    )

    first = client.post("/api/recommendations/stocks/refresh", json={}).json()
    client.put(
        "/api/recommendations/profile",
        json={
            "riskLevel": "balanced",
            "recommendationStyle": "stable",
            "horizon": "intraday",
            "maxDrawdownPct": 6,
        },
    )
    second = client.post("/api/recommendations/stocks/refresh", json={}).json()

    assert first["status"] == "completed"
    assert first["items"][0]["customRankScore"] == first["items"][0]["score"]
    assert "personalization" not in first["items"][0]["metricsSnapshot"]
    assert "personalScore" not in first["items"][0]["metricsSnapshot"]
    assert first["runKey"] != second["runKey"]
    assert second["idempotentReplay"] is False
    assert second["summary"]["scoring"]["recommendationStyle"] == "stable"
    stored = recommendation_app.state.recommendation_repository.latest_run("dev-auth-disabled")
    assert stored["weights_version"] == "professional-personalization-v1"
    assert stored["scoring_input_digest"]


def test_continuous_v2_runtime_option_is_removed(
    recommendation_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECOMMENDATION_ALGORITHM_VERSION", "continuous-v2")
    client = TestClient(recommendation_app)
    client.put(
        "/api/recommendations/profile",
        json={"riskLevel": "balanced", "recommendationStyle": "balanced", "horizon": "intraday"},
    )
    with pytest.raises(ValueError, match="must be legacy, professional-v1, or deterministic-evidence-v3"):
        client.post("/api/recommendations/stocks/refresh", json={})


def test_deterministic_evidence_v3_shares_full_universe_snapshot_and_returns_reliability(
    recommendation_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECOMMENDATION_ALGORITHM_VERSION", "deterministic-evidence-v3")
    recommendation_app.state.recommendation_daily_candles_provider = professional_daily_candles
    recommendation_app.state.recommendation_previous_session_candles_provider = professional_previous_session_candles
    recommendation_app.state.recommendation_benchmark_health_provider = lambda _now, _session: {
        "symbol": "SPY",
        "ready": True,
        "reasons": [],
    }
    market_symbols = (
        "AAPL", "MSFT", "AVGO", "NVDA", "AMZN", "META", "GOOGL", "GOOG",
        "TSLA", "LLY", "JPM", "WMT", "V", "ORCL", "MA", "NFLX",
    )
    recommendation_app.state.recommendation_market_provider = lambda: [
        {
            "symbol": symbol,
            "sector": "Technology",
            "industry": "Software",
            "sessionDollarVolume": 200_000_000,
            "changePercent": change,
            "lastPrice": 100 + change,
            "quotedSpreadBps": 5,
            "tradable": True,
            "priceSource": "canonical",
        }
        for index, symbol in enumerate(market_symbols)
        for change in (round(4.1 - index * 0.1, 1),)
    ]

    class FundamentalProvider:
        def snapshots_as_of(self, symbols, cutoff):
            return {
                "snapshotId": "fundamental-v3",
                "schemaVersion": "fundamentals.v1",
                "featureVersion": "features.v1",
                "digest": "fixture-v3-digest",
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

    recommendation_app.state.recommendation_fundamental_provider = FundamentalProvider()
    client = TestClient(recommendation_app)
    client.put(
        "/api/recommendations/profile",
        json={
            "riskLevel": "balanced",
            "recommendationStyle": "balanced",
            "horizon": "intraday",
            "maxDrawdownPct": 6,
        },
    )

    first = client.post("/api/recommendations/stocks/refresh", json={}).json()
    second = client.post("/api/recommendations/stocks/refresh", json={}).json()

    assert first["status"] == "completed", first["summary"]
    assert first["summary"]["scoring"]["algorithmVersion"] == "deterministic-evidence-v3"
    assert first["summary"]["ruleSetVersion"] == "deterministic-evidence-v3.1"
    assert first["summary"]["universeCount"] == 16
    assert first["summary"]["candidateCount"] == 16
    assert len(first["items"]) == 15
    assert all(item["confidence"] >= 0.70 for item in first["items"])
    assert all(
        item["metricsSnapshot"]["confidenceMeaning"]
        == "evidence_reliability_not_success_probability"
        for item in first["items"]
    )
    assert second["idempotentReplay"] is True
    repository = recommendation_app.state.recommendation_repository
    assert len(repository.evidence_snapshots) == 1
    stored = repository.latest_run("dev-auth-disabled")
    assert stored["algorithm_version"] == "deterministic-evidence-v3"
    assert stored["evidence_snapshot_id"] == first["summary"]["evidenceSnapshotId"]


def test_refresh_automatically_uses_the_active_market_session(recommendation_app) -> None:
    client = TestClient(recommendation_app)
    client.put(
        "/api/recommendations/profile",
        json={"riskLevel": "balanced", "horizon": "intraday", "maxDrawdownPct": 6},
    )

    recommendation_app.state.recommendation_now_provider = lambda: PRE_MARKET_TIME
    pre = client.post("/api/recommendations/stocks/refresh", json={}).json()
    recommendation_app.state.recommendation_now_provider = lambda: REGULAR_MARKET_TIME
    regular = client.post("/api/recommendations/stocks/refresh", json={}).json()

    assert pre["status"] == "completed"
    assert regular["status"] == "completed"
    assert ":pre:" in pre["runKey"]
    assert ":regular:" in regular["runKey"]
    assert pre["runKey"] != regular["runKey"]


def test_public_refresh_does_not_require_a_session_selector(recommendation_app) -> None:
    recommendation_app.state.recommendation_now_provider = lambda: PRE_MARKET_TIME
    client = TestClient(recommendation_app)
    client.put(
        "/api/recommendations/profile",
        json={"riskLevel": "balanced", "horizon": "intraday", "maxDrawdownPct": 6},
    )

    response = client.post("/api/recommendations/stocks/refresh", json={})

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert ":pre:" in response.json()["runKey"]


def test_profile_upsert_defaults_max_drawdown_when_omitted(recommendation_app) -> None:
    client = TestClient(recommendation_app)

    response = client.put(
        "/api/recommendations/profile",
        json={"riskLevel": "balanced", "horizon": "intraday"},
    )

    assert response.status_code == 200
    assert response.json()["profile"]["maxDrawdownPct"] == 6


def test_missing_recommendation_schema_returns_503(recommendation_app) -> None:
    class MissingSchemaRepository(InMemoryRecommendationRepository):
        def get_profile(self, user_sub: str):
            raise RecommendationSchemaUnavailable("recommendation database migration required")

    recommendation_app.state.recommendation_repository = MissingSchemaRepository()

    response = TestClient(recommendation_app).get("/api/recommendations/stocks/latest")

    assert response.status_code == 503
    assert response.json()["detail"] == "recommendation database migration required"


def test_market_closed_does_not_create_new_run(recommendation_app) -> None:
    recommendation_app.state.recommendation_now_provider = lambda: MARKET_CLOSED_TIME
    client = TestClient(recommendation_app)
    client.put(
        "/api/recommendations/profile",
        json={"riskLevel": "balanced", "horizon": "intraday", "maxDrawdownPct": 6},
    )

    response = client.post("/api/recommendations/stocks/refresh", json={})

    assert response.status_code == 200
    assert response.json()["status"] == "market_closed"
    assert recommendation_app.state.recommendation_repository.latest_run("dev-auth-disabled") is None


def test_excluded_symbol_is_not_recommended(recommendation_app) -> None:
    client = TestClient(recommendation_app)
    client.put(
        "/api/recommendations/profile",
        json={
            "riskLevel": "balanced",
            "horizon": "intraday",
            "maxDrawdownPct": 6,
            "excludedSymbols": ["MSFT"],
        },
    )

    response = client.post("/api/recommendations/stocks/refresh", json={})

    assert response.status_code == 200
    assert all(item["symbol"] != "MSFT" for item in response.json()["items"])


def test_watchlist_holding_and_active_symbol_are_not_recommended(recommendation_app) -> None:
    recommendation_app.state.recommendation_watchlist_provider = lambda user_sub: ["AAPL"]
    recommendation_app.state.recommendation_portfolio_provider = lambda user_sub: [
        {"symbol": "MSFT", "sector": "Technology", "marketValueForeign": 10_000}
    ]
    client = TestClient(recommendation_app)
    client.put(
        "/api/recommendations/profile",
        json={
            "riskLevel": "balanced",
            "horizon": "intraday",
            "maxDrawdownPct": 6,
            "preferredSectors": ["Technology"],
        },
    )

    response = client.post("/api/recommendations/stocks/refresh", json={"activeSymbol": "AVGO"})

    assert response.status_code == 200
    symbols = {item["symbol"] for item in response.json()["items"]}
    assert "AAPL" not in symbols
    assert "MSFT" not in symbols
    assert "AVGO" not in symbols


def test_refresh_registers_all_scored_candidates_without_score_cutoff(recommendation_app) -> None:
    recommendation_app.state.recommendation_watchlist_provider = lambda user_sub: []
    recommendation_app.state.recommendation_market_provider = lambda: [
        {
            "symbol": symbol,
            "sector": "Technology",
            "industry": "Software",
            "sessionDollarVolume": volume,
            "changePercent": change,
            "lastPrice": 100,
        }
        for symbol, volume, change in [
            (f"LOW{index:02d}", 500_000_000 - index * 10_000_000, round(1.6 - index * 0.1, 2))
            for index in range(1, 17)
        ]
    ]
    recommendation_app.state.recommendation_candles_provider = flat_candles
    recommendation_app.state.recommendation_news_provider = lambda symbol, now=None: []
    client = TestClient(recommendation_app)
    client.put(
        "/api/recommendations/profile",
        json={"riskLevel": "balanced", "horizon": "intraday", "maxDrawdownPct": 6},
    )

    response = client.post("/api/recommendations/stocks/refresh", json={})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert [item["symbol"] for item in payload["items"]] == [f"LOW{index:02d}" for index in range(1, 17)]
    assert len(payload["items"]) == 16
    assert payload["items"][0]["changePercent"] == 1.5
    assert payload["items"][0]["metricsSnapshot"]["changePercent"] == 1.5
    assert all(item["score"] < 75 for item in payload["items"])


def test_refresh_scores_all_candidates_with_market_snapshot_reasons(recommendation_app) -> None:
    recommendation_app.state.recommendation_watchlist_provider = lambda user_sub: []
    recommendation_app.state.recommendation_market_provider = lambda: [
        {
            "symbol": f"FILL{index:02d}",
            "sector": "Technology",
            "industry": "Software",
            "sessionDollarVolume": 500_000_000 - index * 10_000_000,
            "changePercent": round(3.0 - index * 0.1, 2),
            "lastPrice": 100 + index,
        }
        for index in range(1, 21)
    ]
    recommendation_app.state.recommendation_candles_provider = lambda symbol, now: []
    recommendation_app.state.recommendation_news_provider = lambda symbol, now=None: []
    client = TestClient(recommendation_app)
    client.put(
        "/api/recommendations/profile",
        json={"riskLevel": "balanced", "horizon": "intraday", "maxDrawdownPct": 6},
    )

    response = client.post("/api/recommendations/stocks/refresh", json={})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert len(payload["items"]) == 20
    assert payload["summary"]["recommendedCount"] == 20
    assert all(item["reasons"] for item in payload["items"])
    assert all(item["metricsSnapshot"]["fallback"] is True for item in payload["items"])


def test_recommendation_candles_backfills_session_from_clickhouse(monkeypatch: pytest.MonkeyPatch) -> None:
    future_redis_rows = [
        {
            "timestamp": f"2026-07-07T22:{index % 60:02d}:00Z",
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
            "volume": 100,
        }
        for index in range(120)
    ]
    clickhouse_calls: list[dict] = []

    class RedisProvider:
        def recent_candles(self, symbol: str, interval: str, limit: int) -> list[dict]:
            return future_redis_rows

    class ClickHouseProvider:
        def candles(self, symbol: str, interval: str, limit: int, **kwargs) -> list[dict]:
            clickhouse_calls.append({"symbol": symbol, "interval": interval, "limit": limit, **kwargs})
            return fake_candles(symbol, REGULAR_MARKET_TIME)

    provider = types.SimpleNamespace(redis_provider=RedisProvider(), clickhouse_provider=ClickHouseProvider())
    monkeypatch.setattr("app.recommendations.service.get_market_data_provider", lambda: provider)

    rows = RecommendationDataSource(types.SimpleNamespace(state=types.SimpleNamespace())).candles("MSFT", REGULAR_MARKET_TIME)

    assert clickhouse_calls == [
        {
            "symbol": "MSFT",
            "interval": "1m",
            "limit": 720,
            "from_time": "2026-07-07T13:30:00Z",
            "to_time": "2026-07-07T16:00:00Z",
        }
    ]
    assert rows
    assert all(str(row["timestamp"]) <= "2026-07-07T16:00:00Z" for row in rows)
    assert rows[-1]["timestamp"] == "2026-07-07T15:59:00Z"


def test_recommendation_json_ready_converts_postgres_numeric_decimal() -> None:
    payload = _json_ready({"max_drawdown_pct": Decimal("6.00"), "nested": [Decimal("1.25")]})

    assert payload == {"max_drawdown_pct": 6.0, "nested": [1.25]}


def test_recommendation_worker_processes_profiled_users_once_per_slot(recommendation_app) -> None:
    client = TestClient(recommendation_app)
    client.put(
        "/api/recommendations/profile",
        json={
            "riskLevel": "balanced",
            "horizon": "intraday",
            "maxDrawdownPct": 6,
            "preferredSectors": ["Technology"],
        },
    )
    worker = RecommendationWorker(recommendation_app)

    first = worker.run_once(now=REGULAR_MARKET_TIME)
    second = worker.run_once(now=REGULAR_MARKET_TIME)

    assert first["processed"] == 1
    assert first["generated"] == 1
    assert second["processed"] == 1
    assert second["generated"] == 0


def test_recommendation_news_prefers_redis_and_skips_clickhouse(recommendation_app, monkeypatch: pytest.MonkeyPatch) -> None:
    class RedisProvider:
        def localized_news_articles(self, symbol, limit=5, locale="ko-KR"):
            return [{"symbol": symbol, "sentiment": "positive", "publishedAt": REGULAR_MARKET_TIME.isoformat()}]

    class ClickHouseProvider:
        def localized_news_articles(self, *args, **kwargs):
            raise AssertionError("recommendations must not read ClickHouse news")

    provider = types.SimpleNamespace(redis_provider=RedisProvider(), clickhouse_provider=ClickHouseProvider())
    recommendation_app.state.recommendation_news_provider = None
    monkeypatch.setattr("app.recommendations.service.get_market_data_provider", lambda: provider)

    rows = RecommendationDataSource(recommendation_app).news("MSFT", REGULAR_MARKET_TIME)

    assert rows == [{"symbol": "MSFT", "sentiment": "positive", "publishedAt": REGULAR_MARKET_TIME.isoformat()}]


def test_recommendation_news_falls_back_to_alpaca_api(recommendation_app, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {}

    class RedisProvider:
        def localized_news_articles(self, symbol, limit=5, locale="ko-KR"):
            return []

    class ClickHouseProvider:
        def localized_news_articles(self, *args, **kwargs):
            raise AssertionError("recommendations must not read ClickHouse news")

    def fake_fetch_alpaca_news(key_id, secret_key, **kwargs):
        calls.update({"keyId": key_id, "secretKey": secret_key, **kwargs})
        return [{
            "id": "alpaca-news-1",
            "headline": "Microsoft announces AI infrastructure update",
            "summary": "Microsoft expanded its AI infrastructure roadmap.",
            "created_at": REGULAR_MARKET_TIME.isoformat(),
            "symbols": ["MSFT"],
            "source": "alpaca",
            "url": "https://example.test/news/msft",
        }]

    provider = types.SimpleNamespace(redis_provider=RedisProvider(), clickhouse_provider=ClickHouseProvider())
    recommendation_app.state.recommendation_news_provider = None
    monkeypatch.setattr("app.recommendations.service.get_market_data_provider", lambda: provider)
    monkeypatch.setattr("alfaka.common.secrets.load_alpaca_credentials", lambda: ("key", "secret"))
    monkeypatch.setattr("alfaka.alpaca.news.fetch_alpaca_news", fake_fetch_alpaca_news)

    rows = RecommendationDataSource(recommendation_app).news("MSFT", REGULAR_MARKET_TIME)

    assert calls["keyId"] == "key"
    assert calls["secretKey"] == "secret"
    assert calls["symbols"] == ["MSFT"]
    assert calls["sort"] == "desc"
    assert calls["start"].startswith("2026-06-30")
    assert calls["end"].startswith("2026-07-07")
    assert rows[0]["symbol"] == "MSFT"
    assert rows[0]["articleId"] == "alpaca-news-1"
    assert rows[0]["publishedAt"] == REGULAR_MARKET_TIME.isoformat()
    assert rows[0]["dataSource"] == "alpaca-direct"


def test_recommendation_news_batches_alpaca_fallback_misses(recommendation_app, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class RedisProvider:
        def localized_news_articles(self, symbol, limit=5, locale="ko-KR"):
            return []

    def fake_fetch_alpaca_news(key_id, secret_key, **kwargs):
        calls.append(kwargs)
        return [{
            "id": "shared-news-1",
            "headline": "AI infrastructure suppliers rally",
            "summary": "Microsoft and Broadcom were mentioned in AI infrastructure coverage.",
            "created_at": REGULAR_MARKET_TIME.isoformat(),
            "symbols": ["MSFT", "AVGO"],
            "source": "alpaca",
        }]

    provider = types.SimpleNamespace(redis_provider=RedisProvider(), clickhouse_provider=object())
    recommendation_app.state.recommendation_news_provider = None
    monkeypatch.setattr("app.recommendations.service.get_market_data_provider", lambda: provider)
    monkeypatch.setattr("alfaka.common.secrets.load_alpaca_credentials", lambda: ("key", "secret"))
    monkeypatch.setattr("alfaka.alpaca.news.fetch_alpaca_news", fake_fetch_alpaca_news)

    rows = RecommendationDataSource(recommendation_app).news_for_symbols(["MSFT", "AVGO"], REGULAR_MARKET_TIME)

    assert len(calls) == 1
    assert calls[0]["symbols"] == ["MSFT", "AVGO"]
    assert rows["MSFT"][0]["dataSource"] == "alpaca-direct"
    assert rows["AVGO"][0]["dataSource"] == "alpaca-direct"


def fake_candles(symbol: str, _now: datetime) -> list[dict]:
    start = 100.0 if symbol != "SPY" else 500.0
    end = 104.0 if symbol in {"AAPL", "MSFT", "AVGO"} else 501.0 if symbol == "SPY" else 101.0
    candles = []
    for index in range(180):
        progress = index / 179
        close = start + (end - start) * progress
        volume = 10_000 if index < 120 else 40_000
        candles.append(
            {
                "timestamp": f"2026-07-07T{13 + index // 60:02d}:{index % 60:02d}:00Z",
                "open": close - 0.05,
                "high": close + 0.1,
                "low": close - 0.2,
                "close": close,
                "volume": volume,
            }
        )
    return candles


def professional_previous_session_candles(symbol: str, now: datetime) -> list[dict]:
    base = 500.0 if symbol == "SPY" else 100.0
    start = now - timedelta(days=1, minutes=389)
    return [
        {
            "timestamp": (start + timedelta(minutes=index)).isoformat(),
            "open": base + index * 0.01 - 0.05,
            "high": base + index * 0.01 + 0.1,
            "low": base + index * 0.01 - 0.1,
            "close": base + index * 0.01,
            "volume": 10_000 + index * 100,
        }
        for index in range(390)
    ]


def flat_candles(symbol: str, _now: datetime) -> list[dict]:
    candles = []
    for index in range(180):
        close = 500.0 if symbol == "SPY" else 100.0
        candles.append(
            {
                "timestamp": f"2026-07-07T{13 + index // 60:02d}:{index % 60:02d}:00Z",
                "open": close,
                "high": close + 0.05,
                "low": close - 0.05,
                "close": close,
                "volume": 10_000,
            }
        )
    return candles


def professional_daily_candles(symbol: str, _now: datetime) -> list[dict]:
    rows = []
    count = 260
    start = datetime(2026, 7, 6, tzinfo=timezone.utc) - timedelta(days=count - 1)
    for index in range(count):
        timestamp = start + timedelta(days=index)
        move = 0.1 if symbol == "SPY" else 1.0 if symbol == "MSFT" else 0.7
        close = 100 * (1 + move / 100)
        rows.append({
            "timestamp": timestamp.isoformat(),
            "open": 100,
            "high": close + 0.2,
            "low": 99.8,
            "close": close,
            "volume": 5_000_000 + index * 10_000,
            "is_closed": True,
        })
    return rows
