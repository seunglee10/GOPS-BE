from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit


PII_REDACTED_WARNING = "pii_redacted"
PROFANITY_REMOVED_WARNING = "profanity_removed"
SENSITIVE_URL_REDACTED_WARNING = "sensitive_url_redacted"

EMAIL_TOKEN = "[REDACTED_EMAIL]"
PHONE_TOKEN = "[REDACTED_PHONE]"
KR_RRN_TOKEN = "[REDACTED_KR_RRN]"
ACCOUNT_TOKEN = "[REDACTED_ACCOUNT]"
SECRET_TOKEN = "[REDACTED_SECRET]"
PROFANITY_TOKEN = "[FILTERED]"

SAFETY_WARNING_PRIORITY = (
    "direct_investment_command_removed",
    PII_REDACTED_WARNING,
    PROFANITY_REMOVED_WARNING,
    SENSITIVE_URL_REDACTED_WARNING,
)

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
KR_RRN_RE = re.compile(r"\b\d{6}[- ]?[1-4]\d{6}\b")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?82[-.\s]?)?0?1[016789][-\s.]?\d{3,4}[-.\s.]?\d{4}(?!\d)")
LANDLINE_RE = re.compile(r"(?<!\d)0(?:2|[3-6]\d)[-.\s]?\d{3,4}[-.\s]?\d{4}(?!\d)")
ACCOUNT_TEXT_RE = re.compile(r"(?i)(?:\baccount(?:\s*(?:no|number))?\b|\bacct\b|계좌(?:번호)?)[^\d]{0,12}[\d -]{8,}")
SECRET_TEXT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|bearer|secret|appsecret|app_secret)"
    r"\b\s*[:=]?\s*[A-Za-z0-9._~+/=-]{8,}"
)

PROFANITY_RE = re.compile(
    r"(?i)(fuck|shit|bitch|asshole|시\s*발|씨\s*발|개\s*새\s*끼|병\s*신|좆|꺼\s*져)"
)
NORMALIZED_PROFANITY_TERMS = (
    "fuck",
    "shit",
    "bitch",
    "asshole",
    "시발",
    "씨발",
    "개새끼",
    "병신",
    "좆",
    "꺼져",
)

SECRET_KEYS = (
    "authorization",
    "access_token",
    "accesstoken",
    "refresh_token",
    "refreshtoken",
    "id_token",
    "idtoken",
    "auth_token",
    "authtoken",
    "token",
    "api_key",
    "apikey",
    "secret",
    "appsecret",
    "app_secret",
    "password",
    "credential",
)
ACCOUNT_KEY_PARTS = ("account_no", "account_number", "accountno", "계좌")
URL_KEY_PARTS = ("url", "uri", "href", "link", "sourceurl")


@dataclass(frozen=True)
class SanitizationResult:
    value: Any
    warnings: list[str] = field(default_factory=list)


def sanitize_text(value: str) -> SanitizationResult:
    text = unicodedata.normalize("NFKC", str(value))
    warnings: list[str] = []

    def replace(pattern: re.Pattern[str], replacement: str, warning: str, current: str) -> str:
        updated = pattern.sub(replacement, current)
        if updated != current:
            warnings.append(warning)
        return updated

    text = replace(EMAIL_RE, EMAIL_TOKEN, PII_REDACTED_WARNING, text)
    text = replace(KR_RRN_RE, KR_RRN_TOKEN, PII_REDACTED_WARNING, text)
    text = replace(PHONE_RE, PHONE_TOKEN, PII_REDACTED_WARNING, text)
    text = replace(LANDLINE_RE, PHONE_TOKEN, PII_REDACTED_WARNING, text)
    text = replace(ACCOUNT_TEXT_RE, ACCOUNT_TOKEN, PII_REDACTED_WARNING, text)
    text = replace(SECRET_TEXT_RE, SECRET_TOKEN, PII_REDACTED_WARNING, text)
    text = replace(PROFANITY_RE, PROFANITY_TOKEN, PROFANITY_REMOVED_WARNING, text)

    if _contains_normalized_profanity(text):
        text = PROFANITY_TOKEN
        warnings.append(PROFANITY_REMOVED_WARNING)

    return SanitizationResult(text, merge_safety_warnings(warnings))


