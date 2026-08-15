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
from app.auth.kakao import KakaoOAuthResult
from app.auth.models import AuthenticatedUser, AuthUserError
from app.auth.tokens import ProviderTokens
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


class FakeKakaoOAuthClient:
    def exchange_code(self, *, code: str, redirect_uri: str) -> KakaoOAuthResult:
        if code != "ok-code":
            raise RuntimeError("unexpected code")
        return KakaoOAuthResult(
            user=AuthenticatedUser(
                sub="1234567890",
                email=None,
                email_verified=False,
                name="테스트유저",
                provider="kakao",
            ),
            tokens=ProviderTokens(
                access_token="kakao-access-token",
                refresh_token="kakao-refresh-token",
                access_expires_in=21599,
                refresh_expires_in=5183999,
                scope="profile_image profile_nickname",
            ),
        )


class RecordingTokenStore:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str, ProviderTokens]] = []

    def save(self, *, provider: str, provider_subject: str, tokens: ProviderTokens) -> None:
        self.saved.append((provider, provider_subject, tokens))


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
        os.environ["KAKAO_OAUTH_CLIENT_ID"] = "kakao-client-id"
        os.environ["KAKAO_OAUTH_CLIENT_SECRET"] = "kakao-client-secret"
        os.environ["KIS_ENV"] = "demo"
        os.environ["KAFKA_ACCOUNT_ALIAS"] = "demo-account"
        os.environ["IDEMPOTENCY_HASH_SECRET"] = "test-secret"

        self.app = create_app()
        self.config = AuthConfig.from_env()
        self.store = MemorySessionStore(self.config)
        self.app.state.auth_session_store = self.store
        self.app.state.user_identity_resolver = DeterministicIdentityResolver()
        self.app.state.google_oauth_client = FakeGoogleOAuthClient()
        self.app.state.kakao_oauth_client = FakeKakaoOAuthClient()
        self.token_store = RecordingTokenStore()
        self.app.state.provider_token_store = self.token_store
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

    def test_kakao_login_callback_creates_session_for_user_without_email(self):
        login = self.client.get("/api/auth/kakao/login?returnTo=/workspace", follow_redirects=False)
        self.assertEqual(login.status_code, 307)

        location = urlparse(login.headers["location"])
        self.assertEqual(location.netloc, "kauth.kakao.com")
        self.assertEqual(location.path, "/oauth/authorize")

        query = parse_qs(location.query)
        self.assertEqual(query["client_id"][0], "kakao-client-id")
        self.assertEqual(query["redirect_uri"][0], "http://testserver/api/auth/kakao/callback")
        self.assertNotIn("account_email", query["scope"][0])
        state = query["state"][0]
        self.assertEqual(login.cookies.get(self.config.oauth_state_cookie_name), state)

        callback = self.client.get(f"/api/auth/kakao/callback?code=ok-code&state={state}", follow_redirects=False)
        self.assertEqual(callback.status_code, 307)
        self.assertEqual(callback.headers["location"], "/workspace")
        self.assertTrue(callback.cookies.get(self.config.session_cookie_name))

        me = self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertIsNone(me.json()["user"]["email"])
        self.assertEqual(me.json()["user"]["name"], "테스트유저")
        self.assertEqual(me.json()["user"]["provider"], "kakao")

    def test_kakao_login_stores_provider_tokens(self):
        login = self.client.get("/api/auth/kakao/login", follow_redirects=False)
        state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]

        self.client.get(f"/api/auth/kakao/callback?code=ok-code&state={state}", follow_redirects=False)

        self.assertEqual(len(self.token_store.saved), 1)
        provider, subject, tokens = self.token_store.saved[0]
        self.assertEqual(provider, "kakao")
        self.assertEqual(subject, "1234567890")
        self.assertEqual(tokens.refresh_token, "kakao-refresh-token")
        self.assertEqual(tokens.refresh_expires_in, 5183999)

    def test_provider_tokens_never_appear_in_the_session_payload(self):
        login = self.client.get("/api/auth/kakao/login", follow_redirects=False)
        state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
        self.client.get(f"/api/auth/kakao/callback?code=ok-code&state={state}", follow_redirects=False)

        me = self.client.get("/api/auth/me")

        self.assertNotIn("kakao-refresh-token", me.text)
        self.assertNotIn("kakao-access-token", me.text)

    def test_provider_tokens_are_masked_in_repr(self):
        tokens = ProviderTokens(access_token="secret-access", refresh_token="secret-refresh")

        rendered = repr(tokens)

        self.assertNotIn("secret-access", rendered)
        self.assertNotIn("secret-refresh", rendered)

    def test_kakao_callback_rejects_state_from_another_browser(self):
        self.client.get("/api/auth/kakao/login", follow_redirects=False)

        callback = self.client.get("/api/auth/kakao/callback?code=ok-code&state=forged", follow_redirects=False)

        self.assertEqual(callback.status_code, 400)

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


