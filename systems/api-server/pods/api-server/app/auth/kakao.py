from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.auth.config import AuthConfig
from app.auth.models import AuthenticatedUser
from app.auth.tokens import ProviderTokens


KAKAO_AUTHORIZATION_ENDPOINT = "https://kauth.kakao.com/oauth/authorize"
KAKAO_TOKEN_ENDPOINT = "https://kauth.kakao.com/oauth/token"
KAKAO_USERINFO_ENDPOINT = "https://kapi.kakao.com/v2/user/me"
KAKAO_UNLINK_ENDPOINT = "https://kapi.kakao.com/v1/user/unlink"



@dataclass(frozen=True)
class KakaoOAuthResult:
    user: AuthenticatedUser
    tokens: ProviderTokens


class KakaoOAuthClient:
    def __init__(self, config: AuthConfig) -> None:
        self.config = config

    def exchange_code(self, *, code: str, redirect_uri: str) -> KakaoOAuthResult:
        self.config.require_kakao_settings()
        token_payload = self._request_token(code, redirect_uri)
        access_token = _required_string(token_payload, "access_token")
        profile = self._request_userinfo(access_token)
        return KakaoOAuthResult(
            user=AuthenticatedUser.from_kakao_profile(profile),
            tokens=ProviderTokens.from_token_response(token_payload),
        )

    def _request_token(self, code: str, redirect_uri: str) -> dict[str, Any]:
        data = {
            "grant_type": "authorization_code",
            "client_id": self.config.kakao_client_id,
            "redirect_uri": redirect_uri,
            "code": code,
        }
        if self.config.kakao_client_secret:
            data["client_secret"] = self.config.kakao_client_secret

        with httpx.Client(timeout=10) as client:
            response = client.post(
                KAKAO_TOKEN_ENDPOINT,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
            )
        if response.status_code >= 400:
            raise KakaoOAuthError(f"Kakao token exchange failed: HTTP {response.status_code}")
        payload = response.json()

        if not isinstance(payload, dict):
            raise KakaoOAuthError("Kakao token exchange returned an invalid payload")
        return payload

    def _request_userinfo(self, access_token: str) -> dict[str, Any]:
        with httpx.Client(timeout=10) as client:
            response = client.get(
                KAKAO_USERINFO_ENDPOINT,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if response.status_code >= 400:
            raise KakaoOAuthError(f"Kakao user lookup failed: HTTP {response.status_code}")
        payload = response.json()

        if not isinstance(payload, dict):
            raise KakaoOAuthError("Kakao user lookup returned an invalid payload")
        return payload

    def unlink(self, access_token: str) -> str:
        with httpx.Client(timeout=10) as client:
            response = client.post(
                KAKAO_UNLINK_ENDPOINT,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if response.status_code >= 400:
            raise KakaoOAuthError(f"Kakao unlink failed: HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("id") is None:
            raise KakaoOAuthError("Kakao unlink returned an invalid payload")
        return str(payload["id"])


class KakaoOAuthError(RuntimeError):
    """Raised when Kakao OAuth cannot produce a verified user."""


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise KakaoOAuthError(f"Kakao token response is missing {key}")
    return value.strip()