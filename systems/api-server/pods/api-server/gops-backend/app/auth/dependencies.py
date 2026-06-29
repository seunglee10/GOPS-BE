from __future__ import annotations

from fastapi import HTTPException, Request, WebSocket, status

from app.auth.config import AuthConfig, AuthConfigError
from app.auth.models import AuthenticatedUser, AuthUserError
from app.auth.session_store import SessionStoreError, session_store_from_app


def auth_is_enabled() -> bool:
    return AuthConfig.from_env().enabled


def optional_current_user(request: Request) -> AuthenticatedUser | None:
    config = AuthConfig.from_env()
    if not config.enabled:
        return None
    try:
        user = session_store_from_app(request.app, config).get_session(request.cookies.get(config.session_cookie_name))
    except (AuthConfigError, AuthUserError, SessionStoreError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return user


def require_current_user(request: Request) -> AuthenticatedUser:
    config = AuthConfig.from_env()
    if not config.enabled:
        return AuthenticatedUser.dev()
    user = optional_current_user(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")
    return user


def require_websocket_user(websocket: WebSocket) -> AuthenticatedUser:
    config = AuthConfig.from_env()
    if not config.enabled:
        return AuthenticatedUser.dev()
    try:
        user = session_store_from_app(websocket.app, config).get_session(websocket.cookies.get(config.session_cookie_name))
    except (AuthConfigError, AuthUserError, SessionStoreError) as exc:
        raise WebSocketAuthUnavailable(str(exc)) from exc
    if user is None:
        raise WebSocketAuthRequired("authentication required")
    return user


class WebSocketAuthRequired(RuntimeError):
    """Raised when a WebSocket request has no valid session."""


class WebSocketAuthUnavailable(RuntimeError):
    """Raised when session storage cannot be checked."""

