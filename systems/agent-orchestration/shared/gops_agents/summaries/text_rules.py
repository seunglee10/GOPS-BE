"""생성 문장 공통 검증 규칙.

- 해요체로 끝나야 한다("~요" / "~요.").
- 앞으로의 가격이나 사라·팔라는 말은 쓰지 않는다.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

FORBIDDEN_PHRASES = (
    "사세요",
    "파세요",
    "매수하세요",
    "매도하세요",
    "매수 추천",
    "매도 추천",
    "추천해요",
    "오를 거",
    "내릴 거",
    "오를 것",
    "내릴 것",
    "오를 전망",
    "내릴 전망",
    "목표가",
)

_SENTENCE_END = re.compile(r"요[.!]?$")


def clean_text(value: Any, max_length: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:max_length].rstrip()


def is_haeyo(text: str) -> bool:
    return bool(_SENTENCE_END.search(text.strip().rstrip("”\"'’)")))


def contains_forbidden(text: str) -> bool:
    return any(phrase in text for phrase in FORBIDDEN_PHRASES)


def require_sentence(value: Any, *, field: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = clean_text(value, max_length + 1)
    if not text:
        raise ValueError(f"{field} is empty")
    if len(text) > max_length:
        raise ValueError(f"{field} is too long")
    if not is_haeyo(text):
        raise ValueError(f"{field} must end in 해요체")
    if contains_forbidden(text):
        raise ValueError(f"{field} contains a forecast or recommendation phrase")
    return text


def finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def signed_percent(value: float) -> str:
    return f"{value:+.1f}%"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
