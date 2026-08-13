"""Request-local internal user context used by PostgreSQL RLS policies."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any


_current_app_user_id: ContextVar[str | None] = ContextVar("gops_current_app_user_id", default=None)


def bind_app_user_id(app_user_id: str | None) -> Token[str | None]:
    return _current_app_user_id.set(app_user_id)


def reset_app_user_id(token: Token[str | None]) -> None:
    _current_app_user_id.reset(token)


def current_app_user_id() -> str | None:
    return _current_app_user_id.get()


def apply_postgres_user_context(conn: Any) -> None:
    app_user_id = current_app_user_id()
    if app_user_id:
        conn.execute("SELECT set_config('app.current_user_id', %s, true)", (app_user_id,))
