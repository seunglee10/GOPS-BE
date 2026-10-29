"""이메일 가입·인증·로그인·비밀번호 재설정·약관 동의 API (MOCK_API_CONTRACT D35~D39).

실패 응답은 `{ "code": ..., ... }` 를 본문 최상위에 둔다(계약서 모양). 세션은 소셜
로그인과 같은 저장소·쿠키를 쓴다.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.auth.config import AuthConfig, AuthConfigError
from app.auth.dependencies import require_current_user
from app.auth.models import AuthenticatedUser
from app.auth.session_store import SessionStoreError, session_store_from_app
from app.auth_email.repository import EmailAuthRepositoryError, email_auth_repository_from_app
from app.auth_email.service import EmailAuthError, EmailAuthService
from app.services.mailer import MailerError, mailer_from_app

router = APIRouter(tags=["auth-email"])


class SignupBody(BaseModel):
    email: str
    password: str
    consents: dict[str, Any] | None = None


class VerifyBody(BaseModel):
    email: str
    code: str


class EmailOnlyBody(BaseModel):
    email: str


class LoginBody(BaseModel):
    email: str
    password: str
    remember: bool = False


class ResetBody(BaseModel):
    token: str
    password: str


class ConsentBody(BaseModel):
    tos: bool = False
    privacy: bool = False
    age: bool = False
    mkt: bool = False


@router.post("/api/auth/email/signup", status_code=status.HTTP_201_CREATED)
def email_signup(request: Request, body: SignupBody) -> Response:
    config = _enabled_config()
    try:
        result = email_auth_service_from_app(request.app, config).signup(body.email, body.password, body.consents)
    except EmailAuthError as exc:
        return _error(exc)
    except (EmailAuthRepositoryError, MailerError, AuthConfigError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"verificationRequired": True, "resendAfterSeconds": result.resend_after_seconds},
    )


@router.post("/api/auth/email/verify")
def email_verify(request: Request, body: VerifyBody) -> Response:
    config = _enabled_config()
    try:
        user = email_auth_service_from_app(request.app, config).verify(body.email, body.code)
        return _session_response(request, config, user, ttl_seconds=None)
    except EmailAuthError as exc:
        return _error(exc)
    except (EmailAuthRepositoryError, SessionStoreError, AuthConfigError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.post("/api/auth/email/resend")
def email_resend(request: Request, body: EmailOnlyBody) -> Response:
    config = _enabled_config()
    try:
        resend_after = email_auth_service_from_app(request.app, config).resend(body.email)
    except EmailAuthError as exc:
        return _error(exc)
    except (EmailAuthRepositoryError, MailerError, AuthConfigError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return JSONResponse({"resendAfterSeconds": resend_after})


@router.post("/api/auth/email/login")
def email_login(request: Request, body: LoginBody) -> Response:
    config = _enabled_config()
    try:
        result = email_auth_service_from_app(request.app, config).login(body.email, body.password, body.remember)
        return _session_response(request, config, result.user, ttl_seconds=result.session_ttl_seconds)
    except EmailAuthError as exc:
        return _error(exc)
    except (EmailAuthRepositoryError, SessionStoreError, AuthConfigError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.post("/api/auth/password/forgot")
def password_forgot(request: Request, body: EmailOnlyBody) -> Response:
    config = _enabled_config()
    try:
        email_auth_service_from_app(request.app, config).forgot(body.email)
    except EmailAuthError as exc:
        return _error(exc)
    except (EmailAuthRepositoryError, MailerError, AuthConfigError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return JSONResponse({"sent": True})


@router.post("/api/auth/password/reset")
def password_reset(request: Request, body: ResetBody) -> Response:
    config = _enabled_config()
    try:
        result = email_auth_service_from_app(request.app, config).reset(body.token, body.password)
        # 그 사용자의 세션을 전부 지운다. 쿠키로 들어온 세션도 포함된다.
        session_store_from_app(request.app, config).delete_sessions_for_user(result.app_user_id)
    except EmailAuthError as exc:
        return _error(exc)
    except (EmailAuthRepositoryError, SessionStoreError, AuthConfigError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return JSONResponse({"reset": True})


@router.post("/api/auth/consent")
def auth_consent(
    request: Request,
    body: ConsentBody,
    user: AuthenticatedUser = Depends(require_current_user),
) -> Response:
    config = _enabled_config()
    try:
        email_auth_service_from_app(request.app, config).save_consent(user, body.model_dump())
    except EmailAuthError as exc:
        return _error(exc)
    except (EmailAuthRepositoryError, AuthConfigError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return JSONResponse({"needsConsent": False})


# ---------------------------------------------------------------------------


def email_auth_service_from_app(app: Any, config: AuthConfig | None = None) -> EmailAuthService:
    existing = getattr(app.state, "email_auth_service", None)
    if existing is not None:
        return existing
    resolved = config or AuthConfig.from_env()
    service = EmailAuthService(
        email_auth_repository_from_app(app),
        mailer_from_app(app),
        public_base_url=resolved.public_base_url,
    )
    app.state.email_auth_service = service
    return service


def set_session_cookie(response: Response, request: Request, config: AuthConfig, session_id: str, max_age: int) -> None:
    """소셜 콜백과 같은 쿠키 속성."""
    response.set_cookie(
        config.session_cookie_name,
        session_id,
        max_age=max_age,
        httponly=True,
        secure=config.cookie_secure(request),
        samesite=config.cookie_samesite,
        path="/",
    )


def _session_response(request: Request, config: AuthConfig, user: AuthenticatedUser, *, ttl_seconds: int | None) -> Response:
    config.require_session_settings()
    store = session_store_from_app(request.app, config)
    session_id = store.create_session(user, ttl_seconds=ttl_seconds)
    response = JSONResponse({"user": user.to_public()})
    set_session_cookie(response, request, config, session_id, ttl_seconds or config.session_ttl_seconds)
    return response


def _enabled_config() -> AuthConfig:
    config = AuthConfig.from_env()
    if not config.enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="auth is disabled")
    return config


def _error(exc: EmailAuthError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.payload())
