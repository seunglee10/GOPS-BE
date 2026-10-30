"""Finnhub 경제 캘린더 수집.

GET https://finnhub.io/api/v1/calendar/economic?from=YYYY-MM-DD&to=YYYY-MM-DD&token=...
응답: {"economicCalendar": [{"country": "US", "event": "CPI YoY", "impact": "high",
        "time": "2026-10-10 12:30:00", "actual": 3.1, "estimate": 3.0, "prev": 3.2, "unit": "%"}]}

time 은 UTC 다. 저장은 한국 시간 날짜·시각으로 바꿔서 한다(목업 캘린더가 한국 시간 기준).
"""

from __future__ import annotations

import hashlib
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .titles import importance_from_impact, korean_title

FINNHUB_ECONOMIC_CALENDAR_URL = "https://finnhub.io/api/v1/calendar/economic"
SEOUL = ZoneInfo("Asia/Seoul")
COUNTRY_REGIONS = {"US": "us", "KR": "kr"}
SOURCE = "finnhub"


class FinnhubError(RuntimeError):
    pass


@dataclass(frozen=True)
class EconomicEvent:
    event_id: str
    event_date: date
    event_time_local: str | None
    region: str
    title_ko: str | None
    title_en: str
    importance: str
    actual: str | None
    forecast: str | None
    previous: str | None
    unit: str | None
    source: str = SOURCE

    def as_row(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_date": self.event_date,
            "event_time_local": self.event_time_local,
            "region": self.region,
            "title_ko": self.title_ko,
            "title_en": self.title_en,
            "importance": self.importance,
            "actual": self.actual,
            "forecast": self.forecast,
            "previous": self.previous,
            "unit": self.unit,
            "source": self.source,
        }


def fetch_economic_calendar(
    api_key: str,
    from_date: date,
    to_date: date,
    *,
    urlopen: Callable[..., Any] | None = None,
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"from": from_date.isoformat(), "to": to_date.isoformat(), "token": api_key})
    request = urllib.request.Request(
        f"{FINNHUB_ECONOMIC_CALENDAR_URL}?{query}",
        headers={"Accept": "application/json", "User-Agent": "gops-economic-calendar/1"},
    )
    opener = urlopen or urllib.request.urlopen
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise FinnhubError(f"Finnhub economic calendar HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FinnhubError(f"Finnhub economic calendar request failed: {type(exc).__name__}") from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise FinnhubError("Finnhub economic calendar response was not JSON") from exc
    rows = payload.get("economicCalendar") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise FinnhubError("Finnhub economic calendar response has no economicCalendar list")
    return [row for row in rows if isinstance(row, dict)]


def normalize_events(rows: list[dict[str, Any]]) -> list[EconomicEvent]:
    """미국·한국 행만 골라 EconomicEvent 로 바꾼다. 같은 id 는 마지막 행이 이긴다."""
    events: dict[str, EconomicEvent] = {}
    for row in rows:
        event = normalize_event(row)
        if event is not None:
            events[event.event_id] = event
    return sorted(events.values(), key=lambda e: (e.event_date, e.event_time_local or "", e.region, e.title_en))


def normalize_event(row: dict[str, Any]) -> EconomicEvent | None:
    country = str(row.get("country") or "").strip().upper()
    region = COUNTRY_REGIONS.get(country)
    title_en = " ".join(str(row.get("event") or "").split())
    if region is None or not title_en:
        return None
    parsed = _parse_time(row.get("time"))
    if parsed is None:
        return None
    moment, has_time = parsed
    local = moment.astimezone(SEOUL)
    title_ko, floor = korean_title(title_en, region)
    unit = str(row.get("unit") or "").strip() or None
    return EconomicEvent(
        event_id=event_id(country, moment, title_en),
        event_date=local.date() if has_time else moment.date(),
        event_time_local=local.strftime("%H:%M") if has_time else None,
        region=region,
        title_ko=title_ko,
        title_en=title_en,
        importance=importance_from_impact(row.get("impact"), floor),
        actual=format_number(row.get("actual")),
        forecast=format_number(row.get("estimate")),
        previous=format_number(row.get("prev")),
        unit=unit,
    )


def event_id(country: str, moment: datetime, title_en: str) -> str:
    digest = hashlib.sha1(title_en.lower().encode("utf-8")).hexdigest()[:12]
    return f"{SOURCE}:{country.lower()}:{moment.astimezone(timezone.utc).strftime('%Y%m%dT%H%M')}:{digest}"


def format_number(value: Any) -> str | None:
    """3.1 → "3.1", 221000.0 → "221000", None/NaN → None. 문자열은 그대로."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value.strip() or None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if number == int(number) and abs(number) < 1e15:
        return str(int(number))
    return f"{number:.4f}".rstrip("0").rstrip(".")


def _parse_time(value: Any) -> tuple[datetime, bool] | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) == 10:
        try:
            day = date.fromisoformat(text)
        except ValueError:
            return None
        return datetime(day.year, day.month, day.day, tzinfo=timezone.utc), False
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment, True
