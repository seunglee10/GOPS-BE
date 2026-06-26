import unittest

from fastapi import HTTPException

import backend.app.main as main
import backend.app.services.ai_agents as ai_agents


class ChartRuntimeBackendTests(unittest.TestCase):
    def test_chart_candles_shape(self) -> None:
        payload = main.chart_candles(symbol="AAPL", interval="1m", ma="5,20,60", limit=35)

        self.assertEqual(payload["symbol"], "AAPL")
        self.assertEqual(payload["interval"], "1m")
        self.assertEqual(payload["source"], "dummy")
        self.assertEqual(payload["feed"], "synthetic-demo")
        self.assertTrue(payload["isSynthetic"])
        self.assertEqual(payload["indicators"], {"ma": [5, 20, 60], "volume": True})
        self.assertEqual(len(payload["candles"]), 35)
        self.assertTrue({"timestamp", "open", "high", "low", "close", "volume", "isClosed"}.issubset(payload["candles"][0]))
        self.assertIn("ma20", payload["candles"][20])

    def test_chart_candles_support_five_distinct_symbols(self) -> None:
        closes = {
            symbol: main.chart_candles(symbol=symbol, interval="1m", ma="5,20,60", limit=35)["candles"][-1]["close"]
            for symbol in main.supported_dummy_symbols()
        }

        self.assertEqual(set(closes), {"AAPL", "MSFT", "NVDA", "TSLA", "SPY"})
        self.assertEqual(len(set(closes.values())), 5)

    def test_dummy_symbols_have_distinct_price_paths(self) -> None:
        signatures = {}
        for symbol in main.supported_dummy_symbols():
            candles = main.build_dummy_candles(symbol, "1m", 48)
            deltas = [
                round(float(current["close"]) - float(previous["close"]), 2)
                for previous, current in zip(candles, candles[1:])
            ]
            signatures[symbol] = tuple(deltas[:16])

        self.assertEqual(len(set(signatures.values())), 5)

    def test_unknown_dummy_symbol_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as context:
            main.chart_candles(symbol="GOOG", interval="1m", ma="5,20,60", limit=35)
        self.assertEqual(context.exception.status_code, 400)

    def test_chart_symbols_shape(self) -> None:
        payload = main.chart_symbols()

        self.assertEqual(payload["source"], "dummy")
        self.assertEqual(payload["feed"], "synthetic-demo")
        self.assertEqual(len(payload["symbols"]), 5)
        self.assertTrue({"symbol", "name", "market", "lastPrice", "changePercent", "volume"}.issubset(payload["symbols"][0]))
        by_symbol = {item["symbol"]: item for item in payload["symbols"]}
        self.assertEqual(by_symbol["AAPL"]["market"], "NASDAQ")
        self.assertEqual(by_symbol["SPY"]["market"], "NYSEARCA")

    def test_corrected_live_candle_reuses_existing_timestamp(self) -> None:
        snapshot = main.build_dummy_candles("AAPL", "1m", 164)
        corrected = main.build_live_candle("AAPL", "1m", 163, "CANDLE_CORRECTED", 2)

        self.assertEqual(corrected["timestamp"], snapshot[-2]["timestamp"])
        self.assertTrue(corrected["isClosed"])

    def test_live_updates_mutate_current_open_candle_before_close(self) -> None:
        first_update = main.build_live_candle("NVDA", "1m", 159, "LIVE_CANDLE_UPDATE", 1)
        next_update = main.build_live_candle("NVDA", "1m", 159, "LIVE_CANDLE_UPDATE", 2)
        closed = main.build_live_candle("NVDA", "1m", 159, "CANDLE_CLOSED", 5)

        self.assertEqual(first_update["timestamp"], next_update["timestamp"])
        self.assertEqual(next_update["timestamp"], closed["timestamp"])
        self.assertNotEqual(first_update["close"], next_update["close"])
        self.assertFalse(first_update["isClosed"])
        self.assertTrue(closed["isClosed"])

    def test_live_candle_high_low_only_expand_while_open(self) -> None:
        updates = [
            main.build_live_candle("TSLA", "1m", 159, "LIVE_CANDLE_UPDATE", step)
            for step in range(1, 5)
        ]

        self.assertEqual(len({candle["open"] for candle in updates}), 1)
        for previous, current in zip(updates, updates[1:]):
            self.assertGreaterEqual(current["high"], previous["high"])
            self.assertLessEqual(current["low"], previous["low"])
            self.assertGreaterEqual(current["high"], max(current["open"], current["close"]))
            self.assertLessEqual(current["low"], min(current["open"], current["close"]))

    def test_openai_missing_key_returns_503(self) -> None:
        original = ai_agents.read_dotenv_value

        def without_key(name: str) -> str | None:
            if name == "OPENAI_API_KEY":
                return None
            return original(name)

        ai_agents.read_dotenv_value = without_key
        try:
            with self.assertRaises(HTTPException) as context:
                main.openai_chart_proposal({})
            self.assertEqual(context.exception.status_code, 503)
        finally:
            ai_agents.read_dotenv_value = original

    def test_fallback_agent_chat_returns_chart_commands(self) -> None:
        request = main.AgentChatRequest(
            agentIds=["agent-01"],
            messages=[main.AgentChatMessage(role="user", content="NVDA 5m로 바꾸고 확대해줘")],
            context={},
        )
        payload = main.fallback_agent_chat(request)

        self.assertIn("reply", payload)
        self.assertGreaterEqual(len(payload["commands"]), 2)
        self.assertIn("chart.symbol.set", [command["type"] for command in payload["commands"]])
        self.assertIn("chart.timeframe.set", [command["type"] for command in payload["commands"]])


if __name__ == "__main__":
    unittest.main()
