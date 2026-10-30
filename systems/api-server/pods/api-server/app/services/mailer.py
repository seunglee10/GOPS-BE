"""이메일 발송 추상화.

MAIL_PROVIDER=console (기본) 는 코드·링크를 로그에 찍는다. 로컬·테스트용이다.
MAIL_PROVIDER=ses 는 boto3 로 Amazon SES 를 호출한다. MAIL_FROM 이 필요하다.

메일 본문에 들어가는 값(인증 코드, 재설정 링크)은 1회용 비밀값이라 console 외의
경로에서는 로그에 남기지 않는다.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Protocol

logger = logging.getLogger("gops.mail")

SUBJECT_VERIFICATION = "[GOPS] 이메일 인증 코드"
SUBJECT_PASSWORD_RESET = "[GOPS] 비밀번호 재설정"


class MailerError(RuntimeError):
    """메일을 보낼 수 없을 때."""


class Mailer(Protocol):
    def send_verification_code(self, email: str, code: str) -> None: ...
    def send_password_reset(self, email: str, link: str) -> None: ...


def verification_code_body(code: str, expires_minutes: int) -> str:
    return (
        f"GOPS 이메일 인증 코드는 {code} 입니다.\n"
        f"{expires_minutes}분 안에 입력해 주세요. 직접 요청하지 않았다면 이 메일을 무시하세요.\n"
    )


def password_reset_body(link: str, expires_minutes: int) -> str:
    return (
        "아래 링크에서 GOPS 비밀번호를 새로 정할 수 있습니다.\n"
        f"{link}\n"
        f"링크는 {expires_minutes}분 동안만 유효합니다. 직접 요청하지 않았다면 이 메일을 무시하세요.\n"
    )


class ConsoleMailer:
    """로그로만 보낸다."""

    def __init__(self, *, code_expires_minutes: int = 5, reset_expires_minutes: int = 30) -> None:
        self.code_expires_minutes = code_expires_minutes
        self.reset_expires_minutes = reset_expires_minutes

    def send_verification_code(self, email: str, code: str) -> None:
        logger.info("[mail:console] to=%s subject=%s code=%s", email, SUBJECT_VERIFICATION, code)

    def send_password_reset(self, email: str, link: str) -> None:
        logger.info("[mail:console] to=%s subject=%s link=%s", email, SUBJECT_PASSWORD_RESET, link)


class SesMailer:
    def __init__(
        self,
        client: Any,
        *,
        sender: str,
        code_expires_minutes: int = 5,
        reset_expires_minutes: int = 30,
    ) -> None:
        self.client = client
        self.sender = sender
        self.code_expires_minutes = code_expires_minutes
        self.reset_expires_minutes = reset_expires_minutes

    @classmethod
    def from_env(cls) -> "SesMailer":
        sender = (os.getenv("MAIL_FROM") or "").strip()
        if not sender:
            raise MailerError("MAIL_PROVIDER=ses requires MAIL_FROM")
        region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "ap-northeast-2"
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - boto3 is a declared dependency
            raise MailerError("boto3 is required for MAIL_PROVIDER=ses") from exc
        return cls(boto3.client("ses", region_name=region), sender=sender)

    def send_verification_code(self, email: str, code: str) -> None:
        self._send(email, SUBJECT_VERIFICATION, verification_code_body(code, self.code_expires_minutes))

    def send_password_reset(self, email: str, link: str) -> None:
        self._send(email, SUBJECT_PASSWORD_RESET, password_reset_body(link, self.reset_expires_minutes))

    def _send(self, email: str, subject: str, body: str) -> None:
        try:
            self.client.send_email(
                Source=self.sender,
                Destination={"ToAddresses": [email]},
                Message={
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
                },
            )
        except Exception as exc:
            logger.warning("[mail:ses] send failed to=%s subject=%s", email, subject)
            raise MailerError("mail delivery failed") from exc


def mailer_from_env() -> Mailer:
    provider = (os.getenv("MAIL_PROVIDER") or "console").strip().lower()
    if provider == "ses":
        return SesMailer.from_env()
    if provider in {"", "console"}:
        return ConsoleMailer()
    raise MailerError(f"unknown MAIL_PROVIDER: {provider}")


def mailer_from_app(app: Any) -> Mailer:
    existing = getattr(app.state, "mailer", None)
    if existing is not None:
        return existing
    mailer = mailer_from_env()
    app.state.mailer = mailer
    return mailer
