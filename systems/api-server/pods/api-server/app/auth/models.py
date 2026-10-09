from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class AuthenticatedUser:
    sub: str
    email: str | None
    email_verified: bool
    name: str | None = None
    picture: str | None = None
    app_user_id: str | None = None
    provider: str = "google"

    @classmethod
    def dev(cls) -> "AuthenticatedUser":
        return cls(
            sub="dev-auth-disabled",
            email="dev@gops.local",
            email_verified=True,
            name="GOPS Dev",
            picture=None,
            app_user_id=None,
        )

    @classmethod
    def from_google_claims(cls, claims: dict[str, Any]) -> "AuthenticatedUser":
        sub = _required_string(claims, "sub")
        email = _required_string(claims, "email")
        email_verified = _read_bool(claims.get("email_verified"))
        if not email_verified:
            raise AuthUserError("Google account email is not verified")
        return cls(
            sub=sub,
            email=email,
            email_verified=True,
            name=_optional_string(claims.get("name")),
            picture=_optional_string(claims.get("picture")),
            app_user_id=None,
        )

    @classmethod
    def from_kakao_profile(cls, payload: dict[str, Any]) -> "AuthenticatedUser":
        raw_id = payload.get("id")
        if raw_id is None or not str(raw_id).strip():
            raise AuthUserError("Kakao identity is missing id")

        account = payload.get("kakao_account") or {}
        profile = account.get("profile") or {}

        email = _optional_string(account.get("email"))
        email_verified = (
            bool(email)
            and _read_bool(account.get("is_email_valid"))
            and _read_bool(account.get("is_email_verified"))
        )

        return cls(
            sub=str(raw_id).strip(),
            email=email,
            email_verified=email_verified,
            name=_optional_string(profile.get("nickname")),
            picture=_optional_string(profile.get("profile_image_url")),
            app_user_id=None,
            provider="kakao",
        )

    @classmethod
    def from_session(cls, payload: dict[str, Any]) -> "AuthenticatedUser":
        return cls(
            sub=_required_string(payload, "sub"),
            email=_optional_string(payload.get("email")),
            email_verified=_read_bool(payload.get("email_verified")),
            name=_optional_string(payload.get("name")),
            picture=_optional_string(payload.get("picture")),
            app_user_id=_optional_uuid_string(payload.get("app_user_id")),
            provider=_optional_string(payload.get("provider")) or "google",
        )

    def to_session(self) -> dict[str, Any]:
        payload = {
            "sub": self.sub,
            "email": self.email,
            "email_verified": self.email_verified,
            "name": self.name,
            "picture": self.picture,
            "provider": self.provider,
        }
        if self.app_user_id:
            payload["app_user_id"] = self.app_user_id
        return payload

    def to_public(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "name": self.name,
            "picture": self.picture,
            "provider": self.provider,
        }


class AuthUserError(RuntimeError):
    """Raised when a Google identity payload cannot become a GOPS user."""


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AuthUserError(f"identity is missing {key}")
    return value.strip()


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _read_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return False


def _optional_uuid_string(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return str(UUID(value.strip()))
    except ValueError as exc:
        raise AuthUserError("Session app_user_id is invalid") from exc
