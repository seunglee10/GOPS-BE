from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
ORDER_SHARED = ROOT / "systems" / "order" / "shared"
ORDER_TEST_ROOT = ROOT / "systems" / "order"
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend"
for path in (str(MARKET_SHARED), str(ORDER_SHARED), str(ORDER_TEST_ROOT), str(BACKEND), str(ROOT)):
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

    FASTAPI_TESTCLIENT_AVAILABLE = True
except Exception:
    TestClient = None
    FASTAPI_TESTCLIENT_AVAILABLE = False


class FakeWebSocketSessionManager:
    async def serve_chart(self, websocket, symbol, interval, cursor=None) -> None:
        await websocket.accept()
        await websocket.send_json({
            "type": "CHART_TEST",
            "symbol": symbol,
            "interval": interval,
            "cursor": cursor,
        })
        await websocket.close()

    async def serve_quotes(self, websocket, symbols, interval, max_hz=1.0) -> None:
        await websocket.accept()
        await websocket.send_json({
            "type": "QUOTE_TEST",
            "symbols": symbols,
            "interval": interval,
            "maxHz": max_hz,
        })
        await websocket.close()


@unittest.skipUnless(FASTAPI_TESTCLIENT_AVAILABLE, "FastAPI TestClient is not available")
class StreamRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(create_app())

    def test_chart_stream_normalizes_symbol_and_interval(self) -> None:
        with patch("app.routes.streams.WebSocketSessionManager", return_value=FakeWebSocketSessionManager()):
            with self.client.websocket_connect("/ws/charts?symbol=aapl&interval=1d&cursor=v1:test") as websocket:
                message = websocket.receive_json()

        self.assertEqual(message["type"], "CHART_TEST")
        self.assertEqual(message["symbol"], "AAPL")
        self.assertEqual(message["interval"], "1D")
        self.assertEqual(message["cursor"], "v1:test")

    def test_quote_stream_normalizes_symbols_and_passes_max_hz(self) -> None:
        with patch("app.routes.streams.WebSocketSessionManager", return_value=FakeWebSocketSessionManager()):
            with self.client.websocket_connect("/ws/quotes?symbols=aapl,MSFT,aapl&interval=1m&maxHz=2.5") as websocket:
                message = websocket.receive_json()

        self.assertEqual(message["type"], "QUOTE_TEST")
        self.assertEqual(message["symbols"], ["AAPL", "MSFT"])
        self.assertEqual(message["interval"], "1m")
        self.assertEqual(message["maxHz"], 2.5)

    def test_quote_stream_rejects_empty_symbol_list(self) -> None:
        with self.client.websocket_connect("/ws/quotes?symbols=&interval=1m") as websocket:
            message = websocket.receive_json()

        self.assertEqual(message["type"], "ERROR")
        self.assertEqual(message["detail"], "At least one quote symbol is required.")


if __name__ == "__main__":
    unittest.main()
