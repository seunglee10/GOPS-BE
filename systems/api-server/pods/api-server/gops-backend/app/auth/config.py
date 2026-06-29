import os
from dataclasses import dataclass
from typing import Any


def read_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def read_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class AuthConfig:
    enabled: bool
    public_base_url: str | None
    google_client_id: str | None
    google_client_secret: str | None
    session_secret: str | None
    session_cookie_name: str
    oauth_state_cookie_name: str
    session_ttl_seconds: int
    oauth_state_ttl_seconds: int
    redis_key_prefix: str
    cookie_samesite: str
    cookie_secure_override: bool | None

    @classmethod
    def from_env(cls) -> "AuthConfig":
        secure_env = os.getenv("AUTH_COOKIE_SECURE")
        return cls(
            enabled=read_bool("AUTH_ENABLED", False),
            public_base_url=_clean_optional(os.getenv("AUTH_PUBLIC_BASE_URL")),
            google_client_id=_clean_optional(os.getenv("GOOGLE_OAUTH_CLIENT_ID")),
            google_client_secret=_clean_optional(os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")),
            session_secret=_clean_optional(os.getenv("AUTH_SESSION_SECRET")),
            session_cookie_name=os.getenv("AUTH_SESSION_COOKIE", "gops_session").strip() or "gops_session",
            oauth_state_cookie_name=os.getenv("AUTH_OAUTH_STATE_COOKIE", "gops_oauth_state").strip() or "gops_oauth_state",
            session_ttl_seconds=read_int("AUTH_SESSION_TTL_SECONDS", 8 * 60 * 60),
            oauth_state_ttl_seconds=read_int("AUTH_OAUTH_STATE_TTL_SECONDS", 5 * 60),
            redis_key_prefix=os.getenv("AUTH_REDIS_KEY_PREFIX", "gops:auth").strip().strip(":") or "gops:auth",
            cookie_samesite=os.getenv("AUTH_COOKIE_SAMESITE", "lax").strip().lower() or "lax",
            cookie_secure_override=None if secure_env is None else read_bool("AUTH_COOKIE_SECURE", False),
        )

    def require_oauth_settings(self) -> None:
        missing = [
            name
            for name, value in (
                ("GOOGLE_OAUTH_CLIENT_ID", self.google_client_id),
                ("GOOGLE_OAUTH_CLIENT_SECRET", self.google_client_secret),
                ("AUTH_SESSION_SECRET", self.session_secret),
            )
            if not value
        ]
        if missing:
            raise AuthConfigError(f"Missing auth settings: {', '.join(missing)}")

    def require_session_settings(self) -> None:
        if not self.session_secret:
            raise AuthConfigError("Missing auth settings: AUTH_SESSION_SECRET")

    def callback_url(self, request: Any) -> str:
        if self.public_base_url:
            return f"{self.public_base_url.rstrip('/')}/api/auth/google/callback"
        return str(request.url_for("google_oauth_callback"))

    def cookie_secure(self, request: Any | None = None) -> bool:
        if self.cookie_secure_override is not None:
            return self.cookie_secure_override
        if self.public_base_url:
            return self.public_base_url.startswith("https://")
        return bool(request and getattr(request.url, "scheme", "") == "https")


class AuthConfigError(RuntimeError):
    """Raised when auth is enabled without required configuration."""


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None