def sanitize_url(value: str | None) -> SanitizationResult:
    if value is None:
        return SanitizationResult(None, [])
    text = unicodedata.normalize("NFKC", str(value))
    warnings: list[str] = []
    parts = urlsplit(text)
    if parts.scheme and parts.netloc:
        netloc = parts.netloc
        if "@" in netloc:
            netloc = netloc.rsplit("@", 1)[1]
            warnings.append(SENSITIVE_URL_REDACTED_WARNING)
        if parts.query or parts.fragment:
            warnings.append(SENSITIVE_URL_REDACTED_WARNING)
        text = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    sanitized = sanitize_text(text)
    return SanitizationResult(sanitized.value, merge_safety_warnings([*warnings, *sanitized.warnings]))


def sanitize_value(value: Any, *, key: str | None = None) -> SanitizationResult:
    normalized_key = _normalize_key(key)
    if normalized_key and _is_secret_key(normalized_key):
        if value in {None, ""}:
            return SanitizationResult(value, [])
        return SanitizationResult(SECRET_TOKEN, [PII_REDACTED_WARNING])
    if normalized_key and _is_account_key(normalized_key):
        if value in {None, ""}:
            return SanitizationResult(value, [])
        return SanitizationResult(ACCOUNT_TOKEN, [PII_REDACTED_WARNING])
    if isinstance(value, str):
        if normalized_key and _is_url_key(normalized_key):
            return sanitize_url(value)
        return sanitize_text(value)
    if isinstance(value, dict):
        sanitized: dict[Any, Any] = {}
        warnings: list[str] = []
        for child_key, child_value in value.items():
            result = sanitize_value(child_value, key=str(child_key))
            sanitized[child_key] = result.value
            warnings.extend(result.warnings)
        return SanitizationResult(sanitized, merge_safety_warnings(warnings))
    if isinstance(value, list):
        values = []
        warnings = []
        for item in value:
            result = sanitize_value(item)
            values.append(result.value)
            warnings.extend(result.warnings)
        return SanitizationResult(values, merge_safety_warnings(warnings))
    if isinstance(value, tuple):
        values = []
        warnings = []
        for item in value:
            result = sanitize_value(item)
            values.append(result.value)
            warnings.extend(result.warnings)
        return SanitizationResult(tuple(values), merge_safety_warnings(warnings))
    return SanitizationResult(value, [])


def merge_safety_warnings(values: list[str] | tuple[str, ...]) -> list[str]:
    requested = [str(value or "").strip() for value in values if str(value or "").strip()]
    seen = set()
    ordered = []
    for warning in [*SAFETY_WARNING_PRIORITY, *requested]:
        text = str(warning or "").strip()
        if text and text not in seen and text in requested:
            seen.add(text)
            ordered.append(text)
    return ordered


def _contains_normalized_profanity(text: str) -> bool:
    compacted = re.sub(r"[^0-9A-Za-z가-힣]+", "", unicodedata.normalize("NFKC", text).lower())
    compacted = re.sub(r"(.)\1{2,}", r"\1\1", compacted)
    return any(term in compacted for term in NORMALIZED_PROFANITY_TERMS)


def _normalize_key(key: str | None) -> str:
    if key is None:
        return ""
    return re.sub(r"[^0-9a-zA-Z가-힣_]+", "", str(key)).lower()


def _is_secret_key(key: str) -> bool:
    return key in SECRET_KEYS or key.endswith("secret") or key.endswith("password") or key.endswith("credential")


def _is_account_key(key: str) -> bool:
    return any(part in key for part in ACCOUNT_KEY_PARTS)


def _is_url_key(key: str) -> bool:
    return any(part in key for part in URL_KEY_PARTS)
