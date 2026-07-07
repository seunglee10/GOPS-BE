from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timezone
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
    from app.recommendations.repository import InMemoryRecommendationRepository, RecommendationSchemaUnavailable
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


def test_pre_and_regular_recommendations_use_separate_run_keys(recommendation_app) -> None:
    client = TestClient(recommendation_app)
    client.put(
        "/api/recommendations/profile",
        json={"riskLevel": "balanced", "horizon": "intraday", "maxDrawdownPct": 6},
    )

    recommendation_app.state.recommendation_now_provider = lambda: PRE_MARKET_TIME
    pre = client.post("/api/recommendations/stocks/refresh", json={"sessionMode": "pre"}).json()
    recommendation_app.state.recommendation_now_provider = lambda: REGULAR_MARKET_TIME
    regular = client.post("/api/recommendations/stocks/refresh", json={"sessionMode": "regular"}).json()

    assert pre["status"] == "completed"
    assert regular["status"] == "completed"
    assert ":pre:" in pre["runKey"]
    assert ":regular:" in regular["runKey"]
    assert pre["runKey"] != regular["runKey"]


def test_regular_refresh_before_open_does_not_create_run(recommendation_app) -> None:
    recommendation_app.state.recommendation_now_provider = lambda: PRE_MARKET_TIME
    client = TestClient(recommendation_app)
    client.put(
        "/api/recommendations/profile",
        json={"riskLevel": "balanced", "horizon": "intraday", "maxDrawdownPct": 6},
    )

    response = client.post("/api/recommendations/stocks/refresh", json={"sessionMode": "regular"})

    assert response.status_code == 200
    assert response.json()["status"] == "market_closed"
    assert recommendation_app.state.recommendation_repository.latest_run("dev-auth-disabled") is None


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
