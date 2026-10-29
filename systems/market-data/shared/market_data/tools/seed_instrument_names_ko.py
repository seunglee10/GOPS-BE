"""instrument_names_ko.json 을 Postgres instrument_names_ko 테이블에 upsert 한다.

API 는 JSON 을 직접 읽으므로 이 적재는 서빙 필수 단계가 아니다. 운영 조회와
ERD 상의 테이블을 JSON 과 같은 내용으로 맞춰 두는 용도다. 마이그레이션
0024_instrument_names_ko.sql 이 먼저 적용돼 있어야 한다.

DATABASE_URL 또는 DATABASE_HOST/NAME/USER/PASSWORD 로 접속한다. 둘 다 없으면
적재할 행 수만 출력하고 끝낸다(로컬에서 JSON 검증용).
"""

from __future__ import annotations

import os
import sys

from market_data.reference import instrument_names_ko_path, load_instrument_names_ko


UPSERT_SQL = """
INSERT INTO instrument_names_ko (symbol, name_ko, exchange_ko, updated_at)
VALUES (%s, %s, %s, now())
ON CONFLICT (symbol) DO UPDATE SET
    name_ko = EXCLUDED.name_ko,
    exchange_ko = EXCLUDED.exchange_ko,
    updated_at = now()
WHERE instrument_names_ko.name_ko IS DISTINCT FROM EXCLUDED.name_ko
   OR instrument_names_ko.exchange_ko IS DISTINCT FROM EXCLUDED.exchange_ko
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


def seed_rows() -> list[tuple[str, str, str | None]]:
    names = load_instrument_names_ko()
    return [(symbol, entry["nameKo"], entry.get("exchangeKo")) for symbol, entry in sorted(names.items())]


def main(argv: list[str] | None = None) -> int:
    _ = argv
    rows = seed_rows()
    print(f"시드 파일: {instrument_names_ko_path()} rows={len(rows)}", flush=True)
    conninfo = postgres_conninfo()
    if not conninfo:
        print("DATABASE_URL 이 없어 Postgres 적재를 건너뜁니다.", flush=True)
        return 0

    import psycopg

    with psycopg.connect(conninfo) as conn:
        with conn.cursor() as cursor:
            cursor.executemany(UPSERT_SQL, rows)
        conn.commit()
        total = conn.execute("SELECT count(*) FROM instrument_names_ko").fetchone()[0]
    print(f"PostgreSQL instrument_names_ko 적재 완료: upserted={len(rows)} total={total}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
