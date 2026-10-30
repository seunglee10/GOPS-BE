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

    def create_session(self, user: AuthenticatedUser, ttl_seconds: int | None = None) -> str:
        session_id = secrets.token_urlsafe(48)
        ttl = ttl_seconds if ttl_seconds and ttl_seconds > 0 else self.config.session_ttl_seconds
        payload = {
            "user": user.to_session(),
            "createdAt": int(time.time()),
        }
        key = self._session_key(session_id)
        self._set_json(key, payload, ttl)
        if user.app_user_id:
            # 사용자별 세션 목록. 비밀번호 재설정 때 그 사용자의 세션을 전부 지우는 데 쓴다.
            index_key = self._user_sessions_key(user.app_user_id)
            self.redis.sadd(index_key, key)
            current_ttl = self.redis.ttl(index_key)
            if not isinstance(current_ttl, int) or current_ttl < ttl:
                self.redis.expire(index_key, ttl)
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

    def update_session(self, session_id: str, user: AuthenticatedUser) -> None:
        key = self._session_key(session_id)
        raw = self.redis.get(key)
        if not raw:
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SessionStoreError("Session payload is invalid") from exc
        if not isinstance(payload, dict):
            return
        payload["user"] = user.to_session()
        ttl = self.redis.ttl(key)
        self._set_json(key, payload, ttl if isinstance(ttl, int) and ttl > 0 else self.config.session_ttl_seconds)

    def delete_session(self, session_id: str | None) -> None:
        if session_id:
            self.redis.delete(self._session_key(session_id))

    def delete_sessions_for_user(self, app_user_id: str | None) -> int:
        if not app_user_id:
            return 0
        index_key = self._user_sessions_key(app_user_id)
        members = self.redis.smembers(index_key) or set()
        keys = [member if isinstance(member, str) else member.decode("utf-8") for member in members]
        deleted = 0
        if keys:
            deleted = int(self.redis.delete(*keys) or 0)
        self.redis.delete(index_key)
        return deleted

    def _set_json(self, key: str, payload: dict[str, Any], ttl_seconds: int) -> None:
        self.redis.set(key, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), ex=ttl_seconds)

    def _state_key(self, state: str) -> str:
        return f"{self.config.redis_key_prefix}:oauth_state:{self._digest(state)}"

    def _session_key(self, session_id: str) -> str:
        return f"{self.config.redis_key_prefix}:session:{self._digest(session_id)}"

    def _user_sessions_key(self, app_user_id: str) -> str:
        return f"{self.config.redis_key_prefix}:user_sessions:{app_user_id}"

    def _digest(self, value: str) -> str:
        secret = self.config.session_secret or ""
        return hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


class MemorySessionStore:
    """Small in-memory store for API tests and auth-disabled local runs."""

    def __init__(self, config: AuthConfig) -> None:
        self.config = config
        self.values: dict[str, tuple[float, str]] = {}
        self.user_sessions: dict[str, set[str]] = {}

    def create_oauth_state(self, return_to: str) -> str:
        state = secrets.token_urlsafe(32)
        self._set_json(self._state_key(state), {"returnTo": return_to}, self.config.oauth_state_ttl_seconds)
        return state

    def pop_oauth_state(self, state: str) -> dict[str, Any] | None:
        key = self._state_key(state)
        raw = self._get(key)
        self.values.pop(key, None)
        return json.loads(raw) if raw else None

    def create_session(self, user: AuthenticatedUser, ttl_seconds: int | None = None) -> str:
        session_id = secrets.token_urlsafe(48)
        ttl = ttl_seconds if ttl_seconds and ttl_seconds > 0 else self.config.session_ttl_seconds
        key = self._session_key(session_id)
        self._set_json(key, {"user": user.to_session()}, ttl)
        if user.app_user_id:
            self.user_sessions.setdefault(user.app_user_id, set()).add(key)
        return session_id

    def get_session(self, session_id: str | None) -> AuthenticatedUser | None:
        if not session_id:
            return None
        raw = self._get(self._session_key(session_id))
        if not raw:
            return None
        payload = json.loads(raw)
        return AuthenticatedUser.from_session(payload["user"])

    def update_session(self, session_id: str, user: AuthenticatedUser) -> None:
        key = self._session_key(session_id)
        raw = self._get(key)
        if not raw:
            return
        payload = json.loads(raw)
        payload["user"] = user.to_session()
        expires_at, _value = self.values[key]
        self.values[key] = (expires_at, json.dumps(payload, ensure_ascii=False))

    def delete_session(self, session_id: str | None) -> None:
        if session_id:
            self.values.pop(self._session_key(session_id), None)

    def delete_sessions_for_user(self, app_user_id: str | None) -> int:
        if not app_user_id:
            return 0
        keys = self.user_sessions.pop(app_user_id, set())
        deleted = 0
        for key in keys:
            if self.values.pop(key, None) is not None:
                deleted += 1
        return deleted

    def session_ttl(self, session_id: str) -> float | None:
        """테스트용: 남은 수명(초)."""
        stored = self.values.get(self._session_key(session_id))
        return (stored[0] - time.time()) if stored else None

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
    resolved_config = config or AuthConfig.from_env()
    if resolved_config.enabled:
        resolved_config.require_session_settings()
    store = RedisSessionStore.from_env(resolved_config)
    app.state.auth_session_store = store
    return store
