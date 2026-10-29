from __future__ import annotations

import os
import sys
import types
import unittest
from dataclasses import replace
from datetime import timedelta
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

from app.auth.config import AuthConfig
from app.auth.identity import DeterministicIdentityResolver
from app.auth.models import AuthenticatedUser
from app.auth.session_store import MemorySessionStore
from app.auth_email.passwords import hash_password, password_is_strong, verify_password
from app.auth_email.repository import PURPOSE_RESET, PURPOSE_VERIFY, InMemoryEmailAuthRepository
from app.auth_email.service import REMEMBER_TTL_SECONDS

try:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from kis_trader.persistence.memory import InMemoryOrderRepository

    FASTAPI_TESTCLIENT_AVAILABLE = True
except Exception:
    TestClient = None
    FASTAPI_TESTCLIENT_AVAILABLE = False


class FakeMailer:
    """보낸 코드·링크를 붙잡아 두는 메일러."""

    def __init__(self) -> None:
        self.codes: list[tuple[str, str]] = []
        self.links: list[tuple[str, str]] = []

    def send_verification_code(self, email: str, code: str) -> None:
        self.codes.append((email, code))

    def send_password_reset(self, email: str, link: str) -> None:
        self.links.append((email, link))

    def last_code(self, email: str) -> str:
        return [code for to, code in self.codes if to == email][-1]

    def last_token(self, email: str) -> str:
        link = [link for to, link in self.links if to == email][-1]
        return parse_qs(urlparse(link).query)["token"][0]


class FakeGoogleOAuthClient:
    def exchange_code(self, *, code: str, redirect_uri: str) -> AuthenticatedUser:
        return AuthenticatedUser(sub="google-sub-1", email="google@example.com", email_verified=True, name="G")


class PasswordHashingTest(unittest.TestCase):
    def test_strength_rule_requires_letter_and_digit(self):
        self.assertFalse(password_is_strong("short1"))
        self.assertFalse(password_is_strong("abcdefgh"))
        self.assertFalse(password_is_strong("12345678"))
        self.assertTrue(password_is_strong("abcd1234"))

    def test_hash_round_trip(self):
        stored = hash_password("abcd1234")
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertTrue(verify_password("abcd1234", stored))
        self.assertFalse(verify_password("abcd1235", stored))
        self.assertFalse(verify_password("abcd1234", None))
        self.assertFalse(verify_password("abcd1234", "garbage"))


