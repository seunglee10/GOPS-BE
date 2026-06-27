import io
import json
import urllib.error
import unittest
from pathlib import Path

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

    def test_agent_chat_missing_key_returns_503_even_when_mock_env_is_set(self) -> None:
        original = ai_agents.read_dotenv_value

        def without_key(name: str) -> str | None:
            if name == "OPENAI_API_KEY":
                return None
            if name == "GOPS_USE_MOCK_LLM":
                return "1"
            return original(name)

        ai_agents.read_dotenv_value = without_key
        request = main.AgentChatRequest(
            agentIds=["agent-01"],
            messages=[main.AgentChatMessage(role="user", content="차트를 분석해줘")],
            context={},
        )
        try:
            with self.assertRaises(HTTPException) as context:
                main.agent_chat(request)
            self.assertEqual(context.exception.status_code, 503)
            self.assertEqual(context.exception.detail, "OpenAI API key is not configured.")
        finally:
            ai_agents.read_dotenv_value = original

    def test_openai_agent_chat_analysis_uses_multi_timeframe_context_and_requires_command(self) -> None:
        original_key_reader = ai_agents.read_dotenv_value
        original_request = ai_agents.request_openai_response
        captured_payloads = []

        def fake_key_reader(name: str) -> str | None:
            if name == "OPENAI_API_KEY":
                return "test-key"
            if name == "OPENAI_MODEL":
                return "test-model"
            return original_key_reader(name)

        def fake_request(payload: dict) -> str:
            captured_payloads.append(payload)
            return json.dumps({
                "reply": "분석 응답",
                "title": "분석",
                "summary": "요약",
                "rationale": "근거",
                "commands": [],
                "insights": [],
            })

        ai_agents.read_dotenv_value = fake_key_reader
        ai_agents.request_openai_response = fake_request
        request = main.AgentChatRequest(
            agentIds=["agent-01"],
            messages=[main.AgentChatMessage(role="user", content="차트를 분석해줘")],
            context={
                "chartDocument": {
                    "id": "doc-a",
                    "symbol": "AAPL",
                    "timeframe": "1m",
                    "viewport": {"visibleCount": 80, "rightOffset": 0},
                    "layers": {"ma5": True, "ma20": True, "ma60": True},
                },
                "visibleSummary": {"lastPrice": "138.16"},
            },
        )
        try:
            main.openai_agent_chat(request)
        finally:
            ai_agents.read_dotenv_value = original_key_reader
            ai_agents.request_openai_response = original_request

        payload = captured_payloads[0]
        commands_schema = payload["text"]["format"]["schema"]["properties"]["commands"]
        self.assertEqual(commands_schema["minItems"], 1)

        user_payload = json.loads(payload["input"][1]["content"])
        self.assertTrue(user_payload["isChartAnalysisRequest"])
        context = user_payload["marketAnalysisContext"]
        self.assertEqual(context["symbol"], "AAPL")
        self.assertEqual(set(context["timeframes"].keys()), {"1m", "5m", "10m"})
        self.assertIn("activeView", context)
        self.assertGreaterEqual(len(context["suggestedAnchors"]), 9)
        self.assertNotIn("AAPL", context["comparisonCandidates"])

    def test_openai_agent_chat_non_analysis_allows_empty_commands(self) -> None:
        original_key_reader = ai_agents.read_dotenv_value
        original_request = ai_agents.request_openai_response
        captured_payloads = []

        def fake_key_reader(name: str) -> str | None:
            if name == "OPENAI_API_KEY":
                return "test-key"
            return original_key_reader(name)

        def fake_request(payload: dict) -> str:
            captured_payloads.append(payload)
            return json.dumps({
                "reply": "안녕하세요",
                "title": "대화",
                "summary": "명령 없음",
                "rationale": "차트 조작 요청이 아님",
                "commands": [],
                "insights": [],
            })

        ai_agents.read_dotenv_value = fake_key_reader
        ai_agents.request_openai_response = fake_request
        try:
            main.openai_agent_chat(main.AgentChatRequest(
                agentIds=["agent-01"],
                messages=[main.AgentChatMessage(role="user", content="안녕")],
                context={"chartDocument": {"symbol": "AAPL"}},
            ))
        finally:
            ai_agents.read_dotenv_value = original_key_reader
            ai_agents.request_openai_response = original_request

        commands_schema = captured_payloads[0]["text"]["format"]["schema"]["properties"]["commands"]
        self.assertEqual(commands_schema["minItems"], 0)

    def test_openai_error_detail_is_preserved(self) -> None:
        error = urllib.error.HTTPError(
            "https://api.openai.com/v1/responses",
            400,
            "Bad Request",
            {},
            io.BytesIO(json.dumps({"error": {"message": "Invalid schema detail"}}).encode("utf-8")),
        )

        self.assertEqual(ai_agents.extract_openai_error_detail(error), "Invalid schema detail")

    def test_backend_llm_fallback_strings_are_removed(self) -> None:
        service_code = Path(ai_agents.__file__).read_text(encoding="utf-8")
        routes_code = Path(main.agent_chat.__code__.co_filename).read_text(encoding="utf-8")

        self.assertNotIn("fallback_agent_chat", service_code)
        self.assertNotIn("fallback_chart_proposal", service_code)
        self.assertNotIn("요청을 반영해", service_code)
        self.assertNotIn("fallback_agent_chat", routes_code)


if __name__ == "__main__":
    unittest.main()