KAKAO_ME_WITHOUT_EMAIL = {
    "id": 1234567890,
    "connected_at": "2026-08-15T04:12:33Z",
    "properties": {"nickname": "레거시닉네임"},
    "kakao_account": {
        "profile_nickname_needs_agreement": False,
        "profile_image_needs_agreement": False,
        "profile": {
            "nickname": "테스트유저",
            "thumbnail_image_url": "https://example.test/thumb.jpg",
            "profile_image_url": "https://example.test/profile.jpg",
            "is_default_image": False,
        },
    },
}


class KakaoProfileParsingTest(unittest.TestCase):
    def test_numeric_id_becomes_a_string_subject(self):
        user = AuthenticatedUser.from_kakao_profile(KAKAO_ME_WITHOUT_EMAIL)

        self.assertEqual(user.sub, "1234567890")
        self.assertIsInstance(user.sub, str)
        self.assertEqual(user.provider, "kakao")

    def test_missing_email_is_accepted_and_never_counts_as_verified(self):
        user = AuthenticatedUser.from_kakao_profile(KAKAO_ME_WITHOUT_EMAIL)

        self.assertIsNone(user.email)
        self.assertFalse(user.email_verified)
        self.assertEqual(user.name, "테스트유저")
        self.assertEqual(user.picture, "https://example.test/profile.jpg")

    def test_email_counts_as_verified_only_when_kakao_confirms_both_flags(self):
        payload = json.loads(json.dumps(KAKAO_ME_WITHOUT_EMAIL))
        payload["kakao_account"].update({
            "email": "user@kakao.test",
            "is_email_valid": True,
            "is_email_verified": True,
        })

        user = AuthenticatedUser.from_kakao_profile(payload)

        self.assertEqual(user.email, "user@kakao.test")
        self.assertTrue(user.email_verified)

    def test_unverified_email_is_kept_but_not_trusted(self):
        payload = json.loads(json.dumps(KAKAO_ME_WITHOUT_EMAIL))
        payload["kakao_account"].update({
            "email": "user@kakao.test",
            "is_email_valid": True,
            "is_email_verified": False,
        })

        user = AuthenticatedUser.from_kakao_profile(payload)

        self.assertEqual(user.email, "user@kakao.test")
        self.assertFalse(user.email_verified)

    def test_missing_id_is_rejected(self):
        with self.assertRaises(AuthUserError):
            AuthenticatedUser.from_kakao_profile({"kakao_account": {}})

    def test_provider_survives_a_session_round_trip(self):
        user = AuthenticatedUser.from_kakao_profile(KAKAO_ME_WITHOUT_EMAIL)

        restored = AuthenticatedUser.from_session(user.to_session())

        self.assertEqual(restored.provider, "kakao")
        self.assertIsNone(restored.email)

    def test_legacy_session_without_provider_defaults_to_google(self):
        restored = AuthenticatedUser.from_session({
            "sub": "google-sub-1",
            "email": "user@example.com",
            "email_verified": True,
        })

        self.assertEqual(restored.provider, "google")


if __name__ == "__main__":
    unittest.main()
