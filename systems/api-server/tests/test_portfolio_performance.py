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

    from app.main import create_app
    from app.recommendations.repository import InMemoryRecommendationRepository
    from app.services.portfolio_performance import build_portfolio_performance
except Exception as exc:  # pragma: no cover - dependency guard for lean envs
    pytest.skip(f"Portfolio performance tests are unavailable: {exc}", allow_module_level=True)


def snapshot(
    at: str,
    rate: float,
    *,
    portfolio_value: float | None = None,
    purchase_amounts: tuple[float, ...] = (),
) -> dict:
    return {
        "payload": {
            "asOf": at,
            "account": {
                "unrealizedPnlRate": rate,
                **({"totalValueForeign": portfolio_value} if portfolio_value is not None else {}),
            },
            "positions": [
                {"symbol": f"TEST{index}", "purchaseAmountForeign": amount}
                for index, amount in enumerate(purchase_amounts)
            ],
        },
        "source_as_of": at,
    }


def test_performance_normalizes_actual_reported_returns_without_generating_points() -> None:
    payload = build_portfolio_performance(
        [
            snapshot("2026-07-14T00:00:00Z", 10),
            snapshot("2026-07-15T00:00:00Z", 21),
            snapshot("2026-07-16T00:00:00Z", 15.5),
        ],
        {
            "symbol": "^GSPC",
            "name": "S&P 500",
            "method": "price_return",
            "points": [
                {"time": "2026-07-14T00:00:00Z", "returnPercent": 0},
                {"time": "2026-07-15T00:00:00Z", "returnPercent": 5},
                {"time": "2026-07-16T00:00:00Z", "returnPercent": 8},
            ],
        },
        range_value="1W",
    )

    assert payload["status"] == "ready"
    assert [point["returnPercent"] for point in payload["portfolio"]["points"]] == [0, 10, 5]
    assert [point["returnPercent"] for point in payload["benchmark"]["points"]] == [0, 5, 8]


def test_performance_does_not_invent_history_when_snapshots_are_missing() -> None:
    payload = build_portfolio_performance([], None, range_value="1Y")

    assert payload["status"] == "insufficient_history"
    assert payload["portfolio"]["points"] == []
    assert payload["benchmark"]["points"] == []


def test_performance_preserves_snapshot_value_and_current_holdings_cost_basis() -> None:
    payload = build_portfolio_performance(
        [
            snapshot("2026-07-15T00:00:00Z", 5, portfolio_value=10_500, purchase_amounts=(6_000, 4_000)),
            snapshot("2026-07-16T00:00:00Z", 10, portfolio_value=11_200, purchase_amounts=(6_500, 3_500)),
        ],
        None,
        range_value="1W",
    )

    points = payload["portfolio"]["points"]
    assert [point["portfolioValue"] for point in points] == [10_500, 11_200]
    assert [point["holdingsCostBasis"] for point in points] == [10_000, 10_000]
    assert "netInvestedPrincipal" not in points[-1]


def test_account_performance_route_uses_user_daily_snapshots_and_benchmark() -> None:
    os.environ["AUTH_ENABLED"] = "false"
    app = create_app()
    repository = InMemoryRecommendationRepository()
    repository.upsert_portfolio_snapshot(
        "dev-auth-disabled",
        {"asOf": "2026-07-14T00:00:00Z", "account": {"unrealizedPnlRate": 10}, "positions": []},
    )
    repository.upsert_portfolio_snapshot(
        "dev-auth-disabled",
        {"asOf": "2026-07-15T00:00:00Z", "account": {"unrealizedPnlRate": 21}, "positions": []},
    )
    app.state.recommendation_repository = repository
    app.state.portfolio_performance_now_provider = lambda: datetime(2026, 7, 16, tzinfo=timezone.utc)
    app.state.portfolio_benchmark_provider = lambda range_value, start_at: {
        "symbol": "^GSPC",
        "name": "S&P 500",
        "method": "price_return",
        "source": "test",
        "points": [
            {"time": "2026-07-14T00:00:00Z", "returnPercent": 0},
            {"time": "2026-07-15T00:00:00Z", "returnPercent": 4},
        ],
    }

    response = TestClient(app).get("/api/account/performance?range=1W")

    assert response.status_code == 200
    payload = response.json()
    assert payload["range"] == "1W"
    assert payload["status"] == "ready"
    assert payload["portfolio"]["points"][-1]["returnPercent"] == 10
    assert payload["benchmark"]["points"][-1]["returnPercent"] == 4
