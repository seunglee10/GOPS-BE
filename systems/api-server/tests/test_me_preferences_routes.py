import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
ORDER_SHARED = ROOT / "systems" / "order" / "shared"
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server"
for path in (str(MARKET_SHARED), str(ORDER_SHARED), str(BACKEND), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from fastapi.testclient import TestClient  # noqa: E402

from app.auth.dependencies import require_current_user  # noqa: E402
from app.auth.models import AuthenticatedUser  # noqa: E402
from app.auth_email.repository import InMemoryEmailAuthRepository  # noqa: E402
from app.main import create_app  # noqa: E402
from app.me.preferences import InMemoryPreferencesRepository, PreferencesRepositoryError  # noqa: E402


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


class BrokenRepository:
    def get(self, *args):
        raise PreferencesRepositoryError("down")

    def save(self, *args):
        raise PreferencesRepositoryError("down")


def user(sub: str) -> AuthenticatedUser:
    return AuthenticatedUser(sub=sub, email=f"{sub}@gops.local", email_verified=True, app_user_id=f"uid-{sub}")


DATA = {
    "layout": {"panes": [{"id": "p1"}]},
    "chart": {"interval": "1d"},
    "draw": [],
    "cur": "KRW",
    "sideW": 320,
    "dwW": 420,
    "brand": "gops",
    "experience": "some",
}


class PreferencesRoutesTest(unittest.TestCase):
    def setUp(self):
        os.environ["AUTH_ENABLED"] = "false"
        self.app = create_app()
        self.app.state.user_preferences_repository = InMemoryPreferencesRepository()
        self.app.state.email_auth_repository = InMemoryEmailAuthRepository()
        self.app.state.recent_symbols_now_provider = Clock()
        self.users = {"current": user("alice")}
        self.app.dependency_overrides[require_current_user] = lambda: self.users["current"]
        self.client = TestClient(self.app)

    def put(self, version, data):
        return self.client.put("/api/me/preferences", json={"version": version, "data": data})

    def test_empty_by_default(self):
        response = self.client.get("/api/me/preferences")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"version": 0, "updatedAt": None, "data": {}})

    def test_put_then_get_increments_version(self):
        first = self.put(0, DATA)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), {"version": 1, "updatedAt": "2026-10-30T12:00:01.000Z", "data": DATA})
        second = self.put(1, {**DATA, "cur": "USD"})
        self.assertEqual(second.json()["version"], 2)
        got = self.client.get("/api/me/preferences").json()
        self.assertEqual(got["version"], 2)
        self.assertEqual(got["data"]["cur"], "USD")
        self.assertEqual(got["updatedAt"], "2026-10-30T12:00:02.000Z")

    def test_stale_version_is_409_with_current(self):
        self.put(0, DATA)
        response = self.put(0, {"cur": "USD"})
        self.assertEqual(response.status_code, 409)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "version_conflict")
        self.assertEqual(detail["current"]["version"], 1)
        self.assertEqual(detail["current"]["data"], DATA)
        self.assertEqual(self.put(5, DATA).status_code, 409)

    def test_over_64kb_is_413(self):
        response = self.put(0, {"layout": "x" * (64 * 1024)})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["detail"]["code"], "preferences_too_large")
        self.assertEqual(self.client.get("/api/me/preferences").json()["version"], 0)

    def test_invalid_experience_is_422(self):
        response = self.put(0, {"experience": "guru"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "invalid_experience")
        self.assertEqual(self.put(0, {"experience": None}).status_code, 200)

    def test_negative_version_and_non_object_data_are_rejected(self):
        self.assertEqual(self.put(-1, {}).status_code, 422)
        self.assertEqual(self.client.put("/api/me/preferences", json={"version": 0, "data": [1]}).status_code, 422)

    def test_per_user(self):
        self.put(0, DATA)
        self.users["current"] = user("bob")
        self.assertEqual(self.client.get("/api/me/preferences").json()["version"], 0)

    def test_storage_failure_is_503(self):
        self.app.state.user_preferences_repository = BrokenRepository()
        self.assertEqual(self.client.get("/api/me/preferences").status_code, 503)
        response = self.put(0, DATA)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "preferences_unavailable")

    def _me(self):
        async def fake_optional(request):
            return self.users["current"]

        with mock.patch("app.routes.auth.optional_current_user", fake_optional):
            return self.client.get("/api/auth/me").json()["user"]

    def test_auth_me_reads_experience_from_preferences(self):
        self.assertIsNone(self._me()["experience"])
        self.put(0, DATA)
        self.assertEqual(self._me()["experience"], "some")

    def test_auth_me_ignores_bad_stored_experience_and_storage_failure(self):
        self.app.state.user_preferences_repository.save("uid-alice", 0, {"experience": "guru"}, datetime.now(timezone.utc))
        self.assertIsNone(self._me()["experience"])
        self.app.state.user_preferences_repository = BrokenRepository()
        self.assertIsNone(self._me()["experience"])


if __name__ == "__main__":
    unittest.main()
