from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest import mock

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

    from app.auth.config import AuthConfig
    from app.auth.dependencies import WebSocketAuthRequired, require_websocket_user
    from app.auth.session_store import MemorySessionStore
    from app.main import create_app
except Exception as exc:  # pragma: no cover - dependency guard for lean envs
    pytest.skip(f"FastAPI chart stream tests are unavailable: {exc}", allow_module_level=True)


def test_chart_websocket_allows_anonymous_session_when_auth_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "google-client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "google-client-secret")
    monkeypatch.delenv("GOOGLE_OAUTH_SECRET_NAME", raising=False)
    monkeypatch.delenv("AUTH_SECRET_NAME", raising=False)

    app = create_app()
    app.state.auth_session_store = MemorySessionStore(AuthConfig.from_env())

    class FakeChartSessionManager:
        async def serve_chart(self, websocket, symbol, interval, cursor, user_id="anonymous"):
            await websocket.accept()
            await websocket.send_json({
                "type": "HEARTBEAT",
                "symbol": symbol,
                "interval": interval,
                "userId": user_id,
            })
            await websocket.close()

    with mock.patch("app.routes.streams.WebSocketSessionManager", FakeChartSessionManager):
        with TestClient(app) as client:
            with client.websocket_connect("/ws/charts?symbol=AAPL&interval=1m") as websocket:
                message = websocket.receive_json()

    assert message == {
        "type": "HEARTBEAT",
        "symbol": "AAPL",
        "interval": "1m",
        "userId": "anonymous",
    }


def test_required_websocket_user_still_rejects_missing_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "google-client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "google-client-secret")
    monkeypatch.delenv("GOOGLE_OAUTH_SECRET_NAME", raising=False)
    monkeypatch.delenv("AUTH_SECRET_NAME", raising=False)

    config = AuthConfig.from_env()
    app = types.SimpleNamespace(state=types.SimpleNamespace(auth_session_store=MemorySessionStore(config)))
    websocket = types.SimpleNamespace(app=app, cookies={})

    with pytest.raises(WebSocketAuthRequired, match="authentication required"):
        require_websocket_user(websocket)