@unittest.skipUnless(FASTAPI_TESTCLIENT_AVAILABLE, "FastAPI TestClient is not available")
class EmailAuthRoutesTest(unittest.TestCase):
    EMAIL = "user@example.com"
    PASSWORD = "abcd1234"
    CONSENTS = {"tos": True, "privacy": True, "age": True, "mkt": False}

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
        self.repository = InMemoryEmailAuthRepository()
        self.app.state.email_auth_repository = self.repository
        self.mailer = FakeMailer()
        self.app.state.mailer = self.mailer
        self.client = TestClient(self.app)

    def tearDown(self):
        os.environ["AUTH_ENABLED"] = "false"

    # -- helpers --------------------------------------------------------------

    def signup(self, email: str = EMAIL, password: str = PASSWORD, consents: dict | None = None):
        return self.client.post(
            "/api/auth/email/signup",
            json={"email": email, "password": password, "consents": self.CONSENTS if consents is None else consents},
        )

    def verify(self, email: str = EMAIL, code: str | None = None):
        return self.client.post(
            "/api/auth/email/verify",
            json={"email": email, "code": code if code is not None else self.mailer.last_code(email)},
        )

    def login(self, email: str = EMAIL, password: str = PASSWORD, remember: bool = False):
        return self.client.post(
            "/api/auth/email/login", json={"email": email, "password": password, "remember": remember}
        )

    def signup_and_verify(self):
        self.assertEqual(self.signup().status_code, 201)
        verified = self.verify()
        self.assertEqual(verified.status_code, 200)
        return verified

    def latest_verification(self, purpose: str = PURPOSE_VERIFY):
        return self.repository.latest_verification(self.EMAIL, purpose)

    def rewrite_verification(self, record, **changes):
        index = next(i for i, item in enumerate(self.repository.verifications) if item.id == record.id)
        self.repository.verifications[index] = replace(record, **changes)

    # -- signup / verify / login ----------------------------------------------

    def test_signup_verify_login_happy_path(self):
        signup = self.signup(email="User@Example.com")
        self.assertEqual(signup.status_code, 201)
        self.assertEqual(signup.json(), {"verificationRequired": True, "resendAfterSeconds": 30})
        self.assertEqual(self.mailer.codes[0][0], self.EMAIL)  # 소문자로 정규화
        self.assertRegex(self.mailer.codes[0][1], r"^\d{6}$")

        # 인증 전에는 로그인할 수 없다.
        self.assertEqual(self.login().status_code, 403)
        self.assertEqual(self.login().json()["code"], "unverified")

        verified = self.verify()
        self.assertEqual(verified.status_code, 200)
        self.assertEqual(verified.json()["user"]["email"], self.EMAIL)
        self.assertEqual(verified.json()["user"]["provider"], "email")
        self.assertTrue(verified.cookies.get(self.config.session_cookie_name))

        me = self.client.get("/api/auth/me").json()
        self.assertEqual(me["user"]["provider"], "email")

        self.client.cookies.clear()
        login = self.login()
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["user"]["email"], self.EMAIL)
        self.assertTrue(login.cookies.get(self.config.session_cookie_name))
        self.assertEqual(self.client.get("/api/auth/me").json()["user"]["email"], self.EMAIL)

    def test_duplicate_email_is_409_after_verification(self):
        self.signup_and_verify()
        duplicate = self.signup()
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json(), {"code": "email_taken"})

    def test_unverified_signup_can_be_retried_with_new_code(self):
        self.assertEqual(self.signup().status_code, 201)
        first_code = self.mailer.last_code(self.EMAIL)
        self.assertEqual(self.signup(password="newpass99").status_code, 201)
        self.assertNotEqual(self.mailer.codes[0], self.mailer.codes[1])
        # 옛 코드는 더 이상 쓸 수 없다.
        self.assertEqual(self.verify(code=first_code).status_code, 400)
        self.assertEqual(self.verify().status_code, 200)
        self.client.cookies.clear()
        self.assertEqual(self.login(password="newpass99").status_code, 200)

    def test_weak_password_and_missing_consent_are_422(self):
        weak = self.signup(password="abcdefgh")
        self.assertEqual(weak.status_code, 422)
        self.assertEqual(weak.json(), {"code": "weak_password"})
        self.assertEqual(self.signup(password="1234567").json(), {"code": "weak_password"})

        no_consent = self.signup(consents={"tos": True, "privacy": True, "age": False})
        self.assertEqual(no_consent.status_code, 422)
        self.assertEqual(no_consent.json(), {"code": "consent_required"})
        without_consents = self.client.post(
            "/api/auth/email/signup", json={"email": self.EMAIL, "password": self.PASSWORD}
        )
        self.assertEqual(without_consents.status_code, 422)
        self.assertEqual(without_consents.json(), {"code": "consent_required"})

        bad_email = self.signup(email="not-an-email")
        self.assertEqual(bad_email.status_code, 422)
        self.assertEqual(bad_email.json(), {"code": "invalid_email"})
        self.assertEqual(self.mailer.codes, [])

    def test_wrong_code_counts_down_remaining_attempts(self):
        self.signup()
        first = self.verify(code="000000")
        self.assertEqual(first.status_code, 400)
        self.assertEqual(first.json(), {"code": "invalid_code", "remainingAttempts": 4})
        self.assertEqual(self.verify(code="000000").json()["remainingAttempts"], 3)
        for _ in range(3):
            self.verify(code="000000")
        # 다섯 번 틀리면 올바른 코드도 더는 받지 않는다.
        exhausted = self.verify()
        self.assertEqual(exhausted.status_code, 410)
        self.assertEqual(exhausted.json(), {"code": "expired"})

    def test_expired_code_is_410(self):
        self.signup()
        record = self.latest_verification()
        self.rewrite_verification(record, expires_at=record.created_at - timedelta(seconds=1))
        expired = self.verify()
        self.assertEqual(expired.status_code, 410)
        self.assertEqual(expired.json(), {"code": "expired"})

    def test_resend_is_throttled_for_30_seconds(self):
        self.signup()
        too_soon = self.client.post("/api/auth/email/resend", json={"email": self.EMAIL})
        self.assertEqual(too_soon.status_code, 429)
        self.assertEqual(too_soon.json()["code"], "too_soon")
        self.assertTrue(0 < too_soon.json()["retryAfterSeconds"] <= 30)

        record = self.latest_verification()
        self.rewrite_verification(record, created_at=record.created_at - timedelta(seconds=31))
        resent = self.client.post("/api/auth/email/resend", json={"email": self.EMAIL})
        self.assertEqual(resent.status_code, 200)
        self.assertEqual(resent.json(), {"resendAfterSeconds": 30})
        self.assertEqual(len(self.mailer.codes), 2)
        # 새 코드만 유효하다.
        self.assertEqual(self.verify(code=self.mailer.codes[0][1]).status_code, 400)
        self.assertEqual(self.verify().status_code, 200)

    def test_resend_for_unknown_email_returns_200_without_mail(self):
        response = self.client.post("/api/auth/email/resend", json={"email": "nobody@example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.mailer.codes, [])

    def test_five_failed_logins_lock_the_account_for_ten_minutes(self):
        self.signup_and_verify()
        self.client.cookies.clear()
        for expected_remaining in (4, 3, 2, 1):
            failed = self.login(password="wrongpass1")
            self.assertEqual(failed.status_code, 401)
            self.assertEqual(failed.json(), {"code": "invalid_credentials", "remainingAttempts": expected_remaining})

        locked = self.login(password="wrongpass1")
        self.assertEqual(locked.status_code, 423)
        self.assertEqual(locked.json()["code"], "locked")
        self.assertRegex(locked.json()["lockedUntil"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

        # 잠긴 동안은 올바른 비밀번호도 423.
        self.assertEqual(self.login().status_code, 423)

        # 잠금이 풀리면 처음부터 다시 센다.
        attempt = self.repository.get_login_attempt(self.EMAIL)
        self.repository.save_login_attempt(self.EMAIL, attempt.failed_count, attempt.locked_until - timedelta(minutes=11))
        self.assertEqual(self.login(password="wrongpass1").json()["remainingAttempts"], 4)
        self.assertEqual(self.login().status_code, 200)
        self.assertIsNone(self.repository.get_login_attempt(self.EMAIL))

    def test_unknown_email_login_is_401_without_leaking_existence(self):
        response = self.login(email="nobody@example.com")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"code": "invalid_credentials", "remainingAttempts": 4})

    def test_remember_extends_session_to_30_days(self):
        self.signup_and_verify()
        self.client.cookies.clear()

        short = self.login(remember=False)
        short_ttl = self.store.session_ttl(short.cookies[self.config.session_cookie_name])
        self.assertLessEqual(short_ttl, self.config.session_ttl_seconds)

        self.client.cookies.clear()
        long = self.login(remember=True)
        long_ttl = self.store.session_ttl(long.cookies[self.config.session_cookie_name])
        self.assertGreater(long_ttl, REMEMBER_TTL_SECONDS - 60)
        self.assertIn(f"Max-Age={REMEMBER_TTL_SECONDS}", long.headers["set-cookie"])

    # -- password reset -------------------------------------------------------

    def test_forgot_and_reset_drops_every_session_of_the_user(self):
        self.signup_and_verify()
        other_browser = TestClient(self.app)
        other_browser.post("/api/auth/email/login", json={"email": self.EMAIL, "password": self.PASSWORD})
        self.assertEqual(other_browser.get("/api/auth/me").json()["user"]["email"], self.EMAIL)

        forgot = self.client.post("/api/auth/password/forgot", json={"email": self.EMAIL})
        self.assertEqual(forgot.status_code, 200)
        self.assertEqual(forgot.json(), {"sent": True})
        link = self.mailer.links[0][1]
        self.assertTrue(link.startswith("http://testserver/forgot?step=reset&token="))

        reset = self.client.post(
            "/api/auth/password/reset", json={"token": self.mailer.last_token(self.EMAIL), "password": "newpass99"}
        )
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(reset.json(), {"reset": True})

        # 요청을 보낸 브라우저와 다른 브라우저 세션이 모두 사라졌다.
        self.assertIsNone(self.client.get("/api/auth/me").json()["user"])
        self.assertIsNone(other_browser.get("/api/auth/me").json()["user"])

        # 같은 토큰은 다시 쓸 수 없고, 새 비밀번호로만 로그인된다.
        reused = self.client.post(
            "/api/auth/password/reset", json={"token": self.mailer.last_token(self.EMAIL), "password": "another99"}
        )
        self.assertEqual(reused.status_code, 400)
        self.assertEqual(reused.json(), {"code": "invalid_token"})
        self.assertEqual(self.login().status_code, 401)
        self.assertEqual(self.login(password="newpass99").status_code, 200)

    def test_forgot_for_unknown_email_returns_200_without_mail(self):
        response = self.client.post("/api/auth/password/forgot", json={"email": "nobody@example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"sent": True})
        self.assertEqual(self.mailer.links, [])

    def test_reset_rejects_bad_expired_and_weak_inputs(self):
        self.signup_and_verify()
        self.client.post("/api/auth/password/forgot", json={"email": self.EMAIL})
        token = self.mailer.last_token(self.EMAIL)

        bad = self.client.post("/api/auth/password/reset", json={"token": "nope", "password": "newpass99"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json(), {"code": "invalid_token"})

        weak = self.client.post("/api/auth/password/reset", json={"token": token, "password": "short"})
        self.assertEqual(weak.status_code, 422)
        self.assertEqual(weak.json(), {"code": "weak_password"})

        record = self.latest_verification(PURPOSE_RESET)
        self.rewrite_verification(record, expires_at=record.created_at - timedelta(seconds=1))
        expired = self.client.post("/api/auth/password/reset", json={"token": token, "password": "newpass99"})
        self.assertEqual(expired.status_code, 410)
        self.assertEqual(expired.json(), {"code": "expired"})

        # 비밀번호는 그대로이고 세션도 살아 있다.
        self.assertEqual(self.client.get("/api/auth/me").json()["user"]["email"], self.EMAIL)

if __name__ == "__main__":
    unittest.main()
