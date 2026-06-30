import base64
import json
import os
from dataclasses import dataclass
from functools import lru_cache
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
    secret_error: str | None = None

    @classmethod
    def from_env(cls) -> "AuthConfig":
        enabled = read_bool("AUTH_ENABLED", False)
        secure_env = os.getenv("AUTH_COOKIE_SECURE")
        google_client_id = _clean_optional(os.getenv("GOOGLE_OAUTH_CLIENT_ID"))
        google_client_secret = _clean_optional(os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"))
        session_secret = _clean_optional(os.getenv("AUTH_SESSION_SECRET"))
        secret_error = None

        if enabled and (not google_client_id or not google_client_secret or not session_secret):
            try:
                secret_values = _load_configured_auth_secret_values()
            except AuthConfigError as exc:
                secret_values = {}
                secret_error = str(exc)
            google_client_id = google_client_id or _secret_string(
                secret_values,
                "GOOGLE_OAUTH_CLIENT_ID",
                "GOOGLE_CLIENT_ID",
                "google_client_id",
                "client_id",
            )
            google_client_secret = google_client_secret or _secret_string(
                secret_values,
                "GOOGLE_OAUTH_CLIENT_SECRET",
                "GOOGLE_CLIENT_SECRET",
                "google_client_secret",
                "client_secret",
            )
            session_secret = session_secret or _secret_string(
                secret_values,
                "AUTH_SESSION_SECRET",
                "SESSION_SECRET",
                "session_secret",
            )

        return cls(
            enabled=enabled,
            public_base_url=_clean_optional(os.getenv("AUTH_PUBLIC_BASE_URL")),
            google_client_id=google_client_id,
            google_client_secret=google_client_secret,
            session_secret=session_secret,
            session_cookie_name=os.getenv("AUTH_SESSION_COOKIE", "gops_session").strip() or "gops_session",
            oauth_state_cookie_name=os.getenv("AUTH_OAUTH_STATE_COOKIE", "gops_oauth_state").strip() or "gops_oauth_state",
            session_ttl_seconds=read_int("AUTH_SESSION_TTL_SECONDS", 8 * 60 * 60),
            oauth_state_ttl_seconds=read_int("AUTH_OAUTH_STATE_TTL_SECONDS", 5 * 60),
            redis_key_prefix=os.getenv("AUTH_REDIS_KEY_PREFIX", "gops:auth").strip().strip(":") or "gops:auth",
            cookie_samesite=os.getenv("AUTH_COOKIE_SAMESITE", "lax").strip().lower() or "lax",
            cookie_secure_override=None if secure_env is None else read_bool("AUTH_COOKIE_SECURE", False),
            secret_error=secret_error,
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
            if self.secret_error:
                raise AuthConfigError(f"{self.secret_error}; missing auth settings: {', '.join(missing)}")
            raise AuthConfigError(f"Missing auth settings: {', '.join(missing)}")

    def require_session_settings(self) -> None:
        if not self.session_secret:
            if self.secret_error:
                raise AuthConfigError(f"{self.secret_error}; missing auth settings: AUTH_SESSION_SECRET")
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


def _load_configured_auth_secret_values() -> dict[str, Any]:
    secret_name = _clean_optional(os.getenv("GOOGLE_OAUTH_SECRET_NAME")) or _clean_optional(os.getenv("AUTH_SECRET_NAME"))
    if not secret_name:
        return {}
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "ap-northeast-2"
    return _load_auth_secret_values(secret_name, region)


@lru_cache(maxsize=8)
def _load_auth_secret_values(secret_name: str, region: str) -> dict[str, Any]:
    try:
        import boto3

        response = boto3.client("secretsmanager", region_name=region).get_secret_value(SecretId=secret_name)
    except Exception as exc:
        raise AuthConfigError(f"Unable to load auth secret from AWS Secrets Manager: {secret_name}") from exc

    if response.get("SecretString"):
        raw_secret = response["SecretString"]
    elif response.get("SecretBinary"):
        raw_secret = base64.b64decode(response["SecretBinary"]).decode("utf-8")
    else:
        return {}

    try:
        parsed = json.loads(raw_secret)
    except json.JSONDecodeError as exc:
        raise AuthConfigError(f"Auth secret must be a JSON object: {secret_name}") from exc
    if not isinstance(parsed, dict):
        raise AuthConfigError(f"Auth secret must be a JSON object: {secret_name}")
    return parsed


def _secret_string(secret_values: dict[str, Any], *keys: str) -> str | None:
    for source in _secret_sources(secret_values):
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _secret_sources(secret_values: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    sources = [secret_values]
    for nested_key in ("web", "installed"):
        nested_value = secret_values.get(nested_key)
        if isinstance(nested_value, dict):
            sources.append(nested_value)
    return tuple(sources)


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None
