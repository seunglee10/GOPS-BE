from __future__ import annotations

from fastapi import HTTPException, Request, WebSocket, status
from starlette.concurrency import run_in_threadpool

from app.auth.config import AuthConfig, AuthConfigError
from app.auth.identity import IdentityStoreError, deterministic_app_user_id, identity_resolver_from_app
from app.auth.models import AuthenticatedUser, AuthUserError
from app.auth.session_store import SessionStoreError, session_store_from_app
from kis_trader.persistence.user_context import bind_app_user_id


def auth_is_enabled() -> bool:
    return AuthConfig.from_env().enabled


def _resolve_optional_current_user(request: Request) -> AuthenticatedUser | None:
    config = AuthConfig.from_env()
    if not config.enabled:
        return None
    try:
        session_id = request.cookies.get(config.session_cookie_name)
        store = session_store_from_app(request.app, config)
        user = store.get_session(session_id)
        if user is not None and user.app_user_id is None:
            user = identity_resolver_from_app(request.app).resolve(user, provider="google")
            if session_id:
                store.update_session(session_id, user)
    except (AuthConfigError, AuthUserError, IdentityStoreError, SessionStoreError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return user


async def optional_current_user(request: Request) -> AuthenticatedUser | None:
    user = await run_in_threadpool(_resolve_optional_current_user, request)
    if user is not None:
        bind_app_user_id(user.app_user_id)
    return user


async def require_current_user(request: Request) -> AuthenticatedUser:
    config = AuthConfig.from_env()
    if not config.enabled:
        user = AuthenticatedUser.dev()
        resolved = AuthenticatedUser(
            user.sub, user.email, user.email_verified, user.name, user.picture,
            deterministic_app_user_id(user.sub),
        )
        bind_app_user_id(resolved.app_user_id)
        return resolved
    user = await optional_current_user(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")
    return user


def require_websocket_user(websocket: WebSocket) -> AuthenticatedUser:
    user = optional_websocket_user(websocket)
    if user is None:
        raise WebSocketAuthRequired("authentication required")
    return user


def optional_websocket_user(websocket: WebSocket) -> AuthenticatedUser | None:
    config = AuthConfig.from_env()
    if not config.enabled:
        user = AuthenticatedUser.dev()
        resolved = AuthenticatedUser(
            user.sub, user.email, user.email_verified, user.name, user.picture,
            deterministic_app_user_id(user.sub),
        )
        bind_app_user_id(resolved.app_user_id)
        return resolved
    try:
        session_id = websocket.cookies.get(config.session_cookie_name)
        store = session_store_from_app(websocket.app, config)
        user = store.get_session(session_id)
        if user is not None and user.app_user_id is None:
            user = identity_resolver_from_app(websocket.app).resolve(user, provider="google")
            if session_id:
                store.update_session(session_id, user)
    except (AuthConfigError, AuthUserError, IdentityStoreError, SessionStoreError) as exc:
        raise WebSocketAuthUnavailable(str(exc)) from exc
    if user is not None:
        bind_app_user_id(user.app_user_id)
    return user


class WebSocketAuthRequired(RuntimeError):
    """Raised when a WebSocket request has no valid session."""


class WebSocketAuthUnavailable(RuntimeError):
    """Raised when session storage cannot be checked."""
