"""economic_events 테이블 읽기·쓰기(마이그레이션 0030_economic_events.sql)."""

from __future__ import annotations

import os
from datetime import date
from typing import Any, Iterable

UPSERT_SQL = """
INSERT INTO economic_events (
    event_id, event_date, event_time_local, region, title_ko, title_en, importance,
    actual, forecast, previous, unit, source, updated_at
) VALUES (
    %(event_id)s, %(event_date)s, %(event_time_local)s, %(region)s, %(title_ko)s, %(title_en)s, %(importance)s,
    %(actual)s, %(forecast)s, %(previous)s, %(unit)s, %(source)s, now()
)
ON CONFLICT (event_id) DO UPDATE SET
    event_date = EXCLUDED.event_date,
    event_time_local = EXCLUDED.event_time_local,
    region = EXCLUDED.region,
    title_ko = EXCLUDED.title_ko,
    title_en = EXCLUDED.title_en,
    importance = EXCLUDED.importance,
    actual = COALESCE(EXCLUDED.actual, economic_events.actual),
    forecast = COALESCE(EXCLUDED.forecast, economic_events.forecast),
    previous = COALESCE(EXCLUDED.previous, economic_events.previous),
    unit = EXCLUDED.unit,
    source = EXCLUDED.source,
    updated_at = now()
"""

SELECT_SQL = """
SELECT event_id, event_date, event_time_local, region, title_ko, title_en, importance,
       actual, forecast, previous, unit, source, updated_at
FROM economic_events
WHERE event_date >= %(from_date)s AND event_date <= %(to_date)s
  AND (%(region)s::text IS NULL OR region = %(region)s::text)
ORDER BY event_date, event_time_local NULLS LAST, event_id
"""


def postgres_conninfo() -> str | None:
    conninfo = os.getenv("DATABASE_URL")
    if conninfo:
        return conninfo
    required = ("DATABASE_HOST", "DATABASE_NAME", "DATABASE_USER", "DATABASE_PASSWORD")
    if not all(os.getenv(name) for name in required):
        return None
    from psycopg.conninfo import make_conninfo

    return make_conninfo(
        host=os.environ["DATABASE_HOST"], port=os.getenv("DATABASE_PORT", "5432"),
        dbname=os.environ["DATABASE_NAME"], user=os.environ["DATABASE_USER"],
        password=os.environ["DATABASE_PASSWORD"],
    )


def upsert_events(conninfo: str, rows: Iterable[dict[str, Any]]) -> int:
    import psycopg

    payload = list(rows)
    if not payload:
        return 0
    with psycopg.connect(conninfo) as conn:
        with conn.cursor() as cursor:
            cursor.executemany(UPSERT_SQL, payload)
        conn.commit()
    return len(payload)


def select_events(conninfo: str, from_date: date, to_date: date, region: str | None = None) -> list[dict[str, Any]]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(conninfo, row_factory=dict_row) as conn:
        rows = conn.execute(SELECT_SQL, {"from_date": from_date, "to_date": to_date, "region": region}).fetchall()
    return [dict(row) for row in rows]
