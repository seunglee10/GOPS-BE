from __future__ import annotations

import os
import sys
import types
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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

    from app.auth.config import AuthConfig
    from app.auth.models import AuthenticatedUser
    from app.auth.session_store import MemorySessionStore
    from app.main import create_app
    from kis_trader.persistence.memory import InMemoryOrderRepository
    from tests.kis_trader.fixtures.orders import sample_order_request

    FASTAPI_TESTCLIENT_AVAILABLE = True
except Exception:
    TestClient = None
    FASTAPI_TESTCLIENT_AVAILABLE = False


class FakeGoogleOAuthClient:
    def exchange_code(self, *, code: str, redirect_uri: str) -> AuthenticatedUser:
        if code != "ok-code":
            raise RuntimeError("unexpected code")
        return AuthenticatedUser(
            sub="google-sub-1",
            email="user@example.com",
            email_verified=True,
            name="Example User",
        )


@unittest.skipUnless(FASTAPI_TESTCLIENT_AVAILABLE, "FastAPI TestClient is not available")
class AuthRoutesTest(unittest.TestCase):
    def setUp(self):
        os.environ["AUTH_ENABLED"] = "true"
        os.environ["AUTH_PUBLIC_BASE_URL"] = "http://testserver"
        os.environ["AUTH_SESSION_SECRET"] = "test-session-secret"
        os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "google-client-id"
        os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "google-client-secret"
        os.environ["KIS_ENV"] = "demo"
        os.environ["KAFKA_ACCOUNT_ALIAS"] = "demo-account"
        os.environ["IDEMPOTENCY_HASH_SECRET"] = "test-secret"

        self.app = create_app()
        self.config = AuthConfig.from_env()
        self.store = MemorySessionStore(self.config)
        self.app.state.auth_session_store = self.store
        self.app.state.google_oauth_client = FakeGoogleOAuthClient()
        self.app.state.order_repository = InMemoryOrderRepository()
        self.client = TestClient(self.app)

    def tearDown(self):
        os.environ["AUTH_ENABLED"] = "false"

    def test_me_returns_no_user_without_session(self):
        response = self.client.get("/api/auth/me")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["authEnabled"])
        self.assertIsNone(response.json()["user"])

    def test_google_login_callback_creates_session_cookie(self):
        login = self.client.get("/api/auth/google/login?returnTo=/workspace", follow_redirects=False)
        self.assertEqual(login.status_code, 307)
        state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
        self.assertEqual(login.cookies.get(self.config.oauth_state_cookie_name), state)

        callback = self.client.get(f"/api/auth/google/callback?code=ok-code&state={state}", follow_redirects=False)
        self.assertEqual(callback.status_code, 307)
        self.assertEqual(callback.headers["location"], "/workspace")
        self.assertTrue(callback.cookies.get(self.config.session_cookie_name))

        me = self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["user"]["email"], "user@example.com")

    def test_protected_order_route_requires_session(self):
        response = self.client.post("/api/orders", json=sample_order_request(), headers={"Idempotency-Key": "idem-1"})

        self.assertEqual(response.status_code, 401)

    def test_protected_order_route_accepts_valid_session(self):
        self.client.cookies.set(
            self.config.session_cookie_name,
            self.store.create_session(AuthenticatedUser("google-sub-1", "user@example.com", True)),
        )

        response = self.client.post("/api/orders", json=sample_order_request(), headers={"Idempotency-Key": "idem-1"})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "RECEIVED")

    def test_protected_order_websocket_requires_session(self):
        with self.client.websocket_connect("/ws/orders/ord_missing") as websocket:
            message = websocket.receive_json()

        self.assertEqual(message["type"], "error")
        self.assertEqual(message["detail"], "authentication required")


if __name__ == "__main__":
    unittest.main()
