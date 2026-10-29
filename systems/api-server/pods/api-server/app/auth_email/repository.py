"""이메일 가입·인증 코드·로그인 잠금·약관 동의 저장소.

PostgreSQL 설정(DATABASE_URL 또는 DATABASE_*)이 있으면 Postgres, 없으면 메모리.
chart_presets_repository 와 같은 방식으로 app.state.email_auth_repository 에 붙인다.

여기엔 규칙이 없다. 만료·시도 횟수·잠금 판단은 service.py 가 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row

from app.auth.identity import database_conninfo, deterministic_app_user_id

PROVIDER_EMAIL = "email"
PURPOSE_VERIFY = "verify"
PURPOSE_RESET = "reset"


class EmailAuthRepositoryError(RuntimeError):
    """저장소를 쓸 수 없을 때."""


@dataclass(frozen=True)
class EmailCredential:
    app_user_id: str
    email: str
    password_hash: str
    email_verified_at: datetime | None

    @property
    def verified(self) -> bool:
        return self.email_verified_at is not None


@dataclass(frozen=True)
class EmailVerification:
    id: int
    email: str
    code_hash: str
    purpose: str
    expires_at: datetime
    attempts: int
    consumed_at: datetime | None
    created_at: datetime
    pending_password_hash: str | None = None


@dataclass(frozen=True)
class LoginAttempt:
    email: str
    failed_count: int
    locked_until: datetime | None


@dataclass(frozen=True)
class Consent:
    app_user_id: str
    tos: bool
    privacy: bool
    age: bool
    mkt: bool
    consented_at: datetime


class EmailAuthRepository(Protocol):
    def find_credential(self, email: str) -> EmailCredential | None: ...
    def find_credential_by_user(self, app_user_id: str) -> EmailCredential | None: ...
    def create_credential(self, email: str, password_hash: str) -> EmailCredential: ...
    def update_password(self, app_user_id: str, password_hash: str) -> None: ...
    def mark_verified(self, app_user_id: str, verified_at: datetime) -> None: ...

    def create_verification(
        self,
        email: str,
        purpose: str,
        code_hash: str,
        expires_at: datetime,
        created_at: datetime,
        pending_password_hash: str | None = None,
    ) -> EmailVerification: ...
    def latest_verification(self, email: str, purpose: str) -> EmailVerification | None: ...
    def find_verification_by_hash(self, purpose: str, code_hash: str) -> EmailVerification | None: ...
    def increment_attempts(self, verification_id: int) -> int: ...
    def consume_verification(self, verification_id: int, consumed_at: datetime) -> None: ...

    def get_login_attempt(self, email: str) -> LoginAttempt | None: ...
    def save_login_attempt(self, email: str, failed_count: int, locked_until: datetime | None) -> None: ...
    def clear_login_attempts(self, email: str) -> None: ...

    def get_consent(self, app_user_id: str) -> Consent | None: ...
    def save_consent(self, app_user_id: str, *, tos: bool, privacy: bool, age: bool, mkt: bool, consented_at: datetime) -> Consent: ...


# ---------------------------------------------------------------------------
# 메모리
# ---------------------------------------------------------------------------


class InMemoryEmailAuthRepository:
    """테스트·DB 없는 로컬용. app_user_id 는 Postgres 와 같은 규칙으로 이메일에서 유도한다."""

    def __init__(self) -> None:
        self.credentials: dict[str, EmailCredential] = {}
        self.verifications: list[EmailVerification] = []
        self.login_attempts: dict[str, LoginAttempt] = {}
        self.consents: dict[str, Consent] = {}
        self._next_id = 1

    def find_credential(self, email: str) -> EmailCredential | None:
        return self.credentials.get(email)

    def find_credential_by_user(self, app_user_id: str) -> EmailCredential | None:
        for credential in self.credentials.values():
            if credential.app_user_id == app_user_id:
                return credential
        return None

    def create_credential(self, email: str, password_hash: str) -> EmailCredential:
        credential = EmailCredential(
            app_user_id=deterministic_app_user_id(email),
            email=email,
            password_hash=password_hash,
            email_verified_at=None,
        )
        self.credentials[email] = credential
        return credential

    def update_password(self, app_user_id: str, password_hash: str) -> None:
        credential = self.find_credential_by_user(app_user_id)
        if credential is not None:
            self.credentials[credential.email] = replace(credential, password_hash=password_hash)

    def mark_verified(self, app_user_id: str, verified_at: datetime) -> None:
        credential = self.find_credential_by_user(app_user_id)
        if credential is not None and credential.email_verified_at is None:
            self.credentials[credential.email] = replace(credential, email_verified_at=verified_at)

    def create_verification(
        self,
        email: str,
        purpose: str,
        code_hash: str,
        expires_at: datetime,
        created_at: datetime,
        pending_password_hash: str | None = None,
    ) -> EmailVerification:
        # 같은 이메일·목적의 살아 있는 이전 값은 무효화한다. 유효한 코드는 항상 하나다.
        self.verifications = [
            replace(item, consumed_at=created_at)
            if item.email == email and item.purpose == purpose and item.consumed_at is None
            else item
            for item in self.verifications
        ]
        record = EmailVerification(
            id=self._next_id,
            email=email,
            code_hash=code_hash,
            purpose=purpose,
            expires_at=expires_at,
            attempts=0,
            consumed_at=None,
            created_at=created_at,
            pending_password_hash=pending_password_hash,
        )
        self._next_id += 1
        self.verifications.append(record)
        return record

    def latest_verification(self, email: str, purpose: str) -> EmailVerification | None:
        matches = [item for item in self.verifications if item.email == email and item.purpose == purpose]
        return max(matches, key=lambda item: (item.created_at, item.id)) if matches else None

    def find_verification_by_hash(self, purpose: str, code_hash: str) -> EmailVerification | None:
        for item in reversed(self.verifications):
            if item.purpose == purpose and item.code_hash == code_hash:
                return item
        return None

    def increment_attempts(self, verification_id: int) -> int:
        for index, item in enumerate(self.verifications):
            if item.id == verification_id:
                updated = replace(item, attempts=item.attempts + 1)
                self.verifications[index] = updated
                return updated.attempts
        return 0

    def consume_verification(self, verification_id: int, consumed_at: datetime) -> None:
        for index, item in enumerate(self.verifications):
            if item.id == verification_id and item.consumed_at is None:
                self.verifications[index] = replace(item, consumed_at=consumed_at)

    def get_login_attempt(self, email: str) -> LoginAttempt | None:
        return self.login_attempts.get(email)

    def save_login_attempt(self, email: str, failed_count: int, locked_until: datetime | None) -> None:
        self.login_attempts[email] = LoginAttempt(email=email, failed_count=failed_count, locked_until=locked_until)

    def clear_login_attempts(self, email: str) -> None:
        self.login_attempts.pop(email, None)

    def get_consent(self, app_user_id: str) -> Consent | None:
        return self.consents.get(app_user_id)

    def save_consent(
        self, app_user_id: str, *, tos: bool, privacy: bool, age: bool, mkt: bool, consented_at: datetime
    ) -> Consent:
        consent = Consent(app_user_id, tos, privacy, age, mkt, consented_at)
        self.consents[app_user_id] = consent
        return consent


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


class PostgresEmailAuthRepository:
    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo

    @classmethod
    def from_env(cls) -> "PostgresEmailAuthRepository":
        conninfo = database_conninfo()
        if not conninfo:
            raise EmailAuthRepositoryError("DATABASE_URL or DATABASE_* settings are required")
        return cls(conninfo)

    # -- credentials --------------------------------------------------------

    def find_credential(self, email: str) -> EmailCredential | None:
        row = self._fetchone(
            "SELECT app_user_id, email, password_hash, email_verified_at FROM email_credentials WHERE email = %s",
            (email,),
        )
        return _credential(row)

    def find_credential_by_user(self, app_user_id: str) -> EmailCredential | None:
        row = self._fetchone(
            "SELECT app_user_id, email, password_hash, email_verified_at FROM email_credentials WHERE app_user_id = %s",
            (app_user_id,),
        )
        return _credential(row)

    def create_credential(self, email: str, password_hash: str) -> EmailCredential:
        # 소셜 로그인과 같은 함수로 app_users·user_identities 행을 만든다.
        # provider='email', subject=이메일. 그래서 app_user_id 유도 규칙도 같다.
        try:
            with self._connect() as conn:
                identity = conn.execute(
                    "SELECT gops_ensure_app_user_identity(%s, %s, %s, false, NULL, NULL) AS app_user_id",
                    (PROVIDER_EMAIL, email, email),
                ).fetchone()
                app_user_id = str(identity["app_user_id"]) if identity else None
                if not app_user_id:
                    raise EmailAuthRepositoryError("user identity resolver returned no app_user_id")
                row = conn.execute(
                    """
                    INSERT INTO email_credentials (app_user_id, email, password_hash)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (app_user_id) DO UPDATE SET
                        password_hash = EXCLUDED.password_hash,
                        updated_at = now()
                    RETURNING app_user_id, email, password_hash, email_verified_at
                    """,
                    (app_user_id, email, password_hash),
                ).fetchone()
                conn.commit()
        except psycopg.Error as exc:
            raise EmailAuthRepositoryError("email auth storage is unavailable") from exc
        credential = _credential(row)
        if credential is None:
            raise EmailAuthRepositoryError("email credential insert returned no row")
        return credential

    def update_password(self, app_user_id: str, password_hash: str) -> None:
        self._execute(
            "UPDATE email_credentials SET password_hash = %s, updated_at = now() WHERE app_user_id = %s",
            (password_hash, app_user_id),
        )

    def mark_verified(self, app_user_id: str, verified_at: datetime) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE email_credentials
                    SET email_verified_at = COALESCE(email_verified_at, %s), updated_at = now()
                    WHERE app_user_id = %s
                    """,
                    (verified_at, app_user_id),
                )
                conn.execute(
                    """
                    UPDATE user_identities
                    SET email_verified = true, updated_at = now()
                    WHERE provider = %s AND app_user_id = %s
                    """,
                    (PROVIDER_EMAIL, app_user_id),
                )
                conn.commit()
        except psycopg.Error as exc:
            raise EmailAuthRepositoryError("email auth storage is unavailable") from exc

    # -- verifications ------------------------------------------------------

    def create_verification(
        self,
        email: str,
        purpose: str,
        code_hash: str,
        expires_at: datetime,
        created_at: datetime,
        pending_password_hash: str | None = None,
    ) -> EmailVerification:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE email_verifications SET consumed_at = %s
                    WHERE email = %s AND purpose = %s AND consumed_at IS NULL
                    """,
                    (created_at, email, purpose),
                )
                row = conn.execute(
                    """
                    INSERT INTO email_verifications (
                        email, code_hash, purpose, expires_at, created_at, pending_password_hash
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id, email, code_hash, purpose, expires_at, attempts, consumed_at, created_at,
                              pending_password_hash
                    """,
                    (email, code_hash, purpose, expires_at, created_at, pending_password_hash),
                ).fetchone()
                conn.commit()
        except psycopg.Error as exc:
            raise EmailAuthRepositoryError("email auth storage is unavailable") from exc
        record = _verification(row)
        if record is None:
            raise EmailAuthRepositoryError("email verification insert returned no row")
        return record

    def latest_verification(self, email: str, purpose: str) -> EmailVerification | None:
        row = self._fetchone(
            """
            SELECT id, email, code_hash, purpose, expires_at, attempts, consumed_at, created_at,
                   pending_password_hash
            FROM email_verifications
            WHERE email = %s AND purpose = %s
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (email, purpose),
        )
        return _verification(row)

    def find_verification_by_hash(self, purpose: str, code_hash: str) -> EmailVerification | None:
        row = self._fetchone(
            """
            SELECT id, email, code_hash, purpose, expires_at, attempts, consumed_at, created_at,
                   pending_password_hash
            FROM email_verifications
            WHERE purpose = %s AND code_hash = %s
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (purpose, code_hash),
        )
        return _verification(row)

    def increment_attempts(self, verification_id: int) -> int:
        row = self._fetchone(
            "UPDATE email_verifications SET attempts = attempts + 1 WHERE id = %s RETURNING attempts",
            (verification_id,),
            commit=True,
        )
        return int(row["attempts"]) if row else 0

    def consume_verification(self, verification_id: int, consumed_at: datetime) -> None:
        self._execute(
            "UPDATE email_verifications SET consumed_at = %s WHERE id = %s AND consumed_at IS NULL",
            (consumed_at, verification_id),
        )

    # -- login attempts -----------------------------------------------------

    def get_login_attempt(self, email: str) -> LoginAttempt | None:
        row = self._fetchone(
            "SELECT email, failed_count, locked_until FROM login_attempts WHERE email = %s",
            (email,),
        )
        if not row:
            return None
        return LoginAttempt(
            email=str(row["email"]),
            failed_count=int(row["failed_count"]),
            locked_until=_aware(row["locked_until"]),
        )

    def save_login_attempt(self, email: str, failed_count: int, locked_until: datetime | None) -> None:
        self._execute(
            """
            INSERT INTO login_attempts (email, failed_count, locked_until, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (email) DO UPDATE SET
                failed_count = EXCLUDED.failed_count,
                locked_until = EXCLUDED.locked_until,
                updated_at = now()
            """,
            (email, failed_count, locked_until),
        )

    def clear_login_attempts(self, email: str) -> None:
        self._execute("DELETE FROM login_attempts WHERE email = %s", (email,))

    # -- consents -----------------------------------------------------------

    def get_consent(self, app_user_id: str) -> Consent | None:
        row = self._fetchone(
            "SELECT app_user_id, tos, privacy, age, mkt, consented_at FROM user_consents WHERE app_user_id = %s",
            (app_user_id,),
        )
        return _consent(row)

    def save_consent(
        self, app_user_id: str, *, tos: bool, privacy: bool, age: bool, mkt: bool, consented_at: datetime
    ) -> Consent:
        row = self._fetchone(
            """
            INSERT INTO user_consents (app_user_id, tos, privacy, age, mkt, consented_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (app_user_id) DO UPDATE SET
                tos = EXCLUDED.tos,
                privacy = EXCLUDED.privacy,
                age = EXCLUDED.age,
                mkt = EXCLUDED.mkt,
                consented_at = EXCLUDED.consented_at
            RETURNING app_user_id, tos, privacy, age, mkt, consented_at
            """,
            (app_user_id, tos, privacy, age, mkt, consented_at),
            commit=True,
        )
        consent = _consent(row)
        if consent is None:
            raise EmailAuthRepositoryError("consent insert returned no row")
        return consent

    # -- helpers ------------------------------------------------------------

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.conninfo, row_factory=dict_row)

    def _fetchone(self, sql: str, params: tuple[Any, ...], *, commit: bool = False) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                row = conn.execute(sql, params).fetchone()
                if commit:
                    conn.commit()
                return row
        except psycopg.Error as exc:
            raise EmailAuthRepositoryError("email auth storage is unavailable") from exc

    def _execute(self, sql: str, params: tuple[Any, ...]) -> None:
        try:
            with self._connect() as conn:
                conn.execute(sql, params)
                conn.commit()
        except psycopg.Error as exc:
            raise EmailAuthRepositoryError("email auth storage is unavailable") from exc


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _credential(row: dict[str, Any] | None) -> EmailCredential | None:
    if not row:
        return None
    return EmailCredential(
        app_user_id=str(row["app_user_id"]),
        email=str(row["email"]),
        password_hash=str(row["password_hash"]),
        email_verified_at=_aware(row["email_verified_at"]),
    )


def _verification(row: dict[str, Any] | None) -> EmailVerification | None:
    if not row:
        return None
    return EmailVerification(
        id=int(row["id"]),
        email=str(row["email"]),
        code_hash=str(row["code_hash"]),
        purpose=str(row["purpose"]),
        expires_at=_aware(row["expires_at"]) or datetime.now(timezone.utc),
        attempts=int(row["attempts"]),
        consumed_at=_aware(row["consumed_at"]),
        created_at=_aware(row["created_at"]) or datetime.now(timezone.utc),
        pending_password_hash=row.get("pending_password_hash") or None,
    )


def _consent(row: dict[str, Any] | None) -> Consent | None:
    if not row:
        return None
    return Consent(
        app_user_id=str(row["app_user_id"]),
        tos=bool(row["tos"]),
        privacy=bool(row["privacy"]),
        age=bool(row["age"]),
        mkt=bool(row["mkt"]),
        consented_at=_aware(row["consented_at"]) or datetime.now(timezone.utc),
    )


def email_auth_repository_from_app(app: Any) -> EmailAuthRepository:
    existing = getattr(app.state, "email_auth_repository", None)
    if existing is not None:
        return existing
    mode = getattr(app.state, "email_auth_repository_mode", None)
    if mode is None:
        mode = "postgres" if database_conninfo() else "memory"
    repository: EmailAuthRepository
    if str(mode) == "memory":
        repository = InMemoryEmailAuthRepository()
    else:
        repository = PostgresEmailAuthRepository.from_env()
    app.state.email_auth_repository = repository
    return repository


__all__ = [
    "Consent",
    "EmailAuthRepository",
    "EmailAuthRepositoryError",
    "EmailCredential",
    "EmailVerification",
    "InMemoryEmailAuthRepository",
    "LoginAttempt",
    "PostgresEmailAuthRepository",
    "PROVIDER_EMAIL",
    "PURPOSE_RESET",
    "PURPOSE_VERIFY",
    "email_auth_repository_from_app",
]
