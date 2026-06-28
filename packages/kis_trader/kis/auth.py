"""KIS demo token issuance and cache handling."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path

import requests

from .config import KisConfig
from .fake import KisTokenExpired


class KisAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class AccessToken:
    token: str
    expires_at: datetime

    def is_valid(self) -> bool:
        return bool(self.token) and self.expires_at > datetime.now() + timedelta(seconds=60)


class TokenCache:
    def __init__(self, path: Path, *, app_key: str, env: str = "demo") -> None:
        self.path = path
        self.app_key_hash = sha256(app_key.encode("utf-8")).hexdigest()
        self.env = env

    def load(self) -> AccessToken | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("env") != self.env or payload.get("app_key_hash") != self.app_key_hash:
                return None
            token = AccessToken(
                token=str(payload["access_token"]),
                expires_at=datetime.fromisoformat(str(payload["expires_at"])),
            )
        except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError):
            return None
        return token if token.is_valid() else None

    def save(self, token: AccessToken) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "env": self.env,
                    "app_key_hash": self.app_key_hash,
                    "access_token": token.token,
                    "expires_at": token.expires_at.isoformat(sep=" "),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class KisAuthClient:
    def __init__(self, config: KisConfig) -> None:
        self.config = config
        self.cache = TokenCache(config.token_cache_path, app_key=config.app_key, env=config.env)
        self._access_token: AccessToken | None = None

    def get_access_token(self) -> str:
        if self._access_token and self._access_token.is_valid():
            return self._access_token.token
        cached = self.cache.load()
        if cached:
            self._access_token = cached
            return cached.token
        issued = self._issue_token()
        self.cache.save(issued)
        self._access_token = issued
        return issued.token

    def invalidate_access_token(self) -> None:
        self._access_token = None
        self.cache.clear()

    def trading_headers(self, *, tr_id: str, tr_cont: str = "") -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "text/plain",
            "charset": "UTF-8",
            "User-Agent": self.config.user_agent,
            "authorization": f"Bearer {self.get_access_token()}",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
            "tr_cont": tr_cont,
        }

    def _issue_token(self) -> AccessToken:
        try:
            response = requests.post(
                f"{self.config.base_url}/oauth2/tokenP",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "text/plain",
                    "charset": "UTF-8",
                    "User-Agent": self.config.user_agent,
                },
                json={
                    "grant_type": "client_credentials",
                    "appkey": self.config.app_key,
                    "appsecret": self.config.app_secret,
                },
                timeout=self.config.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise KisAuthError(f"Token request failed: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise KisAuthError(f"Token response is not JSON: HTTP {response.status_code}") from exc
        if response.status_code != 200:
            raise KisAuthError(f"Token request failed: HTTP {response.status_code}")
        token = payload.get("access_token")
        expires_at = payload.get("access_token_token_expired")
        if not token or not expires_at:
            raise KisAuthError("Token response missing access token fields")
        try:
            parsed_expiry = datetime.strptime(str(expires_at), "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise KisAuthError(f"Unexpected token expiry format: {expires_at}") from exc
        return AccessToken(token=str(token), expires_at=parsed_expiry)


def classify_auth_error(exc: Exception) -> KisTokenExpired:
    return KisTokenExpired(str(exc))
