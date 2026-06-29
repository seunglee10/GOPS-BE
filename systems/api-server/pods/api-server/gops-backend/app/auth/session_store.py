from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

try:
    import redis
except Exception:  # pragma: no cover - exercised only when dependency is absent
    redis = None  # type: ignore[assignment]

from app.auth.config import AuthConfig
from app.auth.models import AuthenticatedUser


class SessionStoreError(RuntimeError):
    """Raised when session storage is unavailable or malformed."""


class RedisSessionStore:
    def __init__(self, redis_client: Any, config: AuthConfig) -> None:
        self.redis = redis_client
        self.config = config

    @classmethod
    def from_env(cls, config: AuthConfig) -> "RedisSessionStore":
        if redis is None:
            raise SessionStoreError("redis package is not installed")
        redis_url = os.getenv("AUTH_REDIS_URL") or os.getenv("REDIS_URL") or "redis://localhost:6379/0"
        return cls(redis.from_url(redis_url, decode_responses=True), config)

    def create_oauth_state(self, return_to: str) -> str:
        state = secrets.token_urlsafe(32)
        payload = {"returnTo": return_to, "createdAt": int(time.time())}
        self._set_json(self._state_key(state), payload, self.config.oauth_state_ttl_seconds)
        return state

    def pop_oauth_state(self, state: str) -> dict[str, Any] | None:
        key = self._state_key(state)
        raw = self.redis.get(key)
        self.redis.delete(key)
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SessionStoreError("OAuth state payload is invalid") from exc
        return payload if isinstance(payload, dict) else None

    def create_session(self, user: AuthenticatedUser) -> str:
        session_id = secrets.token_urlsafe(48)
        payload = {
            "user": user.to_session(),
            "createdAt": int(time.time()),
        }
        self._set_json(self._session_key(session_id), payload, self.config.session_ttl_seconds)
        return session_id

    def get_session(self, session_id: str | None) -> AuthenticatedUser | None:
        if not session_id:
            return None
        raw = self.redis.get(self._session_key(session_id))
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            user_payload = payload.get("user") if isinstance(payload, dict) else None
        except json.JSONDecodeError as exc:
            raise SessionStoreError("Session payload is invalid") from exc
        if not isinstance(user_payload, dict):
            return None
        return AuthenticatedUser.from_session(user_payload)

    def delete_session(self, session_id: str | None) -> None:
        if session_id:
            self.redis.delete(self._session_key(session_id))

    def _set_json(self, key: str, payload: dict[str, Any], ttl_seconds: int) -> None:
        self.redis.set(key, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), ex=ttl_seconds)

    def _state_key(self, state: str) -> str:
        return f"{self.config.redis_key_prefix}:oauth_state:{self._digest(state)}"

    def _session_key(self, session_id: str) -> str:
        return f"{self.config.redis_key_prefix}:session:{self._digest(session_id)}"

    def _digest(self, value: str) -> str:
        secret = self.config.session_secret or ""
        return hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


class MemorySessionStore:
    """Small in-memory store for API tests and auth-disabled local runs."""

    def __init__(self, config: AuthConfig) -> None:
        self.config = config
        self.values: dict[str, tuple[float, str]] = {}

    def create_oauth_state(self, return_to: str) -> str:
        state = secrets.token_urlsafe(32)
        self._set_json(self._state_key(state), {"returnTo": return_to}, self.config.oauth_state_ttl_seconds)
        return state

    def pop_oauth_state(self, state: str) -> dict[str, Any] | None:
        key = self._state_key(state)
        raw = self._get(key)
        self.values.pop(key, None)
        return json.loads(raw) if raw else None

    def create_session(self, user: AuthenticatedUser) -> str:
        session_id = secrets.token_urlsafe(48)
        self._set_json(self._session_key(session_id), {"user": user.to_session()}, self.config.session_ttl_seconds)
        return session_id

    def get_session(self, session_id: str | None) -> AuthenticatedUser | None:
        if not session_id:
            return None
        raw = self._get(self._session_key(session_id))
        if not raw:
            return None
        payload = json.loads(raw)
        return AuthenticatedUser.from_session(payload["user"])

    def delete_session(self, session_id: str | None) -> None:
        if session_id:
            self.values.pop(self._session_key(session_id), None)

    def _set_json(self, key: str, payload: dict[str, Any], ttl_seconds: int) -> None:
        self.values[key] = (time.time() + ttl_seconds, json.dumps(payload, ensure_ascii=False))

    def _get(self, key: str) -> str | None:
        stored = self.values.get(key)
        if not stored:
            return None
        expires_at, value = stored
        if expires_at < time.time():
            self.values.pop(key, None)
            return None
        return value

    def _state_key(self, state: str) -> str:
        return f"oauth_state:{state}"

    def _session_key(self, session_id: str) -> str:
        return f"session:{session_id}"


def session_store_from_app(app: Any, config: AuthConfig | None = None) -> Any:
    existing = getattr(app.state, "auth_session_store", None)
    if existing is not None:
        return existing
    store = RedisSessionStore.from_env(config or AuthConfig.from_env())
    app.state.auth_session_store = store
    return store

