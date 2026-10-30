"""economic-calendar 잡 본체. Finnhub 경제 캘린더(미국·한국)를 받아 economic_events 에 upsert 한다.

  FINNHUB_API_KEY                 없으면 로그만 남기고 0 으로 끝낸다.
  ECONOMIC_CALENDAR_PAST_DAYS     오늘(한국 시간)부터 며칠 전까지 다시 받을지. 발표값(actual)을 채우려고 과거도 받는다. 기본 7
  ECONOMIC_CALENDAR_FUTURE_DAYS   며칠 뒤까지 받을지. 기본 35
  DATABASE_URL 또는 DATABASE_*    없으면 받은 행 수만 출력하고 끝낸다.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .finnhub import SEOUL, FinnhubError, fetch_economic_calendar, normalize_events
from .store import postgres_conninfo, upsert_events

logger = logging.getLogger("economic-calendar")


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, ""))
    except ValueError:
        return default
    return value if value >= 0 else default


def run(
    *,
    api_key: str | None,
    conninfo: str | None,
    now: datetime,
    past_days: int = 7,
    future_days: int = 35,
    urlopen: Callable[..., Any] | None = None,
    upsert: Callable[[str, list[dict[str, Any]]], int] = upsert_events,
) -> dict[str, Any]:
    if not api_key:
        logger.warning("FINNHUB_API_KEY 가 없어 경제 캘린더 수집을 건너뜁니다.")
        return {"status": "skipped", "reason": "finnhub_api_key_missing"}
    today = now.astimezone(SEOUL).date()
    from_date, to_date = today - timedelta(days=past_days), today + timedelta(days=future_days)
    rows = fetch_economic_calendar(api_key, from_date, to_date, urlopen=urlopen)
    events = normalize_events(rows)
    result: dict[str, Any] = {
        "status": "fetched",
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "received": len(rows),
        "events": len(events),
        "untranslated": sum(1 for event in events if event.title_ko is None),
    }
    if not conninfo:
        logger.warning("DATABASE_URL 이 없어 Postgres 적재를 건너뜁니다.")
        result["status"] = "dry_run"
        return result
    result["upserted"] = upsert(conninfo, [event.as_row() for event in events])
    result["status"] = "written"
    return result


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    parser = argparse.ArgumentParser(description="Finnhub 경제 캘린더(미국·한국)를 economic_events 에 적재합니다.")
    parser.add_argument("--past-days", type=int, default=_int_env("ECONOMIC_CALENDAR_PAST_DAYS", 7))
    parser.add_argument("--future-days", type=int, default=_int_env("ECONOMIC_CALENDAR_FUTURE_DAYS", 35))
    args = parser.parse_args(argv)
    try:
        result = run(
            api_key=(os.getenv("FINNHUB_API_KEY") or "").strip() or None,
            conninfo=postgres_conninfo(),
            now=datetime.now(timezone.utc),
            past_days=max(0, args.past_days),
            future_days=max(0, args.future_days),
        )
    except FinnhubError as exc:
        logger.error("경제 캘린더 수집 실패: %s", exc)
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
