from __future__ import annotations

import json
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
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server"
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

from app.auth.config import AuthConfig, _load_auth_secret_values
from app.auth.dependencies import optional_current_user
from app.auth.identity import DeterministicIdentityResolver, deterministic_app_user_id
from app.auth.models import AuthenticatedUser
from app.auth.session_store import MemorySessionStore
from kis_trader.persistence.user_context import bind_app_user_id, current_app_user_id

try:
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from app.main import create_app
    from kis_trader.persistence.memory import InMemoryOrderRepository
    from systems.order.tests.kis_trader.fixtures.orders import sample_order_request

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


class AuthConfigSecretManagerTest(unittest.TestCase):
    ENV_KEYS = (
        "AUTH_ENABLED",
        "AUTH_SESSION_SECRET",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_OAUTH_SECRET_NAME",
        "AUTH_SECRET_NAME",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    )

    def setUp(self):
        self.original_env = {key: os.environ.get(key) for key in self.ENV_KEYS}
        self.had_boto3_module = "boto3" in sys.modules
        self.original_boto3_module = sys.modules.get("boto3")
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)
        _load_auth_secret_values.cache_clear()

    def tearDown(self):
        for key, value in self.original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if self.had_boto3_module:
            sys.modules["boto3"] = self.original_boto3_module
        else:
            sys.modules.pop("boto3", None)
        _load_auth_secret_values.cache_clear()

    def test_loads_google_oauth_settings_from_secret_manager(self):
        os.environ["AUTH_ENABLED"] = "true"
        os.environ["GOOGLE_OAUTH_SECRET_NAME"] = "oauth/google"
        os.environ["AWS_REGION"] = "ap-northeast-2"

        class FakeSecretsManagerClient:
            def get_secret_value(self, SecretId: str) -> dict[str, str]:
                assert SecretId == "oauth/google"
                return {
                    "SecretString": json.dumps(
                        {
                            "web": {
                                "client_id": "secret-client-id",
                                "client_secret": "secret-client-secret",
                            },
                            "AUTH_SESSION_SECRET": "secret-session",
                        }
                    )
                }

        def fake_client(service_name: str, region_name: str):
            assert service_name == "secretsmanager"
            assert region_name == "ap-northeast-2"
            return FakeSecretsManagerClient()

        sys.modules["boto3"] = types.SimpleNamespace(client=fake_client)

        config = AuthConfig.from_env()

        self.assertEqual(config.google_client_id, "secret-client-id")
        self.assertEqual(config.google_client_secret, "secret-client-secret")
        self.assertEqual(config.session_secret, "secret-session")
        config.require_oauth_settings()


class AuthenticatedUserCompatibilityTest(unittest.TestCase):
    def test_internal_uuid_round_trips_in_session_but_stays_out_of_public_payload(self):
        app_user_id = deterministic_app_user_id("google-sub-1")
        user = AuthenticatedUser(
            "google-sub-1", "user@example.com", True, "Example User", None, app_user_id
        )

        restored = AuthenticatedUser.from_session(user.to_session())

        self.assertEqual(restored.app_user_id, app_user_id)
        self.assertNotIn("app_user_id", user.to_public())

    def test_legacy_session_without_uuid_is_still_readable(self):
        restored = AuthenticatedUser.from_session({
            "sub": "legacy-sub",
            "email": "legacy@example.com",
            "email_verified": True,
            "name": None,
            "picture": None,
        })

        self.assertIsNone(restored.app_user_id)


class AuthenticatedUserContextTest(unittest.IsolatedAsyncioTestCase):
    async def test_async_dependency_binds_uuid_after_session_lookup_thread(self):
        previous_enabled = os.environ.get("AUTH_ENABLED")
        previous_secret = os.environ.get("AUTH_SESSION_SECRET")
        os.environ["AUTH_ENABLED"] = "true"
        os.environ["AUTH_SESSION_SECRET"] = "context-test-secret"
        try:
            config = AuthConfig.from_env()
            store = MemorySessionStore(config)
            app_user_id = deterministic_app_user_id("context-sub")
            session_id = store.create_session(AuthenticatedUser(
                "context-sub", "context@example.com", True, app_user_id=app_user_id,
            ))
            request = types.SimpleNamespace(
                cookies={config.session_cookie_name: session_id},
                app=types.SimpleNamespace(state=types.SimpleNamespace(auth_session_store=store)),
            )

            user = await optional_current_user(request)

            self.assertEqual(user.app_user_id, app_user_id)
            self.assertEqual(current_app_user_id(), app_user_id)
        finally:
            bind_app_user_id(None)
            if previous_enabled is None:
                os.environ.pop("AUTH_ENABLED", None)
            else:
                os.environ["AUTH_ENABLED"] = previous_enabled
            if previous_secret is None:
                os.environ.pop("AUTH_SESSION_SECRET", None)
            else:
                os.environ["AUTH_SESSION_SECRET"] = previous_secret


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
        self.app.state.user_identity_resolver = DeterministicIdentityResolver()
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

    def test_order_and_event_reads_hide_foreign_order_existence(self):
        owner_cookie = self.store.create_session(AuthenticatedUser("owner-sub", "owner@example.com", True))
        self.client.cookies.set(self.config.session_cookie_name, owner_cookie)
        created = self.client.post(
            "/api/orders",
            json=sample_order_request(),
            headers={"Idempotency-Key": "owner-order"},
        )
        self.assertEqual(created.status_code, 202)
        order_id = created.json()["order_id"]

        attacker_cookie = self.store.create_session(AuthenticatedUser("attacker-sub", "attacker@example.com", True))
        self.client.cookies.set(self.config.session_cookie_name, attacker_cookie)

        foreign_order = self.client.get(f"/api/orders/{order_id}")
        missing_order = self.client.get("/api/orders/ord_missing")
        self.assertEqual(foreign_order.status_code, 404)
        self.assertEqual(foreign_order.json(), missing_order.json())

        foreign_events = self.client.get(f"/api/orders/{order_id}/events")
        missing_events = self.client.get("/api/orders/ord_missing/events")
        self.assertEqual(foreign_events.status_code, 404)
        self.assertEqual(foreign_events.json(), missing_events.json())

    def test_order_websocket_hides_foreign_order_existence(self):
        owner_cookie = self.store.create_session(AuthenticatedUser("owner-sub", "owner@example.com", True))
        self.client.cookies.set(self.config.session_cookie_name, owner_cookie)
        created = self.client.post(
            "/api/orders",
            json=sample_order_request(),
            headers={"Idempotency-Key": "owner-websocket-order"},
        )
        self.assertEqual(created.status_code, 202)
        order_id = created.json()["order_id"]

        attacker_cookie = self.store.create_session(AuthenticatedUser("attacker-sub", "attacker@example.com", True))
        self.client.cookies.set(self.config.session_cookie_name, attacker_cookie)

        observations = []
        for target in (order_id, "ord_missing"):
            with self.client.websocket_connect(f"/ws/orders/{target}") as websocket:
                message = websocket.receive_json()
                with self.assertRaises(WebSocketDisconnect) as closed:
                    websocket.receive_json()
            observations.append((message, closed.exception.code))

        self.assertEqual(observations[0], observations[1])
        self.assertEqual(observations[0], ({"type": "error", "detail": "order not found"}, 1008))

    def test_protected_order_websocket_requires_session(self):
        with self.client.websocket_connect("/ws/orders/ord_missing") as websocket:
            message = websocket.receive_json()

        self.assertEqual(message["type"], "error")
        self.assertEqual(message["detail"], "authentication required")


if __name__ == "__main__":
    unittest.main()
