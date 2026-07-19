import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
ORDER_SHARED = ROOT / "systems" / "order" / "shared"
ORDER_TEST_ROOT = ROOT / "systems" / "order"
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend"
for path in (str(MARKET_SHARED), str(ORDER_SHARED), str(ORDER_TEST_ROOT), str(BACKEND), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from fastapi.testclient import TestClient

from app.company_journal.routes import get_company_journal_service
from app.main import create_app
from app.services.simulator_gateway import SimulatorTimeout, SimulatorUnavailable
from app.alerts.repository import InMemoryAlertRepository
from app.trade_conditions.repository import InMemoryTradeConditionRepository
from kis_trader.persistence.memory import InMemoryOrderRepository
from kis_trader.paper.fixture import SEED_PROFILE
from kis_trader.paper.memory import InMemoryPaperTradingRepository
from systems.order.tests.kis_trader.fixtures.orders import sample_order_request


class FakeSimulatorGateway:
    def __init__(self, trace=None):
        self.mode = "live"
        self.state = "idle"
        self.calls = []
        self.trace = trace
        self.virtual_time = "2026-07-15T00:00:00+09:00"

    def status(self):
        return {
            "available": True,
            "mode": self.mode,
            "state": self.state,
            "datasetId": "sp500-full-20260715-kst-v3",
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
        self.state = "ready" if mode == "simulation" else "idle"
        self.calls.append(("mode", mode))
        if self.trace is not None:
            self.trace.append(("gateway", mode))
        return self.status()

    def action(self, action):
        self.calls.append(("action", action))
        if action == "start":
            self.mode = "simulation"
            self.state = "running"
        return self.status()

    def set_speed(self, speed):
        self.calls.append(("speed", speed))
        return {**self.status(), "requestedSpeed": speed}

    def quote(self, symbol):
        self.calls.append(("quote", symbol))
        return {"symbol": symbol, "bid": 99.0, "ask": 100.0, "runId": "run-1"}

    def quotes(self, symbols):
        normalized = tuple(sorted(str(symbol).upper() for symbol in symbols))
        self.calls.append(("quotes", normalized))
        return {
            "runId": "run-1",
            "quotes": {
                symbol: {
                    "symbol": symbol,
                    "bid": 99.0,
                    "ask": 100.0,
                    "virtualTime": self.virtual_time,
                }
                for symbol in normalized
            },
            "missingSymbols": [],
        }

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

    def indices(self):
        self.calls.append(("indices",))
        return {
            "source": "simulation_replay",
            "cacheStatus": "fresh",
            "warning": None,
            "updatedAt": "2026-07-15T00:00:00+09:00",
            "refreshSeconds": 60,
            "staleRefreshSeconds": 300,
            "period": "2026-07-15-kst",
            "interval": "snapshot",
            "coverage": {"total": 1, "priced": 1, "missing": []},
            "items": [{
                "symbol": "^GSPC",
                "name": "S&P 500",
                "assetClass": "equity_index",
                "group": "US",
                "currency": "USD",
                "unit": "points",
                "price": 6300.0,
                "open": 6290.0,
                "high": 6310.0,
                "low": 6280.0,
                "previousClose": 6285.0,
                "change": 15.0,
                "changePercent": 0.24,
                "sparkline": [6285.0, 6300.0],
                "updatedAt": "2026-07-15T00:00:00+09:00",
                "status": "ok",
            }],
            "datasetId": "sp500-full-20260715-kst-v3",
            "simulation": True,
            "runId": "run-1",
            "virtualTime": self.virtual_time,
        }

    def index_performance(self, range_value, start_at):
        self.calls.append(("index-performance", range_value, start_at))
        return {
            "symbol": "^GSPC",
            "name": "S&P 500",
            "method": "price_return",
            "source": "simulation_replay",
            "range": range_value,
            "asOf": "2026-07-14T14:55:00Z",
            "points": [
                {"time": "2026-07-14T14:00:00Z", "returnPercent": 0},
                {"time": "2026-07-14T14:55:00Z", "returnPercent": 0.24},
            ],
            "simulation": True,
            "virtualTime": self.virtual_time,
        }

    def order_flow(self, symbol):
        self.calls.append(("order-flow", symbol))
        return {
            "symbol": symbol,
            "sessionDate": "2026-07-14",
            "priceBinSize": 0.01,
            "sideClassification": "estimated",
            "classificationVersion": "orderflow-estimated-v2",
            "marketSession": "regular",
            "dataStatus": "ready",
            "minutes": [{
                "eventMinute": "2026-07-14T15:00:00Z",
                "updatedAt": "2026-07-14T15:00:10.000Z",
                "bins": [{"priceBin": 100.0, "askVolume": 10, "bidVolume": 2, "unknownVolume": 0}],
            }],
            "liveQuote": {"bidPrice": 99.9, "askPrice": 100.0, "timestamp": "2026-07-14T15:00:10.000Z"},
            "supportedSymbols": ["NVDA"],
            "source": "simulation_replay",
            "simulation": True,
            "datasetId": "sp500-full-20260715-kst-v3",
            "runId": "run-1",
            "virtualTime": self.virtual_time,
        }

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


class RecordingReplayDerivedService:
    def __init__(self):
        self.candle_calls = []
        self.indicator_calls = []
        self.volume_profile_calls = []

    def candle_snapshot(
        self,
        symbol,
        interval,
        ma,
        limit,
        before=None,
        from_time=None,
        to_time=None,
        include_previous_close=False,
    ):
        self.candle_calls.append({
            "symbol": symbol,
            "interval": interval,
            "ma": ma,
            "limit": limit,
            "before": before,
            "fromTime": from_time,
            "toTime": to_time,
            "includePreviousClose": include_previous_close,
        })
        return {
            "symbol": symbol.upper(),
            "interval": interval,
            "source": "clickhouse",
            "feed": "sip",
            "indicators": {"ma": [5] if ma == "5" else [], "volume": True},
            "candles": [
                {
                    "timestamp": f"2026-07-14T14:5{index}:00Z",
                    "open": float(index + 1),
                    "high": float(index + 1),
                    "low": float(index + 1),
                    "close": float(index + 1),
                    "volume": 100.0,
                    "isClosed": True,
                }
                for index in range(4)
            ],
        }

    def indicator_series(
        self,
        symbol,
        interval,
        from_time=None,
        to_time=None,
        layers=None,
        limit=None,
        *,
        candle_payload=None,
        cache_scope=None,
    ):
        self.indicator_calls.append({
            "symbol": symbol,
            "interval": interval,
            "fromTime": from_time,
            "toTime": to_time,
            "layers": layers,
            "limit": limit,
            "candlePayload": candle_payload,
            "cacheScope": cache_scope,
        })
        replay_timestamp = candle_payload["candles"][-1]["timestamp"]
        return {
            "symbol": symbol.upper(),
            "interval": interval,
            "series": {
                "bollinger:20:2": [{
                    "timestamp": replay_timestamp,
                    "middle": 100.0,
                    "upper": 102.0,
                    "lower": 98.0,
                }],
            },
            "derived": {"state": "ready", "source": "api-compute"},
        }

    def volume_profile_bins(
        self,
        symbol,
        from_time,
        to_time,
        price_bin_size,
        target_bins=10,
        price_min=None,
        price_max=None,
        interval="1m",
        candle_count=None,
        *,
        candle_payload=None,
        cache_scope=None,
    ):
        self.volume_profile_calls.append({
            "symbol": symbol,
            "interval": interval,
            "fromTime": from_time,
            "toTime": to_time,
            "candleCount": candle_count,
            "candlePayload": candle_payload,
            "cacheScope": cache_scope,
        })
        return {
            "symbol": symbol.upper(),
            "interval": interval,
            "from": from_time,
            "to": to_time,
            "targetBins": target_bins,
            "bucketCount": target_bins,
            "priceBinSize": 1.0,
            "sourceCandleCount": candle_count,
            "requestedCandleCount": candle_count,
            "totalVolume": 500.0,
            "dataStatus": "ready",
            "bins": [{"index": index, "volume": 50.0} for index in range(target_bins)],
            "derived": {"state": "ready", "source": "api-compute"},
        }


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

    def test_play_action_prepares_and_starts_replay_from_live(self):
        response = self.client.post("/api/simulator/action", json={"action": "start"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "simulation")
        self.assertEqual(response.json()["state"], "running")
        self.assertEqual(self.gateway.calls, [("action", "start")])

    def test_operator_can_change_replay_speed(self):
        self.gateway.mode = "simulation"

        for speed in (1, 2, 5, 10):
            response = self.client.put("/api/simulator/speed", json={"speed": speed})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["requestedSpeed"], speed)
            self.assertIn(("speed", speed), self.gateway.calls)

        for removed_speed in (20, 60, 300):
            removed = self.client.put("/api/simulator/speed", json={"speed": removed_speed})
            self.assertEqual(removed.status_code, 422)

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

    def test_simulation_order_flow_symbols_and_intraday_use_replay_gateway(self):
        self.gateway.mode = "simulation"

        symbols = self.client.get("/api/charts/order-flow/symbols")
        intraday = self.client.get("/api/charts/order-flow/intraday?symbol=NVDA")
        daily = self.client.get("/api/charts/order-flow/daily?symbol=NVDA&from=2026-07-14&to=2026-07-14")

        self.assertEqual(symbols.status_code, 200)
        self.assertEqual(symbols.json()["symbols"], ["NVDA"])
        self.assertTrue(symbols.json()["simulation"])
        self.assertEqual(intraday.status_code, 200)
        self.assertEqual(intraday.json()["sessionDate"], "2026-07-14")
        self.assertEqual(intraday.json()["minutes"][0]["bins"][0]["askVolume"], 10)
        self.assertIn(("order-flow", "NVDA"), self.gateway.calls)
        self.assertEqual(daily.status_code, 409)
        self.assertEqual(daily.json()["detail"], "simulation_data_unavailable")

    def test_simulation_indicators_use_replay_safe_candles(self):
        self.gateway.mode = "simulation"
        service = RecordingReplayDerivedService()

        with patch("app.market_data.query.routes.get_query_service", return_value=service):
            response = self.client.get(
                "/api/charts/indicators",
                params={
                    "symbol": "NVDA",
                    "interval": "1m",
                    "from": "2026-07-14T14:50:00Z",
                    "to": "2026-07-14T15:00:00Z",
                    "layers": "bollinger:20:2",
                    "limit": 30,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("bollinger:20:2", response.json()["series"])
        self.assertEqual(len(service.indicator_calls), 1)
        indicator_call = service.indicator_calls[0]
        self.assertTrue(indicator_call["candlePayload"]["simulation"])
        self.assertEqual(indicator_call["candlePayload"]["candles"][-1]["timestamp"], "2026-07-14T15:00:00Z")
        self.assertEqual(indicator_call["cacheScope"], "simulation:sp500-full-20260715-kst-v3:run-1")
        self.assertLessEqual(service.candle_calls[0]["toTime"], "2026-07-14T15:00:00Z")

    def test_simulation_volume_profile_uses_replay_safe_candles(self):
        self.gateway.mode = "simulation"
        service = RecordingReplayDerivedService()

        with patch("app.market_data.query.routes.get_query_service", return_value=service):
            response = self.client.get(
                "/api/charts/volume-profile-bins",
                params={
                    "symbol": "NVDA",
                    "interval": "1m",
                    "from": "2026-07-14T14:50:00Z",
                    "to": "2026-07-14T15:00:00Z",
                    "targetBins": 10,
                    "candleCount": 5,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["bucketCount"], 10)
        self.assertEqual(len(service.volume_profile_calls), 1)
        profile_call = service.volume_profile_calls[0]
        self.assertTrue(profile_call["candlePayload"]["simulation"])
        self.assertEqual(profile_call["candlePayload"]["candles"][-1]["timestamp"], "2026-07-14T15:00:00Z")
        self.assertEqual(profile_call["cacheScope"], "simulation:sp500-full-20260715-kst-v3:run-1")

    def test_simulation_daily_indicators_include_the_completed_replay_market_day(self):
        self.gateway.mode = "simulation"
        self.gateway.virtual_time = "2026-07-15T13:00:00+09:00"
        self.gateway.candles = Mock(return_value={
            "symbol": "NVDA",
            "interval": "1D",
            "source": "simulation_replay",
            "feed": "sip+boats",
            "simulation": True,
            "asOf": "2026-07-15T04:00:00Z",
            "candles": [{
                "timestamp": "2026-07-14T04:00:00.000Z",
                "open": 100.0,
                "high": 110.0,
                "low": 99.0,
                "close": 108.0,
                "volume": 1_000.0,
                "isClosed": True,
            }],
        })
        service = RecordingReplayDerivedService()

        with patch("app.market_data.query.routes.get_query_service", return_value=service):
            response = self.client.get(
                "/api/charts/indicators",
                params={
                    "symbol": "NVDA",
                    "interval": "1D",
                    "from": "2026-07-01T04:00:00Z",
                    "to": "2026-07-14T04:00:00Z",
                    "layers": "bollinger:20:2",
                    "limit": 30,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.gateway.candles.assert_called_once_with("NVDA", "1D", 5_000)
        self.assertEqual(
            service.indicator_calls[0]["candlePayload"]["candles"][-1]["timestamp"],
            "2026-07-14T04:00:00.000Z",
        )

    def test_simulation_candles_recompute_requested_moving_averages_across_replay_boundary(self):
        self.gateway.mode = "simulation"
        service = RecordingReplayDerivedService()

        with patch("app.routes.charts.get_query_service", return_value=service):
            response = self.client.get("/api/charts/candles?symbol=NVDA&interval=1m&limit=10&ma=5")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["indicators"], {"ma": [5], "volume": True})
        self.assertEqual(payload["candles"][-1]["timestamp"], "2026-07-14T15:00:00Z")
        self.assertEqual(payload["candles"][-1]["ma5"], 22.0)

    def test_simulation_market_indices_use_the_fixed_replay_snapshot(self):
        self.gateway.mode = "simulation"

        response = self.client.get("/api/market/indices")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["source"], "simulation_replay")
        self.assertEqual(payload["period"], "2026-07-15-kst")
        self.assertEqual(payload["items"][0]["symbol"], "^GSPC")
        self.assertEqual(payload["virtualTime"], "2026-07-15T00:00:00+09:00")
        self.assertIn(("indices",), self.gateway.calls)

    def test_simulation_related_indices_use_the_fixed_replay_snapshot(self):
        self.gateway.mode = "simulation"
        status = self.gateway.status()
        self.gateway.status = Mock(return_value={
            **status,
            "symbols": [{"symbol": "AAPL", "price": 201.0, "changePercent": 1.25}],
        })

        response = self.client.get("/api/market/indices/related?symbol=AAPL")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["source"], "simulation_replay_related")
        self.assertTrue(payload["simulation"])
        self.assertEqual(payload["runId"], "run-1")
        self.assertEqual(payload["virtualTime"], "2026-07-15T00:00:00+09:00")
        self.assertEqual(payload["items"][0]["symbol"], "^GSPC")
        self.assertEqual(payload["items"][0]["companyChangePercent"], 1.25)
        self.assertIsNone(payload["items"][0]["correlation60d"])
        self.assertIn(("indices",), self.gateway.calls)

    def test_simulation_holdings_use_shared_diversified_paper_account(self):
        self.gateway.mode = "simulation"

        response = self.client.get("/api/account/holdings?market=overseas&currency=USD")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["source"], "paper-shared")
        self.assertEqual(
            {item["symbol"] for item in payload["positions"]},
            {"GOOGL", "MSFT", "JPM", "XOM", "JNJ", "COST", "HD", "AAPL", "AMZN", "WMT"},
        )
        self.assertIn("7섹터", payload["account"]["alias"])
        self.assertEqual(payload["asOf"], "2026-07-15T00:00:00+09:00")
        quote_calls = [call for call in self.gateway.calls if call[0] in {"quote", "quotes"}]
        self.assertEqual(len(quote_calls), 1)
        self.assertEqual(quote_calls[0][0], "quotes")

    def test_simulation_holdings_do_not_fall_back_to_non_replay_prices(self):
        self.gateway.mode = "simulation"
        self.gateway.quotes = Mock(return_value={
            "runId": "run-1",
            "quotes": {},
            "missingSymbols": ["GOOGL"],
        })
        live_price_resolver = Mock(return_value={
            "price": "999.00",
            "source": "redis.live_trade",
            "timestamp": "2026-07-18T00:00:00Z",
        })
        self.app.state.paper_price_resolver = live_price_resolver

        response = self.client.get("/api/account/holdings?market=overseas&currency=USD")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "simulation_quote_not_ready")
        self.assertIn("GOOGL", response.json()["detail"]["symbols"])
        live_price_resolver.assert_not_called()

    def test_simulation_holdings_report_quote_timeout_as_retryable_gateway_failure(self):
        self.gateway.mode = "simulation"
        self.gateway.quotes = Mock(side_effect=SimulatorTimeout("timed out"))

        response = self.client.get("/api/account/holdings?market=overseas&currency=USD")

        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["detail"]["code"], "simulation_quote_timeout")
        self.assertTrue(response.json()["detail"]["retryable"])

    def test_simulation_holdings_report_simulator_failure_separately_from_missing_quotes(self):
        self.gateway.mode = "simulation"
        self.gateway.quotes = Mock(side_effect=SimulatorUnavailable("connection refused"))

        response = self.client.get("/api/account/holdings?market=overseas&currency=USD")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "simulation_service_unavailable")

    def test_last_known_simulation_mode_never_falls_back_to_live_prices_on_status_failure(self):
        self.gateway.last_status = {"mode": "simulation", "runId": "run-1"}
        self.gateway.status = Mock(side_effect=SimulatorUnavailable("connection refused"))
        self.gateway.quotes = Mock(side_effect=SimulatorUnavailable("connection refused"))
        live_price_resolver = Mock(return_value={"price": "999.00", "source": "redis.live_trade"})
        self.app.state.paper_price_resolver = live_price_resolver

        response = self.client.get("/api/account/holdings?market=overseas&currency=USD")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "simulation_service_unavailable")
        live_price_resolver.assert_not_called()

    def test_simulation_holdings_skip_live_market_enrichment(self):
        self.gateway.mode = "simulation"

        with (
            patch(
                "app.routes.paper_trading.enrich_holdings_with_market_stats",
                side_effect=lambda _app, payload: payload,
            ) as market_stats,
            patch(
                "app.routes.account.enrich_holdings_with_alpaca_dividends",
                side_effect=lambda payload: payload,
            ) as dividends,
        ):
            response = self.client.get("/api/account/holdings?market=overseas&currency=USD")

        self.assertEqual(response.status_code, 200)
        market_stats.assert_not_called()
        dividends.assert_not_called()

    def test_simulation_performance_uses_virtual_time_and_excludes_future_snapshots(self):
        self.gateway.mode = "simulation"
        read_snapshots = Mock(return_value=[
            {
                "source_as_of": "2026-07-14T14:00:00Z",
                "payload": {
                    "source": "paper-shared",
                    "asOf": "2026-07-14T14:00:00Z",
                    "account": {"totalValueForeign": 100_000, "unrealizedPnlRate": 0},
                    "positions": [],
                },
            },
            {
                "source_as_of": "2026-07-14T16:00:00Z",
                "payload": {
                    "source": "paper-shared",
                    "asOf": "2026-07-14T16:00:00Z",
                    "account": {"totalValueForeign": 999_999, "unrealizedPnlRate": 900},
                    "positions": [],
                },
            },
        ])
        self.app.state.recommendation_repository = SimpleNamespace(
            list_daily_portfolio_snapshots_for_sources=read_snapshots,
        )
        self.app.state.portfolio_performance_now_provider = lambda: datetime(2030, 1, 1, tzinfo=timezone.utc)
        benchmark_provider = Mock(return_value={"symbol": "^GSPC", "points": []})
        self.app.state.portfolio_benchmark_provider = benchmark_provider

        response = self.client.get("/api/account/performance?range=1W")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(read_snapshots.call_args.args[1], "2026-07-07T15:00:00+00:00")
        self.assertEqual(response.json()["asOf"], "2026-07-14T14:00:00Z")
        self.assertEqual(len(response.json()["portfolio"]["points"]), 1)
        self.assertEqual(len(response.json()["benchmark"]["points"]), 2)
        self.assertIn(
            ("index-performance", "1W", datetime(2026, 7, 14, 14, 0, tzinfo=timezone.utc)),
            self.gateway.calls,
        )
        benchmark_provider.assert_not_called()

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
        storage = SimpleNamespace(
            get=lambda _symbol, interval: stored_assets.get(interval),
            get_symbol_assets=lambda _symbol: stored_assets,
            get_commentary=lambda _symbol, interval: {
                "assetVersion": "geometry",
                "algorithmVersion": "ohlcv-consensus-pattern-families-v6",
                "asOf": stored_assets[interval]["asOf"],
                "generatedAt": "2026-07-14T21:00:00Z",
                "inputDigest": f"sha256:{interval}",
                "drawingIds": [],
                "commentary": None,
            } if stored_assets.get(interval) else None,
        )
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
            future_commentary = self.client.get("/api/charts/analysis-assets/commentary?symbol=NVDA&interval=1m")
            safe_commentary = self.client.get("/api/charts/analysis-assets/commentary?symbol=NVDA&interval=1D")

        self.assertEqual(stored_response.status_code, 200)
        self.assertIsNone(stored_response.json()["assets"]["1m"])
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["assets"]["1m"]["asOf"], "2026-07-14T16:59:00.000Z")
        self.assertEqual(payload["assets"]["1m"]["coverage"]["actualBars"], 120)
        self.assertEqual(list(payload["assets"]), ["1m"])
        self.assertTrue(payload["meta"]["simulation"])
        self.assertEqual(payload["meta"]["cutoff"], "2026-07-15T02:00:00+09:00")
        self.assertEqual(payload["meta"]["dynamicInterval"], "1m")
        self.assertEqual(future_commentary.status_code, 200)
        self.assertIsNone(future_commentary.json()["asset"])
        self.assertEqual(safe_commentary.status_code, 200)
        self.assertEqual(safe_commentary.json()["asset"]["asOf"], "2026-07-14T04:00:00Z")
        self.assertTrue(safe_commentary.json()["meta"]["simulation"])

        delete_response = self.client.delete("/api/charts/analysis-assets?symbols=NVDA&intervals=1m")
        self.assertEqual(delete_response.status_code, 409)

    def test_simulation_nvda_daily_asset_uses_demo_falling_wedge_dated_to_july_14(self):
        self.gateway.mode = "simulation"
        stored_asset = {
            "assetVersion": "geometry",
            "algorithmVersion": "ohlcv-consensus-pattern-families-v6",
            "symbol": "NVDA",
            "interval": "1D",
            "sourceInterval": "1D",
            "asOf": "2026-07-16T04:00:00.000Z",
            "generatedAt": "2026-07-17T14:27:53.472Z",
            "status": "ready",
            "commentary": {"status": "ready", "paragraphs": [{"text": "7월 16일 해설"}]},
            "geometry": {
                "drawings": [
                    {
                        "id": "wedge-upper",
                        "type": "trendLine",
                        "label": "하락 쐐기 · 돌파 확인",
                        "anchors": [
                            {"timestamp": "2026-05-27T04:00:00.000Z", "price": 237.90},
                            {"timestamp": "2026-07-16T04:00:00.000Z", "price": 190.07},
                        ],
                        "createdAt": "2026-07-16T04:00:00.000Z",
                        "updatedAt": "2026-07-16T04:00:00.000Z",
                    },
                    {
                        "id": "wedge-lower",
                        "type": "trendLine",
                        "label": "하락 쐐기 · 돌파 확인",
                        "anchors": [
                            {"timestamp": "2026-05-27T04:00:00.000Z", "price": 208.12},
                            {"timestamp": "2026-07-16T04:00:00.000Z", "price": 179.10},
                        ],
                        "createdAt": "2026-07-16T04:00:00.000Z",
                        "updatedAt": "2026-07-16T04:00:00.000Z",
                    },
                ],
                "drawingGroups": {"levels": [], "trend": [], "pattern": ["wedge-upper", "wedge-lower"]},
                "patterns": [{
                    "id": "nvda-falling-wedge",
                    "kind": "falling_wedge",
                    "state": "confirmed",
                    "upper": {
                        "start": {"timestamp": "2026-05-27T04:00:00.000Z", "price": 237.90},
                        "end": {"timestamp": "2026-07-16T04:00:00.000Z", "price": 190.07},
                    },
                    "lower": {
                        "start": {"timestamp": "2026-05-27T04:00:00.000Z", "price": 208.12},
                        "end": {"timestamp": "2026-07-16T04:00:00.000Z", "price": 179.10},
                    },
                    "confirmation": {
                        "breakoutAt": "2026-07-15T04:00:00.000Z",
                        "confirmedAt": "2026-07-16T04:00:00.000Z",
                    },
                }],
                "primaryPattern": {"id": "nvda-falling-wedge", "kind": "falling_wedge"},
                "tradePlan": {
                    "patternId": "nvda-falling-wedge",
                    "patternKind": "falling_wedge",
                    "signalAt": "2026-07-15T04:00:00.000Z",
                },
            },
        }
        dynamic_asset = {
            "assetVersion": "geometry",
            "algorithmVersion": "ohlcv-consensus-pattern-families-v6",
            "symbol": "NVDA",
            "interval": "1D",
            "sourceInterval": "1D",
            "asOf": "2026-07-13T04:00:00.000Z",
            "generatedAt": "2026-07-15T00:00:00.000Z",
            "status": "ready",
            "geometry": {
                "drawings": [{"id": "safe-level", "type": "horizontalLine"}],
                "drawingGroups": {"levels": ["safe-level"], "trend": [], "pattern": []},
                "patterns": [],
                "primaryPattern": None,
                "tradePlan": None,
            },
        }
        storage = SimpleNamespace(get=lambda _symbol, _interval: stored_asset)

        with (
            patch("app.routes.chart_assets.chart_asset_storage", return_value=storage),
            patch("app.routes.chart_assets._build_simulation_analysis_asset", return_value=dynamic_asset),
        ):
            response = self.client.get("/api/charts/analysis-assets?symbol=NVDA&interval=1D")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        asset = payload["assets"]["1D"]
        self.assertEqual(asset["asOf"], "2026-07-14T04:00:00.000Z")
        self.assertEqual(asset["geometry"]["primaryPattern"]["kind"], "falling_wedge")
        self.assertEqual(asset["geometry"]["patterns"][0]["confirmation"]["breakoutAt"], "2026-07-14T04:00:00.000Z")
        self.assertEqual(asset["geometry"]["patterns"][0]["confirmation"]["confirmedAt"], "2026-07-14T04:00:00.000Z")
        self.assertEqual(asset["geometry"]["drawings"][-1]["anchors"][-1]["timestamp"], "2026-07-14T04:00:00.000Z")
        self.assertNotIn("commentary", asset)
        self.assertTrue(payload["meta"]["demoOverride"])
        self.assertEqual(payload["meta"]["demoOverrideAsOf"], "2026-07-14T04:00:00.000Z")

    def test_other_point_in_time_unsafe_market_data_stays_blocked(self):
        self.gateway.mode = "simulation"

        response = self.client.get("/api/market/fundamentals/NVDA/series")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "simulation_data_unavailable")

    def test_ai_coach_latest_report_remains_available_in_simulation(self):
        self.gateway.mode = "simulation"
        expected = {
            "contractVersion": "coach-report.v2",
            "analysisId": "analysis-1",
            "page1": None,
        }

        with patch("app.routes.agents.CoachReportArchive") as archive_type:
            archive_type.return_value.get_latest.return_value = expected
            response = self.client.get("/api/ai-coach/reports/latest")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ready", "report": expected})
        archive_type.return_value.get_latest.assert_called_once_with(user_id="dev-auth-disabled")

    def test_company_journal_uses_simulator_virtual_time_without_live_enqueue(self):
        self.gateway.mode = "simulation"
        service = SimpleNamespace(
            latest=Mock(return_value={
                "analysisAsOf": "2026-07-13",
                "sourceMode": "historical_reconstruction",
                "sourceCutoff": "2026-07-14T15:00:00+00:00",
            }),
            enqueue_if_stale=Mock(),
        )
        self.app.dependency_overrides[get_company_journal_service] = lambda: service

        response = self.client.get("/api/company-journal/NVDA")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")
        self.assertEqual(response.json()["report"]["analysisAsOf"], "2026-07-13")
        cutoff = service.latest.call_args.kwargs["cutoff"]
        self.assertEqual(cutoff.isoformat(), "2026-07-14T15:00:00+00:00")
        service.enqueue_if_stale.assert_not_called()

    def test_fixed_recommendations_are_available_for_the_entire_simulation(self):
        self.gateway.mode = "simulation"
        with patch.dict(os.environ, {
            "RECOMMENDATION_FIXED_REPLAY_ENABLED": "false",
            "RECOMMENDATION_DECISION_V1_ENABLED": "false",
        }):
            before_cutoff = self.client.get("/api/recommendations/stocks/latest")
            refreshed = self.client.post("/api/recommendations/stocks/refresh", json={})
            self.gateway.virtual_time = "2026-07-15T05:00:00+09:00"
            at_cutoff = self.client.get("/api/recommendations/stocks/latest")

        self.assertEqual(before_cutoff.status_code, 200)
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(at_cutoff.status_code, 200)
        self.assertEqual(len(before_cutoff.json()["items"]), 15)
        self.assertEqual(
            before_cutoff.json()["recommendationDigest"],
            refreshed.json()["recommendationDigest"],
        )
        self.assertEqual(
            before_cutoff.json()["recommendationDigest"],
            at_cutoff.json()["recommendationDigest"],
        )
        self.assertEqual(before_cutoff.json()["evidenceAsOf"], "2026-07-14T16:00:00-04:00")
        self.assertEqual(at_cutoff.json()["evidenceAsOf"], "2026-07-14T16:00:00-04:00")

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
