"""경제지표 일정 저장소(economic_events, 마이그레이션 0030). 쓰기는 economic-calendar 잡이 한다."""

from __future__ import annotations

import threading
from datetime import date
from typing import Any, Protocol

from market_data.economic_calendar.store import select_events


class EconomicEventRepository(Protocol):
    def between(self, from_date: date, to_date: date, region: str | None = None) -> list[dict[str, Any]]: ...


class PostgresEconomicEventRepository:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    def between(self, from_date: date, to_date: date, region: str | None = None) -> list[dict[str, Any]]:
        return select_events(self._conninfo, from_date, to_date, region)


class InMemoryEconomicEventRepository:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        for row in rows or []:
            self.upsert(row)

    def upsert(self, row: dict[str, Any]) -> None:
        with self._lock:
            self._rows[str(row["event_id"])] = dict(row)

    def between(self, from_date: date, to_date: date, region: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._rows.values())
        return [
            dict(row)
            for row in rows
            if from_date <= _as_date(row.get("event_date")) <= to_date and (region is None or row.get("region") == region)
        ]


def _as_date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
