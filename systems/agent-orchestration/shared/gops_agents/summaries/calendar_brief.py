"""캘린더 주간 요약(D28 GET /api/calendar/brief) 생성기.

출력: {week, generatedAt, text}. week 는 한국 시간 ISO 주(YYYY-Www), text 는 해요체 한두 문장.
그 주의 주요(level=major) 경제지표·실적 일정만 말하고, 예상치·전망·매매 권유는 넣지 않는다.
LLM 이 꺼져 있거나 실패·검증 실패하면 템플릿 문장으로 만든다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .llm import JsonModelClient, SummaryModelError
from .text_rules import clean_text, isoformat_z, require_sentence, utc_now

logger = logging.getLogger(__name__)

SEOUL = ZoneInfo("Asia/Seoul")
PROMPT_VERSION = "calendar-brief.ko.v1"
TEXT_MAX_CHARS = 200
MAX_ECON = 4
MAX_EARN = 5
FALLBACK_EVENTS = 3
# 예상치·전망을 말하지 않게 한다. text_rules.FORBIDDEN_PHRASES 에 더해 이 말도 막는다.
EXTRA_FORBIDDEN = ("예상", "전망", "컨센서스", "%", "매수", "매도")
_QUALIFIER_SUFFIX = re.compile(r"\s+(전월 대비|전년 대비|전분기 대비|속보치|예비치|잠정치|확정치)(\s|$).*$")

INSTRUCTIONS = (
    "당신은 한국 개인 투자자에게 이번 주 증시 일정을 알려 주는 편집자입니다. "
    "입력 JSON 의 events 만 근거로 한두 문장을 씁니다. 주요 일정(경제지표 발표, 큰 기업 실적 발표)을 날짜와 함께 짚어 줍니다. "
    "모든 문장은 해요체로 끝냅니다(예: '29일 FOMC 금리 결정과 마이크로소프트 실적 발표가 이어져요.'). "
    "예상치·전망·숫자 예측, 사라·팔라는 말, 시장이 어떻게 될지에 대한 추측은 쓰지 않습니다. "
    f"{TEXT_MAX_CHARS}자 이내로 씁니다."
)

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}


@dataclass
class CalendarBriefInput:
    """week: YYYY-Www. items: 그 주의 D28 항목(market_data.economic_calendar.items 가 만든 형태)."""

    week: str
    items: list[dict[str, Any]] = field(default_factory=list)

    def has_data(self) -> bool:
        return bool(self.items)


class CalendarBriefGenerator:
    def __init__(self, model_client: JsonModelClient | None = None, *, now: Callable[[], datetime] | None = None) -> None:
        self.model_client = model_client
        self.now = now or utc_now

    def generate(self, data: CalendarBriefInput) -> dict[str, Any]:
        return self.generate_with_source(data)[0]

    def generate_with_source(self, data: CalendarBriefInput) -> tuple[dict[str, Any], str]:
        facts = brief_facts(data)
        base = {"week": data.week, "generatedAt": isoformat_z(self.now())}
        if self.model_client is not None and facts["events"]:
            try:
                output = self.model_client.generate_json(
                    name="calendar_brief_ko_v1",
                    instructions=INSTRUCTIONS,
                    payload={"promptVersion": PROMPT_VERSION, "week": data.week, **facts},
                    schema=OUTPUT_SCHEMA,
                )
                return {**base, "text": validate_brief_text(output, facts)}, "llm"
            except (SummaryModelError, ValueError, TypeError, KeyError) as exc:
                logger.warning("calendar brief model fallback week=%s reason=%s", data.week, exc)
        return {**base, "text": template_text(facts)}, "template"


# ---------- 사실 정리 ----------

def brief_facts(data: CalendarBriefInput) -> dict[str, Any]:
    """주요 일정만 골라 모델·템플릿에 넘긴다. 주요가 하나도 없으면 아무 일정이나 앞에서 몇 개."""
    rows = sorted(data.items, key=lambda item: (str(item.get("date") or ""), str(item.get("at") or "99:99")))
    major = [item for item in rows if item.get("level") == "major"]
    chosen = major or rows[:FALLBACK_EVENTS]
    events = []
    seen: set[tuple[str, str]] = set()
    for item in chosen:
        kind = item.get("kind")
        title = short_title(item)
        key = (str(kind), title)
        if not title or key in seen:
            continue
        seen.add(key)
        events.append({
            "date": str(item.get("date") or ""),
            "day": _day_label(item.get("date")),
            "kind": kind,
            "region": item.get("region"),
            "title": title,
        })
    return {"events": events, "hasMajor": bool(major)}


def short_title(item: dict[str, Any]) -> str:
    if item.get("kind") == "earn":
        return clean_text(item.get("nameKo") or item.get("symbol"), 40)
    title = clean_text(item.get("title"), 60)
    return _QUALIFIER_SUFFIX.sub("", title).strip()


def _day_label(value: Any) -> str:
    try:
        return f"{date.fromisoformat(str(value)[:10]).day}일"
    except ValueError:
        return ""


# ---------- 검증 ----------

def validate_brief_text(output: Any, facts: dict[str, Any]) -> str:
    if not isinstance(output, dict):
        raise ValueError("output must be an object")
    text = require_sentence(output.get("text"), field="text", max_length=TEXT_MAX_CHARS)
    if any(word in text for word in EXTRA_FORBIDDEN):
        raise ValueError("text mentions a forecast or trading word")
    if not any(event["title"] and event["title"] in text for event in facts["events"]):
        raise ValueError("text does not mention any input event")
    return text


# ---------- 템플릿 ----------

def template_text(facts: dict[str, Any]) -> str:
    events = facts["events"]
    if not events:
        return "이번 주에는 예정된 주요 일정이 없어요."
    econ = [event for event in events if event["kind"] == "econ"][:MAX_ECON]
    earn = [event for event in events if event["kind"] == "earn"][:MAX_EARN]
    econ_text = ", ".join(f"{event['day']} {event['title']}".strip() for event in econ)
    earn_text = ", ".join(event["title"] for event in earn)
    prefix = "이번 주에는" if facts.get("hasMajor") else "이번 주에는 큰 일정이 적고,"
    if econ and earn:
        return f"{prefix} {econ_text} 발표가 있고, {earn_text} 실적 발표도 이어져요."
    if econ:
        return f"{prefix} {econ_text} 발표가 있어요."
    return f"{prefix} {earn_text} 실적 발표가 이어져요."


# ---------- 주 계산 ----------

def iso_week(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def week_bounds(week: str) -> tuple[date, date]:
    year, number = week.split("-W")
    monday = date.fromisocalendar(int(year), int(number), 1)
    return monday, monday + timedelta(days=6)


def current_and_next_week(now: datetime) -> list[str]:
    today = now.astimezone(SEOUL).date()
    return [iso_week(today), iso_week(today + timedelta(days=7))]


def seoul_day_utc_bounds(from_date: date, to_date: date) -> tuple[str, str]:
    start = datetime.combine(from_date, time.min, SEOUL).astimezone(timezone.utc)
    end = datetime.combine(to_date + timedelta(days=1), time.min, SEOUL).astimezone(timezone.utc)
    return start.strftime("%Y-%m-%dT%H:%M:%S.000Z"), end.strftime("%Y-%m-%dT%H:%M:%S.000Z")
