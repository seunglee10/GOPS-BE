import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
ORDER_SHARED = ROOT / "systems" / "order" / "shared"
ORDER_TEST_ROOT = ROOT / "systems" / "order"
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend"
for path in (str(MARKET_SHARED), str(ORDER_SHARED), str(ORDER_TEST_ROOT), str(BACKEND), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from fastapi.testclient import TestClient

from app.main import create_app
from app.alerts.repository import InMemoryAlertRepository
from app.trade_conditions.repository import InMemoryTradeConditionRepository
from kis_trader.persistence.memory import InMemoryOrderRepository
from kis_trader.paper.fixture import SEED_PROFILE
from kis_trader.paper.memory import InMemoryPaperTradingRepository
from systems.order.tests.kis_trader.fixtures.orders import sample_order_request


class FakeSimulatorGateway:
    def __init__(self, trace=None):
        self.mode = "live"
        self.calls = []
        self.trace = trace
        self.virtual_time = "2026-07-15T00:00:00+09:00"

    def status(self):
        return {
            "available": True,
            "mode": self.mode,
            "state": "ready" if self.mode == "simulation" else "idle",
            "datasetId": "sp500-top20-20260715-kst-v1",
            "runId": "run-1" if self.mode == "simulation" else None,
            "virtualTime": self.virtual_time,
            "startTime": "2026-07-15T00:00:00+09:00",
            "endTime": "2026-07-16T00:00:00+09:00",
            "requestedSpeed": 1,
            "effectiveSpeed": 0,
            "processedEventCount": 0,
            "totalEventCount": 10,
            "progress": 0,
            "lagMs": 0,
            "symbols": [],
        }

    def set_mode(self, mode):
        self.mode = mode
        self.calls.append(("mode", mode))
        if self.trace is not None:
            self.trace.append(("gateway", mode))
        return self.status()

    def action(self, action):
        self.calls.append(("action", action))
        return self.status()

    def set_speed(self, speed):
        self.calls.append(("speed", speed))
        return {**self.status(), "requestedSpeed": speed}

    def quote(self, symbol):
        self.calls.append(("quote", symbol))
        return {"symbol": symbol, "bid": 99.0, "ask": 100.0, "runId": "run-1"}

    def candles(self, symbol, interval, limit):
        self.calls.append(("candles", symbol, interval, limit))
        return {
            "symbol": symbol,
            "interval": interval,
            "simulation": True,
            "asOf": "2026-07-14T15:01:00Z",
            "candles": [{"timestamp": "2026-07-14T15:00:00Z", "close": 100.0}],
        }

    def symbols(self, query="", limit=100):
        self.calls.append(("symbols", query, limit))
        return {"source": "simulation_replay", "symbols": [{"symbol": "NVDA"}]}

    def account(self, user_id):
        self.calls.append(("account", user_id))
        return {
            "status": "ok",
            "source": "gops-simulator",
            "virtualTime": "2026-07-15T00:00:00+09:00",
            "account": {"alias": "반도체 집중형 · SIMULATED", "currency": "USD", "cashForeign": 100},
            "positions": {
                "NVDA": {"symbol": "NVDA", "quantity": 100, "sector": "Information Technology"},
                "AMD": {"symbol": "AMD", "quantity": 50, "sector": "Information Technology"},
            },
            "orders": [],
            "limitations": ["simulation only"],
        }

    def individual_order(self, *, user_id, symbol, side, quantity, order_type, limit_price, idempotency_key):
        self.calls.append(("individual", user_id, symbol, side, quantity, order_type, limit_price, idempotency_key))
        return {"order": {"order_id": "sim-one", "status": "filled", "symbol": symbol, "side": side, "qty": str(quantity), "order_type": order_type, "simulation": True}}

    def order(self, user_id, order_id):
        self.calls.append(("order", user_id, order_id))
        return {"order_id": order_id, "status": "filled", "simulation": True, "runId": "run-1"}

    def order_events(self, user_id, order_id):
        self.calls.append(("order-events", user_id, order_id))
        return {"order_id": order_id, "events": [{"status": "accepted"}, {"status": "filled"}]}

    def conditions(self, user_id):
        self.calls.append(("conditions", user_id))
        return {"conditions": [], "runId": "run-1"}

    def create_condition(self, user_id, payload):
        self.calls.append(("create-condition", user_id, payload))
        return {"condition": {"id": 1, **payload}, "runId": "run-1"}

    def update_condition(self, user_id, condition_id, payload):
        self.calls.append(("update-condition", user_id, condition_id, payload))
        return {"condition": {"id": condition_id, **payload}, "runId": "run-1"}

    def delete_condition(self, user_id, condition_id):
        self.calls.append(("delete-condition", user_id, condition_id))
        return {"deleted": True, "condition": {"id": condition_id}, "runId": "run-1"}


class FakeSimulatorMarketStateManager:
    def __init__(self, trace):
        self.trace = trace

    def capture(self):
        self.trace.append(("market-state", "capture"))

    def restore(self):
        self.trace.append(("market-state", "restore"))


class SimulatorRoutesTest(unittest.TestCase):
    def setUp(self):
        os.environ["AUTH_ENABLED"] = "false"
        os.environ["KIS_ENV"] = "demo"
        os.environ["IDEMPOTENCY_HASH_SECRET"] = "test-secret"
        self.trace = []
        self.gateway = FakeSimulatorGateway(self.trace)
        self.repository = InMemoryOrderRepository()
        self.app = create_app()
        self.app.state.simulator_gateway = self.gateway
        self.app.state.order_repository = self.repository
        self.paper_repository = InMemoryPaperTradingRepository(seed_profile=SEED_PROFILE)
        self.app.state.paper_trading_repository = self.paper_repository
        self.alert_repository = InMemoryAlertRepository()
        self.app.state.alert_repository = self.alert_repository
        self.app.state.trade_condition_repository = InMemoryTradeConditionRepository(self.alert_repository)
        self.app.state.alert_projection = SimpleNamespace(
            upsert_alert=lambda _alert: None,
            delete_alert=lambda _alert_id, symbol=None: None,
        )
        self.client = TestClient(self.app)

    def test_mode_control_is_exposed_to_the_frontend(self):
        initial = self.client.get("/api/simulator/status")
        started = self.client.put("/api/simulator/mode", json={"mode": "simulation"})
        stopped = self.client.put("/api/simulator/mode", json={"mode": "live"})

        self.assertEqual(initial.status_code, 200)
        self.assertEqual(initial.json()["mode"], "live")
        self.assertEqual(started.status_code, 200)
        self.assertEqual(started.json()["mode"], "simulation")
        self.assertEqual(stopped.status_code, 200)
        self.assertEqual(stopped.json()["mode"], "live")
        self.assertEqual(self.gateway.calls, [("mode", "simulation"), ("mode", "live")])
        self.assertEqual(self.trace, [("gateway", "simulation"), ("gateway", "live")])

    def test_operator_can_change_replay_speed(self):
        self.gateway.mode = "simulation"

        response = self.client.put(
            "/api/simulator/speed",
            json={"speed": 300},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["requestedSpeed"], 300)
        self.assertIn(("speed", 300), self.gateway.calls)

        invalid = self.client.put("/api/simulator/speed", json={"speed": 2})
        self.assertEqual(invalid.status_code, 422)

    def test_simulation_quote_is_available_to_quick_order(self):
        self.gateway.mode = "simulation"

        response = self.client.get("/api/simulator/quote?symbol=nvda")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "symbol": "NVDA",
            "bid": 99.0,
            "ask": 100.0,
            "runId": "run-1",
        })
        self.assertIn(("quote", "NVDA"), self.gateway.calls)

    def test_simulation_quote_rejects_live_mode(self):
        response = self.client.get("/api/simulator/quote?symbol=NVDA")

        self.assertEqual(response.status_code, 409)
        self.assertNotIn(("quote", "NVDA"), self.gateway.calls)

    def test_simulation_holdings_use_shared_diversified_paper_account(self):
        self.gateway.mode = "simulation"

        response = self.client.get("/api/account/holdings?market=overseas&currency=USD")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["source"], "paper-shared")
        self.assertEqual({item["symbol"] for item in payload["positions"]}, {"GOOGL", "MSFT", "JPM", "XOM", "JNJ", "COST", "HD"})
        self.assertIn("7섹터", payload["account"]["alias"])
        self.assertEqual(payload["asOf"], "2026-07-15T00:00:00+09:00")

    def test_kis_holdings_source_bypasses_simulation_account(self):
        class FakeKisClient:
            def fetch_holdings(self, *, market, currency, exchange):
                return {
                    "status": "ok",
                    "source": "kis-demo",
                    "account": {"alias": "KIS", "market": market, "currency": currency},
                    "positions": [{"symbol": "AAPL", "quantity": 3, "averagePrice": 180}],
                    "limitations": [],
                }

        self.gateway.mode = "simulation"
        self.app.state.kis_client = FakeKisClient()

        response = self.client.get("/api/account/holdings?market=overseas&currency=USD&source=kis")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["source"], "kis-demo")
        self.assertEqual(response.json()["positions"][0]["symbol"], "AAPL")
        self.assertNotIn(("account", "dev-auth-disabled"), self.gateway.calls)

    def test_standard_order_route_forwards_limit_order_to_replay_ledger(self):
        self.gateway.mode = "simulation"

        response = self.client.post(
            "/api/orders",
            headers={"Idempotency-Key": "manual-one"},
            json={**sample_order_request(symbol="XOM", side="buy", qty="3", price="140.00"), "risk_acknowledged": True},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "accepted")
        self.assertTrue(response.json()["simulation"])
        self.assertEqual(self.repository.orders, {})
        stored = self.paper_repository.get_order("dev-auth-disabled", response.json()["order_id"])
        self.assertEqual(stored["execution_mode"], "simulation")
        self.assertEqual(stored["runId"], "run-1")

    def test_market_order_does_not_require_a_price_in_simulation_mode(self):
        self.gateway.mode = "simulation"

        response = self.client.post(
            "/api/orders",
            headers={"Idempotency-Key": "market-one"},
            json={
                "market": "overseas",
                "symbol": "NVDA",
                "side": "buy",
                "qty": "2",
                "exchange": "NASD",
                "order_type": "market",
            },
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["order_type"], "market")
        self.assertEqual(response.json()["status"], "filled")
        self.assertEqual(response.json()["runId"], "run-1")

    def test_simulation_chart_and_symbol_routes_only_use_replay_gateway(self):
        self.gateway.mode = "simulation"

        historical = {
            "candles": [
                {"timestamp": "2026-07-14T14:59:00Z", "close": 99.0},
                {"timestamp": "2026-07-15T15:01:00Z", "close": 999.0},
            ]
        }
        with patch(
            "app.routes.charts.get_query_service",
            return_value=SimpleNamespace(candle_snapshot=lambda *_args, **_kwargs: historical),
        ):
            candles = self.client.get("/api/charts/candles?symbol=NVDA&interval=1m&limit=20")
        symbols = self.client.get("/api/charts/symbols?query=NV&limit=20")

        self.assertEqual(candles.status_code, 200)
        self.assertTrue(candles.json()["simulation"])
        self.assertEqual(candles.json()["candles"][-1]["timestamp"], "2026-07-14T15:00:00Z")
        self.assertNotIn("2026-07-15T15:01:00Z", [item["timestamp"] for item in candles.json()["candles"]])
        self.assertEqual(symbols.json()["symbols"], [{"symbol": "NVDA"}])
        self.assertIn(("candles", "NVDA", "1m", 20), self.gateway.calls)
        self.assertIn(("symbols", "NV", 20), self.gateway.calls)

    def test_simulation_daily_chart_replaces_closed_history_with_one_live_market_day(self):
        self.gateway.mode = "simulation"
        self.gateway.candles = lambda symbol, interval, limit: {
            "symbol": symbol,
            "interval": interval,
            "simulation": True,
            "asOf": "2026-07-14T15:01:00Z",
            "candles": [{
                "timestamp": "2026-07-14T00:00:00Z",
                "open": 170.0,
                "high": 171.0,
                "low": 169.5,
                "close": 170.5,
                "volume": 1000,
                "isClosed": False,
            }],
        }
        historical = {
            "symbol": "NVDA",
            "interval": "1D",
            "candles": [
                {
                    "timestamp": "2026-07-13T04:00:00Z",
                    "close": 168.0,
                    "isClosed": True,
                },
                {
                    "timestamp": "2026-07-14T04:00:00Z",
                    "close": 169.0,
                    "isClosed": True,
                },
            ],
        }

        with patch(
            "app.routes.charts.get_query_service",
            return_value=SimpleNamespace(candle_snapshot=lambda *_args, **_kwargs: historical),
        ):
            response = self.client.get("/api/charts/candles?symbol=NVDA&interval=1D&limit=20")

        self.assertEqual(response.status_code, 200)
        candles = response.json()["candles"]
        self.assertEqual([item["timestamp"] for item in candles], [
            "2026-07-13T04:00:00.000Z",
            "2026-07-14T04:00:00.000Z",
        ])
        self.assertEqual(candles[-1]["close"], 170.5)
        self.assertFalse(candles[-1]["isClosed"])

    def test_simulation_ready_daily_chart_hides_the_overlapping_completed_market_day(self):
        self.gateway.mode = "simulation"
        self.gateway.candles = lambda symbol, interval, limit: {
            "symbol": symbol,
            "interval": interval,
            "simulation": True,
            "asOf": "2026-07-14T15:00:00Z",
            "candles": [],
        }
        historical = {
            "symbol": "NVDA",
            "interval": "1D",
            "candles": [
                {"timestamp": "2026-07-13T04:00:00Z", "close": 168.0, "isClosed": True},
                {"timestamp": "2026-07-14T04:00:00Z", "close": 169.0, "isClosed": True},
            ],
        }

        with patch(
            "app.routes.charts.get_query_service",
            return_value=SimpleNamespace(candle_snapshot=lambda *_args, **_kwargs: historical),
        ):
            response = self.client.get("/api/charts/candles?symbol=NVDA&interval=1D&limit=20")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["timestamp"] for item in response.json()["candles"]], [
            "2026-07-13T04:00:00.000Z",
        ])

    def test_simulation_order_history_and_trade_conditions_stay_in_run_ledger(self):
        self.gateway.mode = "simulation"

        submitted = self.client.post(
            "/api/orders",
            headers={"Idempotency-Key": "history-one"},
            json={"market": "overseas", "symbol": "GOOGL", "side": "buy", "qty": "1", "exchange": "NASD", "order_type": "market"},
        ).json()
        order_id = submitted["order_id"]
        order = self.client.get(f"/api/orders/{order_id}")
        events = self.client.get(f"/api/orders/{order_id}/events")
        listed = self.client.get("/api/trade-conditions")
        created = self.client.post(
            "/api/trade-conditions",
            json={
                "symbol": "NVDA",
                "side": "buy",
                "direction": "atOrAbove",
                "triggerPrice": "101",
                "limitPrice": "101",
                "quantity": 1,
            },
        )
        updated = self.client.patch("/api/trade-conditions/1", json={"status": "paused"})
        deleted = self.client.delete("/api/trade-conditions/1")

        self.assertEqual(order.status_code, 200)
        self.assertEqual([item["status"] for item in events.json()["events"]], ["accepted", "filled"])
        self.assertEqual(listed.json()["runId"], "run-1")
        self.assertEqual(created.status_code, 201)
        self.assertEqual(updated.json()["condition"]["status"], "paused")
        self.assertTrue(deleted.json()["deleted"])

    def test_simulation_latest_news_uses_the_replay_cursor(self):
        self.gateway.mode = "simulation"
        service = SimpleNamespace(latest_news=unittest.mock.Mock(return_value={
            "symbol": "NVDA",
            "source": "clickhouse-simulation",
            "items": [{
                "symbol": "NVDA",
                "title": "가상시각 이전 뉴스",
                "summary": "미래 뉴스는 포함하지 않습니다.",
                "publishedAt": "2026-07-14T14:32:44.000Z",
            }],
        }))

        with patch("app.market_data.query.routes.get_query_service", return_value=service):
            response = self.client.get("/api/market/news/latest?symbol=NVDA")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["title"], "가상시각 이전 뉴스")
        service.latest_news.assert_called_once_with(
            "NVDA",
            limit=10,
            locale="ko-KR",
            now=datetime(2026, 7, 14, 15, 0, tzinfo=timezone.utc),
        )

    def test_simulation_daily_news_uses_the_replay_cursor(self):
        self.gateway.mode = "simulation"
        service = SimpleNamespace(daily_news=unittest.mock.Mock(return_value={
            "symbol": "NVDA",
            "displayMode": "dailySummary",
            "dailySummaries": [{
                "date": "2026-07-13",
                "symbol": "NVDA",
                "summary": "가상시각 이전 일별 뉴스",
            }],
        }))

        with patch("app.market_data.query.routes.get_query_service", return_value=service):
            response = self.client.get("/api/market/news/daily?symbol=NVDA")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["dailySummaries"][0]["date"], "2026-07-13")
        service.daily_news.assert_called_once_with(
            "NVDA",
            limit=5,
            locale="ko-KR",
            now=datetime(2026, 7, 14, 15, 0, tzinfo=timezone.utc),
        )

    def test_simulation_analysis_assets_are_built_from_replay_safe_candles(self):
        self.gateway.mode = "simulation"
        self.gateway.virtual_time = "2026-07-15T02:00:00+09:00"
        replay_rows = [
            {
                "timestamp": f"2026-07-14T{15 + index // 60:02d}:{index % 60:02d}:00Z",
                "open": 100.0 + index / 100,
                "high": 101.0 + index / 100,
                "low": 99.0 + index / 100,
                "close": 100.5 + index / 100,
                "volume": 1_000 + index,
                "isClosed": True,
            }
            for index in range(120)
        ]
        self.gateway.candles = lambda symbol, interval, limit: {
            "symbol": symbol,
            "interval": interval,
            "simulation": True,
            "asOf": replay_rows[-1]["timestamp"],
            "candles": replay_rows,
        }
        stored_assets = {
            "1m": {
                "assetVersion": "geometry",
                "symbol": "NVDA",
                "interval": "1m",
                "sourceInterval": "1m",
                "asOf": "2026-07-14T20:00:00Z",
            },
            "5m": None,
            "10m": None,
            "1h": None,
            "4h": None,
            "1D": {
                "assetVersion": "geometry",
                "symbol": "NVDA",
                "interval": "1D",
                "sourceInterval": "1D",
                "asOf": "2026-07-14T04:00:00Z",
            },
            "1W": None,
        }
        analysis_result = {
            "drawings": [],
            "supports": [],
            "resistances": [],
            "patterns": [],
            "primaryPattern": None,
            "tradePlan": None,
            "primaryTriangle": None,
            "historicalTriangle": None,
            "evidence": [],
            "trends": [],
            "primaryTrend": None,
            "drawingGroups": {"levels": [], "trend": [], "pattern": []},
            "analysisTrace": {
                "version": "geometry-analysis-trace-v2",
                "completeness": {"complete": True, "detected": 0, "stored": 0},
            },
            "indicators": {
                "sma60": 100.5,
                "sma120": 100.25,
                "cross": {"status": "none", "direction": None, "timestamp": None, "barsAgo": None},
            },
        }
        storage = SimpleNamespace(get_symbol_assets=lambda _symbol: stored_assets)
        historical = {"symbol": "NVDA", "interval": "1m", "candles": []}

        with (
            patch("app.routes.chart_assets.chart_asset_storage", return_value=storage),
            patch(
                "app.routes.charts.get_query_service",
                return_value=SimpleNamespace(candle_snapshot=lambda *_args, **_kwargs: historical),
            ),
            patch("alfaka.analytics.geometry.analyze_geometry", return_value=analysis_result),
        ):
            stored_response = self.client.get("/api/charts/analysis-assets?symbol=NVDA")
            response = self.client.get("/api/charts/analysis-assets?symbol=NVDA&interval=1m")

        self.assertEqual(stored_response.status_code, 200)
        self.assertIsNone(stored_response.json()["assets"]["1m"])
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["assets"]["1m"]["asOf"], "2026-07-14T16:59:00.000Z")
        self.assertEqual(payload["assets"]["1m"]["coverage"]["actualBars"], 120)
        self.assertEqual(payload["assets"]["1D"]["asOf"], "2026-07-14T04:00:00Z")
        self.assertTrue(payload["meta"]["simulation"])
        self.assertEqual(payload["meta"]["cutoff"], "2026-07-15T02:00:00+09:00")
        self.assertEqual(payload["meta"]["dynamicInterval"], "1m")

        delete_response = self.client.delete("/api/charts/analysis-assets?symbols=NVDA&intervals=1m")
        self.assertEqual(delete_response.status_code, 409)

    def test_other_point_in_time_unsafe_market_data_stays_blocked(self):
        self.gateway.mode = "simulation"

        response = self.client.get("/api/market/fundamentals/NVDA/series")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "simulation_data_unavailable")

    def test_chart_events_use_the_replay_cursor_in_simulation_mode(self):
        self.gateway.mode = "simulation"
        service = SimpleNamespace(chart_events=unittest.mock.Mock(return_value={
            "symbol": "NVDA",
            "from": "2026-07-01T00:00:00.000Z",
            "to": "2026-07-31T23:59:59.000Z",
            "status": {"earnings": "empty", "news": "ready"},
            "earnings": [],
            "newsDays": [{"id": "news:NVDA:2026-07-14", "type": "news", "date": "2026-07-14"}],
            "upcomingEarnings": None,
        }))

        with patch("app.market_data.query.routes.get_query_service", return_value=service):
            response = self.client.get(
                "/api/charts/events",
                params={
                    "symbol": "NVDA",
                    "from": "2026-07-01T00:00:00Z",
                    "to": "2026-07-31T23:59:59Z",
                },
            )

        self.assertEqual(response.status_code, 200)
        service.chart_events.assert_called_once_with(
            "NVDA",
            "2026-07-01T00:00:00Z",
            "2026-07-31T23:59:59Z",
            locale="ko-KR",
            upcoming_days=90,
            now=datetime(2026, 7, 14, 15, 0, tzinfo=timezone.utc),
        )


if __name__ == "__main__":
    unittest.main()
