from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "systems/api-server/pods/api-server/gops-backend"
MARKET_SHARED = ROOT / "systems/market-data/shared"
ORDER_SHARED = ROOT / "systems/order/shared"
AGENT_SHARED = ROOT / "systems/agent-orchestration/shared"
for path in (BACKEND, MARKET_SHARED, ORDER_SHARED, AGENT_SHARED, ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.recommendations.fixed_replay import (  # noqa: E402
    DEFAULT_ARTIFACT_PATH,
    FixedReplayRecommendationProvider,
)
from app.recommendations.routes import router as recommendation_router  # noqa: E402
from app.recommendations.worker import RecommendationWorker  # noqa: E402


class FailingRecommendationRepository:
    def __getattr__(self, name):
        raise AssertionError(f"fixed replay must not access repository: {name}")


def configured_app(monkeypatch: pytest.MonkeyPatch, artifact: Path = DEFAULT_ARTIFACT_PATH):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("RECOMMENDATION_FIXED_REPLAY_ENABLED", "true")
    monkeypatch.setenv("RECOMMENDATION_FIXED_REPLAY_PATH", str(artifact))
    monkeypatch.setenv("RECOMMENDATION_DECISION_V1_ENABLED", "false")
    app = FastAPI()
    app.include_router(recommendation_router)
    app.state.recommendation_repository = FailingRecommendationRepository()
    return app


def test_latest_and_refresh_return_identical_common_artifact_without_db_writes(monkeypatch) -> None:
    app = configured_app(monkeypatch)
    client = TestClient(app)

    latest = client.get("/api/recommendations/stocks/latest?sessionMode=pre")
    refresh = client.post(
        "/api/recommendations/stocks/refresh",
        json={"activeSymbol": "NVDA", "sessionMode": "regular"},
    )

    assert latest.status_code == 200
    assert refresh.status_code == 200
    assert latest.json() == refresh.json()
    payload = latest.json()
    assert payload["status"] == "completed"
    assert payload["marketDate"] == "2026-07-15"
    assert payload["sourceMode"] == "historical_reconstruction"
    assert payload["scoringMode"] == "canonical_fixed_replay"
    assert payload["evidencePoolDigest"]
    assert payload["narrativeMode"] == "company_grounded"
    assert len(payload["items"]) == 15
    assert min(item["confidence"] for item in payload["items"]) >= 0.70
    assert all(item["explanation"]["primary"]["status"] == "ready" for item in payload["items"])
    assert all(item["explanation"]["primary"]["source"] == "deterministic" for item in payload["items"])
    assert all("decision" not in item for item in payload["items"])
    assert all("sizing" not in item for item in payload["items"])
    assert all("keyEvidence" not in item for item in payload["items"])
    assert all("counterEvidence" not in item for item in payload["items"])
    assert all("cautions" not in item for item in payload["items"])
    assert all(
        "limitedPortfolioEvidence" not in item["metricsSnapshot"]["softPenalties"]
        for item in payload["items"]
    )


def test_decision_v1_returns_fixed_direct_actions_and_no_dummy_missing_evidence(monkeypatch) -> None:
    class CutoffRepository:
        def get_profile_at(self, _user_sub, _cutoff):
            return None

        def get_portfolio_snapshot_at(self, _user_sub, _cutoff):
            return None

    app = configured_app(monkeypatch)
    monkeypatch.setenv("RECOMMENDATION_DECISION_V1_ENABLED", "true")
    app.state.recommendation_repository = CutoffRepository()
    client = TestClient(app)

    latest = client.get("/api/recommendations/stocks/latest")
    refresh = client.post("/api/recommendations/stocks/refresh", json={"activeSymbol": "NVDA"})

    assert latest.status_code == 200
    assert latest.json() == refresh.json()
    payload = latest.json()
    assert payload["scoringMode"] == "cutoff_user_profile"
    actions = {item["symbol"]: item["action"] for item in payload["items"]}
    assert {symbol for symbol, action in actions.items() if action == "buy"} == {"JPM", "AMZN"}
    assert {symbol for symbol, action in actions.items() if action == "conditional_buy"} == {
        "NVDA", "GOOGL", "PANW", "PLTR"
    }
    assert all(4 <= len(item["keyEvidence"]) <= 6 for item in payload["items"])
    assert all(item["explanation"]["primary"]["promptVersion"] == "recommendation-decision-renderer.ko.v8" for item in payload["items"])
    assert len({item["explanation"]["primary"]["listSummary"] for item in payload["items"]}) == len(payload["items"])
    assert len({item["explanation"]["primary"]["headline"] for item in payload["items"]}) == len(payload["items"])
    assert all(3 <= len(re.findall(r"니다[.!?]", item["explanation"]["primary"]["body"])) <= 5 for item in payload["items"])
    assert all(
        not any(character.isdigit() for character in evidence["interpretation"])
        and not any(token in evidence["interpretation"] for token in ("%p", "bp", "/100"))
        and evidence["metrics"]
        for item in payload["items"]
        for evidence in item["keyEvidence"]
    )
    assert all(
        metric["value"]
        and metric["comparison"]
        and 0 <= metric["valuePositionPct"] <= 100
        and 0 <= metric["referencePositionPct"] <= 100
        for item in payload["items"]
        for evidence in item["keyEvidence"]
        for metric in evidence["metrics"]
    )
    assert all(item["explanation"]["primary"]["headline"] for item in payload["items"])
    assert all(
        not any(character.isdigit() for character in item["explanation"]["primary"]["headline"])
        for item in payload["items"]
    )
    assert all(item["counterEvidence"]["sentence"] for item in payload["items"] if item["counterEvidence"])
    assert all(item["cautions"] for item in payload["items"])
    assert all(
        len({row["code"] for row in item["cautions"]}) == len(item["cautions"])
        and len({row["sentence"] for row in item["cautions"]}) == len(item["cautions"])
        for item in payload["items"]
    )
    assert all(
        not {"decision_scope", "confidence_scope"}.intersection({row["code"] for row in item["cautions"]})
        for item in payload["items"]
    )
    assert all(
        "chase_limit" in {row["code"] for row in item["cautions"]}
        for item in payload["items"]
        if item["action"] in {"buy", "conditional_buy"}
    )
    jpm = next(item for item in payload["items"] if item["symbol"] == "JPM")
    chase_sentence = next(row["sentence"] for row in jpm["cautions"] if row["code"] == "chase_limit")
    assert "돌파 매수는" in chase_sentence and "무효화 기준" in chase_sentence and "하락 폭은 주당" in chase_sentence
    nvda = next(item for item in payload["items"] if item["symbol"] == "NVDA")
    assert "마감 전" in nvda["counterEvidence"]["sentence"]
    assert all("중립값" not in " ".join(item["riskWarnings"]) for item in payload["items"])
    assert all(item["sizing"]["status"] == "unavailable" for item in payload["items"] if item["action"] in {"buy", "conditional_buy"})


def test_common_api_digest_is_the_verified_digest_used_by_simulation(monkeypatch) -> None:
    app = configured_app(monkeypatch)
    response = TestClient(app).get("/api/recommendations/stocks/latest")

    assert response.status_code == 200
    assert response.json()["recommendationDigest"] == FixedReplayRecommendationProvider.load().payload[
        "recommendationDigest"
    ]


def test_personalized_replay_excludes_existing_holdings_and_has_user_digest(monkeypatch) -> None:
    monkeypatch.setenv("RECOMMENDATION_DECISION_V1_ENABLED", "true")
    provider = FixedReplayRecommendationProvider.load()
    snapshot = {
        "id": 7,
        "source_as_of": "2026-07-14T15:59:00-04:00",
        "payload": {
            "totalValue": 100_000,
            "cash": 20_000,
            "positions": [{"symbol": "JPM", "sector": "Financials", "marketValueForeign": 10_000}],
        },
    }

    balanced = provider.response(profile=None, portfolio_snapshot=snapshot)
    aggressive = provider.response(
        profile={"risk_level": "aggressive", "recommendation_style": "momentum"},
        portfolio_snapshot=snapshot,
    )

    assert "JPM" not in [item["symbol"] for item in balanced["items"]]
    assert balanced["evidencePoolDigest"] == aggressive["evidencePoolDigest"]
    assert balanced["scoringDigest"] != aggressive["scoringDigest"]
    assert balanced["recommendationDigest"] != aggressive["recommendationDigest"]


def test_tampered_artifact_fails_closed_without_legacy_fallback(monkeypatch, tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    shutil.copytree(DEFAULT_ARTIFACT_PATH, artifact)
    payload_path = artifact / "recommendation.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload["items"][0]["score"] += 1
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    app = configured_app(monkeypatch, artifact)

    response = TestClient(app).get("/api/recommendations/stocks/latest")

    assert response.status_code == 503
    assert response.json() == {"detail": "fixed_replay_recommendation_unavailable"}


def test_worker_skips_profiles_generation_and_notifications(monkeypatch) -> None:
    monkeypatch.setenv("RECOMMENDATION_FIXED_REPLAY_ENABLED", "true")
    monkeypatch.setenv("RECOMMENDATION_FIXED_REPLAY_PATH", str(DEFAULT_ARTIFACT_PATH))

    result = RecommendationWorker.from_env().run_once()

    assert result["status"] == "fixed_replay_override"
    assert result["ready"] is True
    assert result["processed"] == 0
    assert result["generated"] == 0
    assert result["recommendationDigest"]
