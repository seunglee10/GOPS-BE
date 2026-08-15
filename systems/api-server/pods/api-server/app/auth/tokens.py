"""제공자가 발급한 토큰의 표현과 저장.

이 토큰들은 로그인 자격증명이 아니다. 사용자가 누구인지는 로그인 순간 한 번
확인하면 끝이고, 여기 보관하는 토큰은 나중에 제공자 API를 호출할 때 쓴다
(카카오 연결 끊기, 메시지 전송 등).

리프레시 토큰은 수명이 길다. 절대 API 응답이나 로그에 싣지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import psycopg

from app.auth.identity import database_conninfo


@dataclass(frozen=True)
class ProviderTokens:
    access_token: str
    refresh_token: str | None = None
    access_expires_in: int | None = None
    refresh_expires_in: int | None = None
    scope: str | None = None

    @classmethod
    def from_token_response(cls, payload: dict[str, Any]) -> "ProviderTokens":
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise TokenStoreError("token response is missing access_token")
        return cls(
            access_token=access_token.strip(),
            refresh_token=_optional_string(payload.get("refresh_token")),
            access_expires_in=_optional_int(payload.get("expires_in")),
            refresh_expires_in=_optional_int(payload.get("refresh_token_expires_in")),
            scope=_optional_string(payload.get("scope")),
        )

    def __repr__(self) -> str:
        # 실수로 로그에 찍혀도 값이 새지 않게 한다.
        return (
            f"ProviderTokens(access_token=***, refresh_token="
            f"{'***' if self.refresh_token else 'None'}, scope={self.scope!r})"
        )


class TokenStoreError(RuntimeError):
    """Raised when provider tokens cannot be stored."""


class TokenStore(Protocol):
    def save(self, *, provider: str, provider_subject: str, tokens: ProviderTokens) -> None: ...


class NullTokenStore:
    """PostgreSQL 설정이 없는 로컬/테스트 프로세스용. 아무것도 저장하지 않는다."""

    def save(self, *, provider: str, provider_subject: str, tokens: ProviderTokens) -> None:
        del provider, provider_subject, tokens


class PostgresTokenStore:
    """평문 저장. 암호화로 올릴 때는 이 클래스만 고치면 된다."""

    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo

    def save(self, *, provider: str, provider_subject: str, tokens: ProviderTokens) -> None:
        try:
            with psycopg.connect(self.conninfo) as conn:
                conn.execute(
                    """
                    INSERT INTO user_identity_tokens (
                        provider, provider_subject, access_token, refresh_token,
                        access_expires_at, refresh_expires_at, scope
                    ) VALUES (
                        %s, %s, %s, %s,
                        now() + make_interval(secs => %s),
                        now() + make_interval(secs => %s),
                        %s
                    )
                    ON CONFLICT (provider, provider_subject) DO UPDATE SET
                        access_token = EXCLUDED.access_token,
                        -- 제공자가 리프레시 토큰을 매번 주지는 않는다.
                        -- 안 왔다고 기존 값을 지우면 안 된다.
                        refresh_token = COALESCE(EXCLUDED.refresh_token, user_identity_tokens.refresh_token),
                        access_expires_at = EXCLUDED.access_expires_at,
                        refresh_expires_at = COALESCE(
                            EXCLUDED.refresh_expires_at, user_identity_tokens.refresh_expires_at
                        ),
                        scope = COALESCE(EXCLUDED.scope, user_identity_tokens.scope),
                        updated_at = now()
                    """,
                    (
                        provider,
                        provider_subject,
                        tokens.access_token,
                        tokens.refresh_token,
                        tokens.access_expires_in,
                        tokens.refresh_expires_in,
                        tokens.scope,
                    ),
                )
        except psycopg.Error as exc:
            raise TokenStoreError("provider token storage is unavailable") from exc


def token_store_from_app(app: Any) -> TokenStore:
    existing = getattr(app.state, "provider_token_store", None)
    if existing is not None:
        return existing
    conninfo = database_conninfo()
    store: TokenStore = PostgresTokenStore(conninfo) if conninfo else NullTokenStore()
    app.state.provider_token_store = store
    return store


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None
