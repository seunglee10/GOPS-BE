import json
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
ORDER_SHARED = ROOT / "systems" / "order" / "shared"
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend"
for path in (str(MARKET_SHARED), str(ORDER_SHARED), str(BACKEND), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

try:
    from fastapi.testclient import TestClient

    import app.routes.paper_trading as paper_routes
    from alfaka.serving.symbol_registry import SymbolRegistry
    from app.auth.models import AuthenticatedUser
    from app.main import create_app
    from kis_trader.paper.memory import InMemoryPaperTradingRepository
    from kis_trader.paper.fixture import SEED_PROFILE, fallback_price

    TESTCLIENT_AVAILABLE = True
except Exception:
    TestClient = None
    TESTCLIENT_AVAILABLE = False


def payload(**overrides):
    value = {
        "market": "overseas",
        "symbol": "AAPL",
        "side": "buy",
        "qty": "10",
        "price": "100",
        "exchange": "NASD",
        "order_division": "00",
    }
    value.update(overrides)
    return value


@unittest.skipUnless(TESTCLIENT_AVAILABLE, "FastAPI TestClient is not available")
class PaperTradingRoutesTest(unittest.TestCase):
    def setUp(self):
        os.environ["AUTH_ENABLED"] = "false"
        os.environ["IDEMPOTENCY_HASH_SECRET"] = "paper-test-secret"
        self.repository = InMemoryPaperTradingRepository()
        self.subscription_syncs = []
        self.app = create_app()
        self.app.state.paper_trading_repository = self.repository
        self.persisted_portfolio_snapshots = []
        self.app.state.recommendation_repository = SimpleNamespace(
            upsert_portfolio_snapshot=lambda user_sub, payload: self.persisted_portfolio_snapshots.append(
                (user_sub, payload)
            ),
        )
        self.app.state.paper_symbol_validator = lambda symbol: {
            "symbol": symbol,
            "assetClass": "us_equity",
            "tradable": True,
            "status": "active",
        }
        self.app.state.paper_symbol_search = lambda _query, _limit: [
            {"symbol": "AAPL", "name": "Apple", "assetClass": "us_equity", "tradable": True, "status": "active"},
            {"symbol": "SPY", "name": "SPDR S&P 500 ETF", "assetClass": "us_etf", "tradable": True, "status": "active"},
            {"symbol": "BTC/USD", "name": "Bitcoin", "assetClass": "crypto", "tradable": True, "status": "active"},
            {"symbol": "OLD", "name": "Inactive", "assetClass": "us_equity", "tradable": True, "status": "inactive"},
        ]
        self.app.state.paper_subscription_sync = lambda orders, positions: self.subscription_syncs.append((orders, positions))
        self.app.state.paper_price_resolver = lambda _symbol: {"price": "105", "source": "test"}
        self.client = TestClient(self.app)

    def submit(self, body=None, key="paper-1"):
        return self.client.post(
            "/api/paper/orders",
            json=body or payload(),
            headers={"Idempotency-Key": key},
        )

    def test_account_is_persistent_and_starts_with_one_hundred_thousand_dollars(self):
        response = self.client.get("/api/paper/account")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["account"]["cash_balance"], 100000.0)
        self.assertEqual(response.json()["execution_mode"], "paper")

    def test_account_refresh_persists_json_ready_paper_portfolio_snapshot(self):
        response = self.client.get("/api/paper/account")

        self.assertEqual(response.status_code, 200)
        user_sub, snapshot = self.persisted_portfolio_snapshots[-1]
        self.assertEqual(user_sub, "dev-auth-disabled")
        self.assertEqual(snapshot["source"], "paper-shared")
        self.assertEqual(snapshot["account"]["cashForeign"], 100000.0)
        self.assertEqual(snapshot["positions"], [])
        json.dumps(snapshot)

    def test_paper_symbol_search_only_returns_active_tradable_us_assets(self):
        response = self.client.get("/api/paper/symbols/search?q=ap&limit=20")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["symbol"] for item in response.json()["symbols"]], ["AAPL", "SPY"])

    def test_submit_creates_pending_paper_order_without_kis_outbox(self):
        with mock.patch(
            "kis_trader.persistence.postgres.PostgresOrderRepository.create_received_order",
            side_effect=AssertionError("KIS order repository must not be called"),
        ):
            response = self.submit()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "pending")
        self.assertEqual(response.json()["execution_mode"], "paper")
        self.assertEqual(self.repository.account_snapshot("dev-auth-disabled")["account"]["reserved_cash"], Decimal("1000"))
        self.assertEqual(self.subscription_syncs[-1][0], ["AAPL"])
        self.assertFalse(hasattr(self.repository, "outbox_events"))

    def test_submit_accepts_lowercase_configured_universe_symbol(self):
        self.app.state.paper_symbol_validator = SymbolRegistry().detail

        response = self.submit(payload(symbol="aapl"))

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["symbol"], "AAPL")

    def test_submit_requires_idempotency_and_replays_same_order(self):
        missing = self.client.post("/api/paper/orders", json=payload())
        first = self.submit()
        second = self.submit()

        self.assertEqual(missing.status_code, 400)
        self.assertEqual(first.json()["order_id"], second.json()["order_id"])
        self.assertEqual(second.headers["X-Idempotent-Replay"], "true")

    def test_pretrade_and_submit_enforce_cash_and_position(self):
        risk = self.client.post("/api/paper/risk/pretrade", json=payload(qty="2000"))
        submit = self.submit(payload(qty="2000"))
        sell = self.submit(payload(side="sell", qty="1"), key="sell")

        self.assertEqual(risk.status_code, 200)
        self.assertEqual(risk.json()["risk"]["verdict"], "block")
        self.assertEqual(submit.status_code, 422)
        self.assertEqual(sell.status_code, 422)

    def test_cancel_releases_balance_and_updates_websocket(self):
        created = self.submit().json()
        cancelled = self.client.post(f"/api/paper/orders/{created['order_id']}/cancel")

        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        balance = self.client.get("/api/paper/account/balance?price=100").json()
        self.assertEqual(balance["orderable_cash"], 100000.0)
        with self.client.websocket_connect(f"/ws/paper/orders/{created['order_id']}") as socket:
            message = socket.receive_json()
        self.assertEqual(message["order"]["status"], "cancelled")

    def test_http_and_websocket_do_not_expose_another_users_order(self):
        created = self.submit().json()
        another_user = AuthenticatedUser(
            sub="another-user",
            email="another@example.com",
            email_verified=True,
        )
        self.app.dependency_overrides[paper_routes.require_current_user] = lambda: another_user

        response = self.client.get(f"/api/paper/orders/{created['order_id']}")

        self.assertEqual(response.status_code, 404)
        with mock.patch.object(paper_routes, "require_websocket_user", return_value=another_user):
            with self.client.websocket_connect(f"/ws/paper/orders/{created['order_id']}") as socket:
                message = socket.receive_json()
        self.assertEqual(message["type"], "error")
        self.assertEqual(message["detail"], "paper order not found")

    def test_fill_updates_account_valuation(self):
        created = self.submit().json()
        self.repository.match_quote(
            symbol="AAPL",
            bid_price=Decimal("98"),
            ask_price=Decimal("99"),
            quote_timestamp="2026-07-14T10:00:00Z",
            quote_event_id="quote-1",
        )

        account = self.client.get("/api/paper/account").json()
        self.assertEqual(account["positions"][0]["current_price"], 105)
        self.assertEqual(account["positions"][0]["unrealized_pnl"], 60)
        self.assertEqual(account["account"]["equity"], 100060.0)
        self.assertEqual(self.client.get(f"/api/paper/orders/{created['order_id']}").json()["fill_price"], 99)

    def test_reset_preserves_previous_history(self):
        self.submit()
        response = self.client.post("/api/paper/account/reset", json={"starting_cash": "250000"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["account"]["generation"], 2)
        current = self.client.get("/api/paper/orders").json()["orders"]
        history = self.client.get("/api/paper/orders?include_previous=true").json()["orders"]
        self.assertEqual(current, [])
        self.assertEqual(history[0]["status"], "cancelled")

    def test_seeded_account_holdings_and_performance_share_the_same_fixture(self):
        self.repository = InMemoryPaperTradingRepository(seed_profile=SEED_PROFILE)
        self.app.state.paper_trading_repository = self.repository
        self.app.state.paper_price_resolver = lambda symbol: {
            "price": fallback_price(symbol), "source": "seeded-demo",
        }
        self.app.state.recommendation_repository = SimpleNamespace(
            list_daily_portfolio_snapshots=lambda _user, _start: [],
            upsert_portfolio_snapshot=lambda *_args: None,
        )
        self.app.state.portfolio_benchmark_provider = lambda *_args: None

        account = self.client.get("/api/paper/account").json()
        holdings = self.client.get("/api/account/holdings").json()
        performance = self.client.get("/api/account/performance?range=ALL").json()

        account_positions = {row["symbol"]: row for row in account["positions"]}
        holding_positions = {row["symbol"]: row for row in holdings["positions"]}
        self.assertEqual(set(account_positions), {"GOOGL", "MSFT", "JPM", "XOM", "JNJ", "COST", "HD"})
        for symbol in account_positions:
            self.assertEqual(account_positions[symbol]["qty"], holding_positions[symbol]["quantity"])
            self.assertEqual(account_positions[symbol]["average_price"], holding_positions[symbol]["averagePrice"])
            self.assertEqual(account_positions[symbol]["market_value"], holding_positions[symbol]["marketValueForeign"])
        self.assertEqual(account["account"]["equity"], 104793.52)
        self.assertEqual(performance["dataOrigin"], "seeded-demo")
        self.assertEqual(performance["portfolio"]["points"][-1]["portfolioValue"], 104793.52)
        self.assertEqual(performance["portfolio"]["points"][-1]["holdingsCostBasis"], 79183.38)

        submitted = self.submit(payload(symbol="GOOGL", qty="1", price="200"), key="after-seed").json()
        self.repository.match_quote(
            symbol="GOOGL", bid_price=Decimal("184"), ask_price=Decimal("185"),
            quote_timestamp="2026-07-18T15:00:00Z", quote_event_id="after-seed-fill",
        )
        self.assertEqual(self.client.get(f"/api/paper/orders/{submitted['order_id']}").json()["status"], "filled")
        self.assertEqual(self.client.get("/api/account/performance?range=ALL").json()["dataOrigin"], "account-history")


if __name__ == "__main__":
    unittest.main()
