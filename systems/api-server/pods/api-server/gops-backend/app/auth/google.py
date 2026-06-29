from __future__ import annotations

import time
from typing import Any

import httpx

from app.auth.config import AuthConfig
from app.auth.models import AuthenticatedUser


GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_TOKENINFO_ENDPOINT = "https://oauth2.googleapis.com/tokeninfo"
GOOGLE_USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"


class GoogleOAuthClient:
    def __init__(self, config: AuthConfig) -> None:
        self.config = config

    def exchange_code(self, *, code: str, redirect_uri: str) -> AuthenticatedUser:
        self.config.require_oauth_settings()
        token_payload = self._request_token(code, redirect_uri)
        id_token = _required_string(token_payload, "id_token")
        claims = self._verify_id_token(id_token)
        access_token = token_payload.get("access_token")
        if isinstance(access_token, str) and access_token:
            claims = {**claims, **self._request_userinfo(access_token)}
        return AuthenticatedUser.from_google_claims(claims)

    def _request_token(self, code: str, redirect_uri: str) -> dict[str, Any]:
        with httpx.Client(timeout=10) as client:
            response = client.post(
                GOOGLE_TOKEN_ENDPOINT,
                data={
                    "code": code,
                    "client_id": self.config.google_client_id,
                    "client_secret": self.config.google_client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
        if response.status_code >= 400:
            raise GoogleOAuthError(f"Google token exchange failed: HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise GoogleOAuthError("Google token exchange returned an invalid payload")
        return payload

    def _verify_id_token(self, id_token: str) -> dict[str, Any]:
        with httpx.Client(timeout=10) as client:
            response = client.get(GOOGLE_TOKENINFO_ENDPOINT, params={"id_token": id_token})
        if response.status_code >= 400:
            raise GoogleOAuthError(f"Google ID token validation failed: HTTP {response.status_code}")
        claims = response.json()
        if not isinstance(claims, dict):
            raise GoogleOAuthError("Google ID token validation returned an invalid payload")
        if claims.get("aud") != self.config.google_client_id:
            raise GoogleOAuthError("Google ID token audience does not match this application")
        if claims.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}:
            raise GoogleOAuthError("Google ID token issuer is invalid")
        exp = claims.get("exp")
        if exp is not None and int(str(exp)) <= int(time.time()):
            raise GoogleOAuthError("Google ID token is expired")
        return claims

    def _request_userinfo(self, access_token: str) -> dict[str, Any]:
        with httpx.Client(timeout=10) as client:
            response = client.get(GOOGLE_USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"})
        if response.status_code >= 400:
            return {}
        payload = response.json()
        return payload if isinstance(payload, dict) else {}


class GoogleOAuthError(RuntimeError):
    """Raised when Google OAuth cannot produce a verified user."""


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise GoogleOAuthError(f"Google token response is missing {key}")
    return value.strip()

