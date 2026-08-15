from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from app.auth.config import AuthConfig, AuthConfigError
from app.auth.dependencies import optional_current_user
from app.auth.identity import IdentityStoreError, identity_resolver_from_app
from app.auth.google import GOOGLE_AUTHORIZATION_ENDPOINT, GoogleOAuthClient, GoogleOAuthError
from app.auth.kakao import KAKAO_AUTHORIZATION_ENDPOINT, KakaoOAuthClient, KakaoOAuthError
from app.auth.models import AuthUserError
from app.auth.session_store import SessionStoreError, session_store_from_app
from app.auth.tokens import TokenStoreError, token_store_from_app


router = APIRouter(tags=["auth"])


@router.get("/api/auth/google/login")
def google_oauth_login(request: Request, return_to: str = Query(default="/", alias="returnTo")) -> Response:
    config = AuthConfig.from_env()
    safe_return_to = _safe_return_to(return_to)
    if not config.enabled:
        return RedirectResponse(safe_return_to)

    try:
        config.require_oauth_settings()
        store = session_store_from_app(request.app, config)
        state = store.create_oauth_state(safe_return_to)
    except (AuthConfigError, SessionStoreError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    redirect_uri = config.callback_url(request)
    params = urlencode(
        {
            "client_id": config.google_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "online",
            "prompt": "select_account",
        }
    )
    response = RedirectResponse(f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{params}")
    response.set_cookie(
        config.oauth_state_cookie_name,
        state,
        max_age=config.oauth_state_ttl_seconds,
        httponly=True,
        secure=config.cookie_secure(request),
        samesite=config.cookie_samesite,
        path="/",
    )
    return response


@router.get("/api/auth/google/callback", name="google_oauth_callback")
def google_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    config = AuthConfig.from_env()
    if not config.enabled:
        return RedirectResponse("/")
    if error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Google OAuth error: {error}")
    if not code or not state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google OAuth callback is missing code or state")
    if request.cookies.get(config.oauth_state_cookie_name) != state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google OAuth state did not match this browser")

    try:
        store = session_store_from_app(request.app, config)
        state_record = store.pop_oauth_state(state)
        if not state_record:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google OAuth state expired")
        user = google_oauth_client_from_app(request.app, config).exchange_code(
            code=code,
            redirect_uri=config.callback_url(request),
        )
        user = identity_resolver_from_app(request.app).resolve(user, provider="google")
        session_id = store.create_session(user)
    except HTTPException:
        raise
    except (AuthConfigError, IdentityStoreError, SessionStoreError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except (AuthUserError, GoogleOAuthError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    response = RedirectResponse(_safe_return_to(str(state_record.get("returnTo") or "/")))
    response.set_cookie(
        config.session_cookie_name,
        session_id,
        max_age=config.session_ttl_seconds,
        httponly=True,
        secure=config.cookie_secure(request),
        samesite=config.cookie_samesite,
        path="/",
    )
    response.delete_cookie(config.oauth_state_cookie_name, path="/")
    return response


# 카카오 로그인 버튼 눌렀을 때 카카오 로그인 화면으로 보내는 api
@router.get("/api/auth/kakao/login")
def kakao_oauth_login(request: Request, return_to: str = Query(default="/", alias="returnTo")) -> Response:
    config = AuthConfig.from_env()
    safe_return_to = _safe_return_to(return_to)
    if not config.enabled:
        return RedirectResponse(safe_return_to)

    try:
        config.require_kakao_settings()
        store = session_store_from_app(request.app, config)
        state = store.create_oauth_state(safe_return_to)
    except (AuthConfigError, SessionStoreError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    params = urlencode(
        {
            "client_id": config.kakao_client_id,
            "redirect_uri": config.callback_url(request, "kakao"),
            "response_type": "code",
            "state": state,
            "scope": "profile_nickname,profile_image",
        }
    )
    response = RedirectResponse(f"{KAKAO_AUTHORIZATION_ENDPOINT}?{params}")
    response.set_cookie(
        config.oauth_state_cookie_name,
        state,
        max_age=config.oauth_state_ttl_seconds,
        httponly=True,
        secure=config.cookie_secure(request),
        samesite=config.cookie_samesite,
        path="/",
    )
    return response


# 카카오 로그인 후 내 서비스로 돌아왔을 때 실행되는 callback api
@router.get("/api/auth/kakao/callback", name="kakao_oauth_callback")
def kakao_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    config = AuthConfig.from_env()
    if not config.enabled:
        return RedirectResponse("/")
    if error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Kakao OAuth error: {error}")
    if not code or not state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Kakao OAuth callback is missing code or state")
    if request.cookies.get(config.oauth_state_cookie_name) != state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Kakao OAuth state did not match this browser")

    try:
        store = session_store_from_app(request.app, config)
        state_record = store.pop_oauth_state(state)
        if not state_record:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Kakao OAuth state expired")
        result = kakao_oauth_client_from_app(request.app, config).exchange_code(
            code=code,
            redirect_uri=config.callback_url(request, "kakao"),
        )
        user = identity_resolver_from_app(request.app).resolve(result.user, provider="kakao")
        # identity 행이 만들어진 뒤에 저장한다. 토큰 테이블이 그 행을 참조한다.
        token_store_from_app(request.app).save(
            provider="kakao",
            provider_subject=user.sub,
            tokens=result.tokens,
        )
        session_id = store.create_session(user)
    except HTTPException:
        raise
    except (AuthConfigError, IdentityStoreError, SessionStoreError, TokenStoreError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except (AuthUserError, KakaoOAuthError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    response = RedirectResponse(_safe_return_to(str(state_record.get("returnTo") or "/")))
    response.set_cookie(
        config.session_cookie_name,
        session_id,
        max_age=config.session_ttl_seconds,
        httponly=True,
        secure=config.cookie_secure(request),
        samesite=config.cookie_samesite,
        path="/",
    )
    response.delete_cookie(config.oauth_state_cookie_name, path="/")
    return response


@router.get("/api/auth/me")
async def auth_me(request: Request) -> dict[str, Any]:
    config = AuthConfig.from_env()
    user = await optional_current_user(request)
    return {
        "authEnabled": config.enabled,
        "user": user.to_public() if user else None,
    }


@router.post("/api/auth/logout")
def auth_logout(request: Request) -> Response:
    config = AuthConfig.from_env()
    session_id = request.cookies.get(config.session_cookie_name)
    if config.enabled and session_id:
        try:
            session_store_from_app(request.app, config).delete_session(session_id)
        except SessionStoreError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(config.session_cookie_name, path="/")
    return response


def google_oauth_client_from_app(app: Any, config: AuthConfig) -> GoogleOAuthClient:
    existing = getattr(app.state, "google_oauth_client", None)
    if existing is not None:
        return existing
    client = GoogleOAuthClient(config)
    app.state.google_oauth_client = client
    return client


def kakao_oauth_client_from_app(app: Any, config: AuthConfig) -> KakaoOAuthClient:
    existing = getattr(app.state, "kakao_oauth_client", None)
    if existing is not None:
        return existing
    client = KakaoOAuthClient(config)
    app.state.kakao_oauth_client = client
    return client


def _safe_return_to(value: str) -> str:
    value = value.strip() or "/"
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    return value
