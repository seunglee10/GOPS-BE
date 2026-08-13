"""Resolve public OAuth subjects to the internal GOPS user identifier."""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from typing import Any, Protocol
from uuid import UUID

import psycopg
from psycopg.conninfo import make_conninfo

from app.auth.models import AuthenticatedUser


class IdentityStoreError(RuntimeError):
    """Raised when the canonical user identity cannot be resolved."""


class IdentityResolver(Protocol):
    def resolve(self, user: AuthenticatedUser, *, provider: str = "google") -> AuthenticatedUser: ...


def deterministic_app_user_id(subject: str) -> str:
    digest = hashlib.md5(f"gops-app-user:{subject}".encode("utf-8"), usedforsecurity=False).hexdigest()
    return str(UUID(digest))


class DeterministicIdentityResolver:
    """Fallback used only when a local/test process has no PostgreSQL settings."""

    def resolve(self, user: AuthenticatedUser, *, provider: str = "google") -> AuthenticatedUser:
        del provider
        return replace(user, app_user_id=user.app_user_id or deterministic_app_user_id(user.sub))


class PostgresIdentityResolver:
    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo

    def resolve(self, user: AuthenticatedUser, *, provider: str = "google") -> AuthenticatedUser:
        if user.app_user_id:
            return user
        try:
            with psycopg.connect(self.conninfo) as conn:
                row = conn.execute(
                    """
                    SELECT gops_ensure_app_user_identity(%s, %s, %s, %s, %s, %s)
                    """,
                    (provider, user.sub, user.email, user.email_verified, user.name, user.picture),
                ).fetchone()
            if not row or not row[0]:
                raise IdentityStoreError("user identity resolver returned no app_user_id")
            return replace(user, app_user_id=str(row[0]))
        except psycopg.Error as exc:
            raise IdentityStoreError("user identity storage is unavailable") from exc


def identity_resolver_from_app(app: Any) -> IdentityResolver:
    existing = getattr(app.state, "user_identity_resolver", None)
    if existing is not None:
        return existing
    conninfo = _database_conninfo()
    resolver: IdentityResolver = PostgresIdentityResolver(conninfo) if conninfo else DeterministicIdentityResolver()
    app.state.user_identity_resolver = resolver
    return resolver


def _database_conninfo() -> str | None:
    if value := os.getenv("DATABASE_URL"):
        return value
    required = ("DATABASE_HOST", "DATABASE_NAME", "DATABASE_USER", "DATABASE_PASSWORD")
    if not all(os.getenv(name) for name in required):
        return None
    return make_conninfo(
        host=os.environ["DATABASE_HOST"],
        port=os.getenv("DATABASE_PORT", "5432"),
        dbname=os.environ["DATABASE_NAME"],
        user=os.environ["DATABASE_USER"],
        password=os.environ["DATABASE_PASSWORD"],
    )
