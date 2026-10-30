"""D28 캘린더 항목 만들기. API(GET /api/calendar)와 주간 요약 생성기가 같이 쓴다.

경제지표: economic_events 행 → {id, date, kind:"econ", region, title, level, at, done, actual, forecast, previous}
실적: ClickHouse yahoo_earnings_estimates 최신 행 → {id, date, kind:"earn", region:"us", title, symbol, nameKo, call, level, done}

날짜·시각은 한국 시간이다. level 은 "major" | "watch" | null.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

SEOUL = ZoneInfo("Asia/Seoul")
LEVELS = {"major", "watch"}
CALL_LABELS = {"pre": "장 시작 전", "after": "장 마감 후"}


def econ_item(row: dict[str, Any]) -> dict[str, Any]:
    event_date = row.get("event_date")
    day = event_date.isoformat() if isinstance(event_date, date) else str(event_date or "")[:10]
    unit = str(row.get("unit") or "").strip()
    actual = with_unit(row.get("actual"), unit)
    importance = str(row.get("importance") or "normal")
    return {
        "id": f"econ:{row.get('event_id')}",
        "date": day,
        "kind": "econ",
        "region": row.get("region"),
        "title": row.get("title_ko") or row.get("title_en") or "",
        "level": importance if importance in LEVELS else None,
        "at": row.get("event_time_local") or None,
        "done": actual is not None,
        "actual": actual,
        "forecast": with_unit(row.get("forecast"), unit),
        "previous": with_unit(row.get("previous"), unit),
    }


def earnings_item(
    row: dict[str, Any],
    *,
    name_ko: Callable[[str], str | None],
    level: str | None,
) -> dict[str, Any] | None:
    symbol = str(row.get("symbol") or "").strip().upper()
    moment = parse_utc(row.get("eventAt") or row.get("event_at"))
    if not symbol or moment is None:
        return None
    local = moment.astimezone(SEOUL)
    session = str(row.get("eventSession") or row.get("event_session") or "").strip().lower()
    status = str(row.get("eventStatus") or row.get("event_status") or "").strip().lower()
    actual = row.get("actualValue") if "actualValue" in row else row.get("actual_value")
    korean = name_ko(symbol)
    display = korean or symbol
    return {
        "id": f"earn:{symbol}:{local.date().isoformat()}",
        "date": local.date().isoformat(),
        "kind": "earn",
        "region": "us",
        "title": f"{display} 실적",
        "symbol": symbol,
        "nameKo": korean,
        "call": CALL_LABELS.get(session),
        "level": level if level in LEVELS else None,
        "done": status == "reported" or actual is not None,
    }


def earnings_level(symbol: str, *, personal: set[str], major: set[str]) -> str | None:
    """관심 종목·보유 종목이면 watch, 시총 상위면 major, 아니면 None. watch 가 먼저다."""
    if symbol in personal:
        return "watch"
    if symbol in major:
        return "major"
    return None


def build_earnings_items(
    rows: Iterable[dict[str, Any]],
    *,
    name_ko: Callable[[str], str | None],
    personal: set[str] | None = None,
    major: set[str] | None = None,
    universe: set[str] | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
) -> list[dict[str, Any]]:
    personal = personal or set()
    major = major or set()
    items: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if universe is not None and symbol not in universe:
            continue
        item = earnings_item(row, name_ko=name_ko, level=earnings_level(symbol, personal=personal, major=major))
        if item is None:
            continue
        if from_date is not None and item["date"] < from_date.isoformat():
            continue
        if to_date is not None and item["date"] > to_date.isoformat():
            continue
        items[item["id"]] = item
    return list(items.values())


def sort_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """날짜 → 시각(없으면 그날 맨 뒤) → 경제지표 먼저 → 제목."""
    return sorted(
        items,
        key=lambda item: (
            item.get("date") or "",
            item.get("at") is None,
            item.get("at") or "",
            0 if item.get("kind") == "econ" else 1,
            item.get("title") or "",
        ),
    )


def with_unit(value: Any, unit: str) -> str | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    if unit and not text.endswith(unit):
        return f"{text}{unit}"
    return text


def parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
