from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from gops_agents.query_understanding import KoreanEntityResolver


EXPLICIT_ALERT_WORDS = ("알림", "알람", "alert")
EXPLICIT_ACTION_WORDS = ("설정", "등록", "추가", "알려", "울려", "줘", "notify", "set")
INTERVAL_PATTERNS = (
    (r"(?:1|일)\s*분봉", "1m"),
    (r"5\s*분봉", "5m"),
    (r"10\s*분봉", "10m"),
    (r"(?:1|한)\s*시간봉", "1h"),
    (r"4\s*시간봉", "4h"),
    (r"(?:일봉|1\s*[dD])", "1D"),
)
ABOVE_WORDS = ("이상", "위", "넘", "돌파", "오르", "상승", "급등", "커지")
BELOW_WORDS = ("이하", "아래", "밑", "내려", "떨어", "하락", "급락", "작아지")


def resolve_alert_command(
    text: str,
    *,
    context_symbol: str | None = None,
    context_interval: str | None = None,
) -> dict[str, Any]:
    normalized = str(text or "").strip()
    lowered = normalized.lower()
    if not normalized or not any(word in lowered for word in EXPLICIT_ALERT_WORDS):
        return {"status": "not_matched"}
    if not any(word in lowered for word in EXPLICIT_ACTION_WORDS):
        return {"status": "not_matched"}

    metrics = sum(keyword in lowered for keyword in ("rsi", "거래량", "가격", "주가", "현재가", "%", "퍼센트"))
    if metrics > 1 and any(word in lowered for word in ("그리고", "동시에", "이면서", "면서", " and ")):
        return {
            "status": "rejected",
            "reason": "compound_condition_not_supported",
            "clarification": "이번 버전에서는 한 알림에 한 조건만 설정할 수 있습니다.",
        }

    symbol_resolution = KoreanEntityResolver().resolve(normalized)
    symbol = symbol_resolution.symbol if symbol_resolution.status == "confirmed" else _normalized_symbol(context_symbol)
    if symbol is None:
        return {
            "status": "clarify",
            "reason": "symbol_required",
            "clarification": "어느 기업의 알림인지 기업명이나 티커를 알려주세요.",
        }

    interval = _interval_from_text(lowered) or _normalized_interval(context_interval)
    condition = _rsi_condition(lowered, interval)
    if condition is None:
        condition = _volume_condition(lowered, interval)
    if condition is None:
        condition = _price_change_condition(lowered)
    if condition is None:
        condition = _price_cross_condition(lowered)
    if condition is None:
        return {
            "status": "ai_fallback",
            "reason": "unsupported_expression",
            "symbol": symbol,
        }
    if condition.get("missing") == "interval":
        return {
            "status": "clarify",
            "reason": "interval_required",
            "symbol": symbol,
            "clarification": "거래량 조건은 몇 분봉 기준인지 알려주세요. 예: 5분봉",
        }
    if condition.get("missing") == "windowMin":
        return {
            "status": "clarify",
            "reason": "window_required",
            "symbol": symbol,
            "clarification": "가격 변동률을 몇 분 기준으로 볼지 알려주세요. 예: 10분",
        }
    condition.pop("missing", None)
    lifecycle = _lifecycle(lowered)
    return {
        "status": "ready",
        "symbol": symbol,
        "condition": condition,
        **lifecycle,
    }


class AlertCommandDraftStore:
    def __init__(self, redis_client: Any | None = None, ttl_seconds: int = 600) -> None:
        self.redis = redis_client
        self.ttl_seconds = ttl_seconds
        self.memory: dict[tuple[str, str], str] = {}

    def save(self, user_sub: str, text: str) -> str:
        draft_id = uuid.uuid4().hex
        if self.redis is not None:
            self.redis.setex(self._key(user_sub, draft_id), self.ttl_seconds, json.dumps({"text": text}, ensure_ascii=False))
        else:
            self.memory[(user_sub, draft_id)] = text
        return draft_id

    def consume(self, user_sub: str, draft_id: str | None) -> str | None:
        if not draft_id:
            return None
        if self.redis is not None:
            key = self._key(user_sub, draft_id)
            raw = self.redis.get(key)
            self.redis.delete(key)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                value = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                value = {}
            return str(value.get("text") or "").strip() or None
        return self.memory.pop((user_sub, draft_id), None)

    @staticmethod
    def _key(user_sub: str, draft_id: str) -> str:
        prefix = os.getenv("ALERT_COMMAND_DRAFT_PREFIX", "alerts:commands:draft")
        return f"{prefix}:{user_sub}:{draft_id}"


