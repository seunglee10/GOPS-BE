"""비밀번호 해시. 표준 라이브러리 scrypt 만 쓴다 (의존성 추가 없음).

저장 형식: scrypt$<n>$<r>$<p>$<salt b64>$<hash b64>
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SALT_BYTES = 16

PASSWORD_MIN_LENGTH = 8
_HAS_LETTER = re.compile(r"[A-Za-z]")
_HAS_DIGIT = re.compile(r"\d")


def password_is_strong(password: str) -> bool:
    """8자 이상, 영문 1자 이상, 숫자 1자 이상."""
    if not isinstance(password, str) or len(password) < PASSWORD_MIN_LENGTH:
        return False
    return bool(_HAS_LETTER.search(password)) and bool(_HAS_DIGIT.search(password))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return "$".join(
        (
            "scrypt",
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            _b64encode(salt),
            _b64encode(digest),
        )
    )


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not isinstance(password, str):
        return False
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = _b64decode(salt_b64)
        expected = _b64decode(digest_b64)
        actual = _scrypt(password, salt, int(n), int(r), int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, dklen: int = SCRYPT_DKLEN) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=dklen,
        maxmem=128 * n * r * 2,
    )


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
