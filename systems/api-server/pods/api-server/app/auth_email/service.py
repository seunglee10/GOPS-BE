"""이메일 가입·인증·로그인·비밀번호 재설정·약관 동의 규칙.

HTTP 는 모른다. 실패는 EmailAuthError(status, code, extra) 로 올리고 routes.py 가
`{ "detail": { code, ... } }` 응답으로 바꾼다.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.auth.models import AuthenticatedUser
from app.auth_email.passwords import hash_password, password_is_strong, verify_password
from app.auth_email.repository import (
    PROVIDER_EMAIL,
    PURPOSE_RESET,
    PURPOSE_VERIFY,
    EmailAuthRepository,
    EmailCredential,
)
from app.services.mailer import Mailer

CODE_LENGTH = 6
CODE_TTL = timedelta(minutes=5)
CODE_MAX_ATTEMPTS = 5
RESEND_INTERVAL = timedelta(seconds=30)

LOGIN_MAX_FAILURES = 5
LOGIN_LOCK_DURATION = timedelta(minutes=10)
REMEMBER_TTL_SECONDS = 30 * 24 * 60 * 60

RESET_TOKEN_BYTES = 32
RESET_TTL = timedelta(minutes=30)
RESET_PATH = "/forgot"

REQUIRED_CONSENTS = ("tos", "privacy", "age")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class EmailAuthError(Exception):
    def __init__(self, status_code: int, code: str, **extra: Any) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.extra = extra

    def payload(self) -> dict[str, Any]:
        return {"code": self.code, **self.extra}


@dataclass(frozen=True)
class SignupResult:
    credential: EmailCredential
    resend_after_seconds: int = int(RESEND_INTERVAL.total_seconds())


@dataclass(frozen=True)
class LoginResult:
    user: AuthenticatedUser
    session_ttl_seconds: int | None  # None 이면 기본 세션 수명


@dataclass(frozen=True)
class ResetResult:
    app_user_id: str
    email: str


def normalize_email(value: Any) -> str:
    if not isinstance(value, str):
        raise EmailAuthError(422, "invalid_email")
    email = value.strip().lower()
    if not email or len(email) > 320 or not _EMAIL_RE.match(email):
        raise EmailAuthError(422, "invalid_email")
    return email


def normalize_consents(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        raise EmailAuthError(422, "consent_required")
    consents = {key: bool(value.get(key)) for key in (*REQUIRED_CONSENTS, "mkt")}
    if not all(consents[key] for key in REQUIRED_CONSENTS):
        raise EmailAuthError(422, "consent_required")
    return consents


def email_user(credential: EmailCredential) -> AuthenticatedUser:
    """소셜 로그인과 같은 세션 객체. sub 는 이메일, provider 는 'email'."""
    return AuthenticatedUser(
        sub=credential.email,
        email=credential.email,
        email_verified=credential.verified,
        name=None,
        picture=None,
        app_user_id=credential.app_user_id,
        provider=PROVIDER_EMAIL,
    )


class EmailAuthService:
    def __init__(
        self,
        repository: EmailAuthRepository,
        mailer: Mailer,
        *,
        public_base_url: str | None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.mailer = mailer
        self.public_base_url = (public_base_url or "").rstrip("/")
        self._now = now or (lambda: datetime.now(timezone.utc))

    # -- 가입 ----------------------------------------------------------------

    def signup(self, email_value: Any, password: Any, consents_value: Any) -> SignupResult:
        email = normalize_email(email_value)
        if not password_is_strong(password):
            raise EmailAuthError(422, "weak_password")
        consents = normalize_consents(consents_value)

        existing = self.repository.find_credential(email)
        if existing is not None and existing.verified:
            raise EmailAuthError(409, "email_taken")

        now = self._now()
        password_hash = hash_password(password)
        if existing is None:
            credential = self.repository.create_credential(email, password_hash)
        else:
            # 인증을 못 끝낸 가입은 다시 시도할 수 있다. 코드는 그 메일로만 오니
            # 남의 미인증 가입을 가로채도 얻는 게 없다.
            self.repository.update_password(existing.app_user_id, password_hash)
            credential = EmailCredential(existing.app_user_id, email, password_hash, None)
        self.repository.save_consent(credential.app_user_id, consented_at=now, **consents)
        self._issue_code(email, now)
        return SignupResult(credential=credential)

    def resend(self, email_value: Any) -> int:
        email = normalize_email(email_value)
        now = self._now()
        latest = self.repository.latest_verification(email, PURPOSE_VERIFY)
        if latest is not None:
            elapsed = now - latest.created_at
            if elapsed < RESEND_INTERVAL:
                retry_after = max(1, int((RESEND_INTERVAL - elapsed).total_seconds() + 0.999))
                raise EmailAuthError(429, "too_soon", retryAfterSeconds=retry_after)
        credential = self.repository.find_credential(email)
        # 모르는 이메일·이미 인증된 계정에도 같은 응답을 준다. 존재 여부를 흘리지 않는다.
        if credential is not None and not credential.verified:
            self._issue_code(email, now)
        return int(RESEND_INTERVAL.total_seconds())

    def verify(self, email_value: Any, code_value: Any) -> AuthenticatedUser:
        email = normalize_email(email_value)
        code = str(code_value or "").strip()
        now = self._now()
        record = self.repository.latest_verification(email, PURPOSE_VERIFY)
        if record is None or record.consumed_at is not None or record.attempts >= CODE_MAX_ATTEMPTS:
            raise EmailAuthError(410, "expired")
        if record.expires_at <= now:
            raise EmailAuthError(410, "expired")
        if not hmac.compare_digest(record.code_hash, self._code_hash(PURPOSE_VERIFY, email, code)):
            attempts = self.repository.increment_attempts(record.id)
            remaining = max(0, CODE_MAX_ATTEMPTS - attempts)
            raise EmailAuthError(400, "invalid_code", remainingAttempts=remaining)

        credential = self.repository.find_credential(email)
        if credential is None:
            raise EmailAuthError(410, "expired")
        self.repository.consume_verification(record.id, now)
        self.repository.mark_verified(credential.app_user_id, now)
        self.repository.clear_login_attempts(email)
        return email_user(EmailCredential(credential.app_user_id, email, credential.password_hash, now))

    # -- 로그인 --------------------------------------------------------------

    def login(self, email_value: Any, password: Any, remember: bool) -> LoginResult:
        email = normalize_email(email_value)
        now = self._now()
        attempt = self.repository.get_login_attempt(email)
        if attempt is not None and attempt.locked_until is not None and attempt.locked_until > now:
            raise EmailAuthError(423, "locked", lockedUntil=_iso(attempt.locked_until))

        credential = self.repository.find_credential(email)
        if credential is None or not verify_password(password, credential.password_hash):
            failed = 1 if attempt is None or (attempt.locked_until and attempt.locked_until <= now) else attempt.failed_count + 1
            if failed >= LOGIN_MAX_FAILURES:
                locked_until = now + LOGIN_LOCK_DURATION
                self.repository.save_login_attempt(email, failed, locked_until)
                raise EmailAuthError(423, "locked", lockedUntil=_iso(locked_until))
            self.repository.save_login_attempt(email, failed, None)
            raise EmailAuthError(401, "invalid_credentials", remainingAttempts=LOGIN_MAX_FAILURES - failed)

        if not credential.verified:
            raise EmailAuthError(403, "unverified")

        self.repository.clear_login_attempts(email)
        return LoginResult(
            user=email_user(credential),
            session_ttl_seconds=REMEMBER_TTL_SECONDS if remember else None,
        )

    # -- 비밀번호 재설정 -------------------------------------------------------

    def forgot(self, email_value: Any) -> None:
        email = normalize_email(email_value)
        credential = self.repository.find_credential(email)
        if credential is None:
            return  # 없는 이메일도 200. 존재 여부를 흘리지 않는다.
        now = self._now()
        token = secrets.token_urlsafe(RESET_TOKEN_BYTES)
        self.repository.create_verification(
            email, PURPOSE_RESET, self._token_hash(token), now + RESET_TTL, now
        )
        self.mailer.send_password_reset(email, self.reset_link(token))

    def reset(self, token_value: Any, password: Any) -> ResetResult:
        token = str(token_value or "").strip()
        if not token:
            raise EmailAuthError(400, "invalid_token")
        if not password_is_strong(password):
            raise EmailAuthError(422, "weak_password")
        now = self._now()
        record = self.repository.find_verification_by_hash(PURPOSE_RESET, self._token_hash(token))
        if record is None or record.consumed_at is not None:
            raise EmailAuthError(400, "invalid_token")
        if record.expires_at <= now:
            raise EmailAuthError(410, "expired")
        credential = self.repository.find_credential(record.email)
        if credential is None:
            raise EmailAuthError(400, "invalid_token")

        self.repository.consume_verification(record.id, now)
        self.repository.update_password(credential.app_user_id, hash_password(password))
        # 재설정 메일을 받았다는 것 자체가 이메일 소유 증명이다.
        self.repository.mark_verified(credential.app_user_id, now)
        self.repository.clear_login_attempts(record.email)
        return ResetResult(app_user_id=credential.app_user_id, email=record.email)

    def reset_link(self, token: str) -> str:
        return f"{self.public_base_url}{RESET_PATH}?step=reset&token={token}"

    # -- 약관 동의 -------------------------------------------------------------

    def needs_consent(self, user: AuthenticatedUser) -> bool:
        if not user.app_user_id:
            return False
        return self.repository.get_consent(user.app_user_id) is None

    def save_consent(self, user: AuthenticatedUser, consents_value: Any) -> None:
        if not user.app_user_id:
            raise EmailAuthError(422, "consent_required")
        consents = normalize_consents(consents_value)
        self.repository.save_consent(user.app_user_id, consented_at=self._now(), **consents)

    # -- helpers -------------------------------------------------------------

    def _issue_code(self, email: str, now: datetime) -> None:
        code = "".join(secrets.choice("0123456789") for _ in range(CODE_LENGTH))
        self.repository.create_verification(
            email, PURPOSE_VERIFY, self._code_hash(PURPOSE_VERIFY, email, code), now + CODE_TTL, now
        )
        self.mailer.send_verification_code(email, code)

    @staticmethod
    def _code_hash(purpose: str, email: str, code: str) -> str:
        return hashlib.sha256(f"{purpose}:{email}:{code}".encode("utf-8")).hexdigest()

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(f"{PURPOSE_RESET}:{token}".encode("utf-8")).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
