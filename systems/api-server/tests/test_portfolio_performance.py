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
    from app.routes.account import _performance_principal_for_snapshots
    from app.services.portfolio_performance import build_portfolio_performance
    from kis_trader.paper.fixture import DEMO_EQUITY, SEED_PROFILE
    from kis_trader.paper.memory import InMemoryPaperTradingRepository
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


def test_performance_keeps_net_principal_separate_from_holdings_cost_basis() -> None:
    payload = build_portfolio_performance(
        [
            snapshot("2026-07-15T00:00:00Z", 5, portfolio_value=10_500, purchase_amounts=(6_000, 4_000)),
            snapshot("2026-07-16T00:00:00Z", 10, portfolio_value=11_200, purchase_amounts=(7_000, 4_000)),
        ],
        None,
        range_value="1W",
        net_invested_principal=12_000,
    )

    points = payload["portfolio"]["points"]
    assert [point["holdingsCostBasis"] for point in points] == [10_000, 11_000]
    assert [point["netInvestedPrincipal"] for point in points] == [12_000, 12_000]


def test_current_paper_principal_is_not_backfilled_across_account_resets_or_simulation() -> None:
    current_run_started_at = datetime(2026, 7, 16, tzinfo=timezone.utc)
    current_run = [snapshot("2026-07-16T00:00:00Z", 5), snapshot("2026-07-17T00:00:00Z", 10)]
    prior_run = [snapshot("2026-07-15T00:00:00Z", 3), *current_run]

    assert _performance_principal_for_snapshots(
        current_run,
        current_principal=12_000,
        current_principal_started_at=current_run_started_at,
        simulation_time=None,
    ) == 12_000
    assert _performance_principal_for_snapshots(
        prior_run,
        current_principal=12_000,
        current_principal_started_at=current_run_started_at,
        simulation_time=None,
    ) is None
    assert _performance_principal_for_snapshots(
        current_run,
        current_principal=12_000,
        current_principal_started_at=current_run_started_at,
        simulation_time=datetime(2026, 7, 16, 12, tzinfo=timezone.utc),
    ) is None


def test_account_performance_route_uses_user_daily_snapshots_and_benchmark() -> None:
    os.environ["AUTH_ENABLED"] = "false"
    app = create_app()
    repository = InMemoryRecommendationRepository()
    repository.upsert_portfolio_snapshot(
        "dev-auth-disabled",
        {
            "asOf": "2026-07-14T00:00:00Z",
            "source": "account-history",
            "account": {"unrealizedPnlRate": 10},
            "positions": [],
        },
    )
    repository.upsert_portfolio_snapshot(
        "dev-auth-disabled",
        {
            "asOf": "2026-07-15T00:00:00Z",
            "source": "account-history",
            "account": {"unrealizedPnlRate": 21},
            "positions": [],
        },
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


def test_daily_paper_history_filters_stale_kis_rows_before_selecting_day_latest() -> None:
    repository = InMemoryRecommendationRepository()
    repository.upsert_portfolio_snapshot(
        "user-1",
        {
            "asOf": "2026-07-15T10:00:00Z",
            "source": "account-history",
            "account": {"unrealizedPnlRate": 5},
            "positions": [],
        },
    )
    repository.upsert_portfolio_snapshot(
        "user-1",
        {
            "asOf": "2026-07-15T11:00:00Z",
            "source": "kis-demo",
            "account": {"unrealizedPnlRate": 90},
            "positions": [],
        },
    )

    rows = repository.list_daily_portfolio_snapshots_for_sources(
        "user-1",
        None,
        ("paper-shared", "account-history", "seeded-demo"),
    )

    assert len(rows) == 1
    assert rows[0]["payload"]["source"] == "account-history"
    assert rows[0]["payload"]["account"]["unrealizedPnlRate"] == 5


def test_seeded_performance_ignores_live_valuation_outlier_until_user_trades() -> None:
    os.environ["AUTH_ENABLED"] = "false"
    app = create_app()
    paper_repository = InMemoryPaperTradingRepository(seed_profile=SEED_PROFILE)
    paper_repository.ensure_account("dev-auth-disabled")
    repository = InMemoryRecommendationRepository()
    for row in paper_repository.portfolio_history:
        repository.upsert_portfolio_snapshot("dev-auth-disabled", row["payload"])
    repository.upsert_portfolio_snapshot(
        "dev-auth-disabled",
        {
            "asOf": "2026-07-17T22:00:00Z",
            "source": "paper-shared",
            "account": {
                "cashForeign": 8401.32,
                "stockValueForeign": 120000,
                "totalValueForeign": 128401.32,
                "unrealizedPnlRate": 30,
            },
            "positions": [],
        },
    )
    app.state.paper_trading_repository = paper_repository
    app.state.recommendation_repository = repository
    app.state.portfolio_performance_now_provider = lambda: datetime(2026, 7, 18, tzinfo=timezone.utc)
    app.state.portfolio_benchmark_provider = lambda *_args: None

    response = TestClient(app).get("/api/account/performance?range=1M")

    assert response.status_code == 200
    payload = response.json()
    assert payload["portfolio"]["points"][-1]["portfolioValue"] == float(DEMO_EQUITY)
    values = [point["portfolioValue"] for point in payload["portfolio"]["points"]]
    assert max(values) - min(values) < 1000