def _rsi_condition(text: str, interval: str | None) -> dict[str, Any] | None:
    if "rsi" not in text:
        return None
    match = re.search(r"rsi(?:\s*\(\s*(\d{1,3})\s*\))?.*?(\d+(?:\.\d+)?)\s*(이상|이하|위|아래|넘\w*|밑\w*)", text, re.IGNORECASE)
    if not match:
        return None
    period = int(match.group(1) or 14)
    return {
        "kind": "rsi_threshold",
        "operator": _operator(match.group(3)),
        "threshold": float(match.group(2)),
        "interval": interval or "1D",
        "period": period,
    }


def _volume_condition(text: str, interval: str | None) -> dict[str, Any] | None:
    if "거래량" not in text and "volume" not in text:
        return None
    relative = re.search(r"(?:평균|평소|average).*?(\d+(?:\.\d+)?)\s*배(?:\s*(이상|이하|위|아래|넘\w*|밑\w*))?", text)
    if relative is None:
        relative = re.search(r"(\d+(?:\.\d+)?)\s*배.*?(?:평균|평소|average)(?:\s*(이상|이하|위|아래|넘\w*|밑\w*))?", text)
    if relative is not None:
        return {
            "kind": "volume_relative",
            "operator": _operator(relative.group(2) or "이상"),
            "threshold": float(relative.group(1)),
            "interval": interval,
            "lookback": 20,
            **({"missing": "interval"} if interval is None else {}),
        }
    absolute = re.search(r"거래량.*?([\d,]+(?:\.\d+)?)\s*(이상|이하|위|아래|넘\w*|밑\w*|떨어\w*)", text)
    if absolute is None:
        return None
    return {
        "kind": "volume_absolute",
        "operator": _operator(absolute.group(2)),
        "threshold": float(absolute.group(1).replace(",", "")),
        "interval": interval,
        **({"missing": "interval"} if interval is None else {}),
    }


def _price_change_condition(text: str) -> dict[str, Any] | None:
    if "%" not in text and "퍼센트" not in text:
        return None
    threshold_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|퍼센트)", text)
    window_match = re.search(r"(\d{1,3})\s*(분|시간)", text)
    if threshold_match is None:
        return None
    direction = "below" if any(word in text for word in BELOW_WORDS) else "above" if any(word in text for word in ABOVE_WORDS) else "either"
    window_min = None
    if window_match is not None:
        window_min = int(window_match.group(1)) * (60 if window_match.group(2) == "시간" else 1)
    return {
        "kind": "price_change",
        "operator": direction,
        "threshold": float(threshold_match.group(1)),
        "windowMin": window_min,
        **({"missing": "windowMin"} if window_min is None else {}),
    }


def _price_cross_condition(text: str) -> dict[str, Any] | None:
    if not any(word in text for word in ("가격", "주가", "현재가", "$", "달러")):
        return None
    match = re.search(r"(?:가격|주가|현재가|\$).*?\$?\s*([\d,]+(?:\.\d+)?)\s*(?:달러|불)?\s*(이상|이하|위|아래|넘\w*|밑\w*|돌파\w*|떨어\w*|오르\w*|내려\w*)", text)
    if match is None:
        match = re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*(이상|이하|위|아래|넘\w*|밑\w*|돌파\w*|떨어\w*|오르\w*|내려\w*)", text)
    if match is None:
        return None
    return {
        "kind": "price_cross",
        "operator": _operator(match.group(2)),
        "threshold": float(match.group(1).replace(",", "")),
    }


def _operator(value: str) -> str:
    text = str(value or "").lower()
    return "below" if any(word in text for word in BELOW_WORDS) else "above"


def _interval_from_text(text: str) -> str | None:
    for pattern, interval in INTERVAL_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return interval
    return None


def _normalized_interval(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    return "1D" if raw.lower() == "1d" else raw.lower()


def _normalized_symbol(value: str | None) -> str | None:
    raw = str(value or "").strip().upper()
    return raw if re.fullmatch(r"[A-Z][A-Z0-9.]{0,11}", raw) else None


def _lifecycle(text: str) -> dict[str, Any]:
    repeat_match = re.search(r"(?:최대\s*)?(3|5|10)\s*(?:번|회)", text)
    if repeat_match:
        return {"repeatLimit": int(repeat_match.group(1))}
    if any(word in text for word in ("계속", "매번", "반복", "유지")):
        return {"repeatLimit": None}
    if any(word in text for word in ("장 마감까지", "오늘까지", "오늘만")):
        now = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
        close = now.replace(hour=16, minute=0, second=0, microsecond=0)
        return {"repeatLimit": None, "expiresAt": close.astimezone(timezone.utc).isoformat()}
    return {"repeatLimit": 1}
