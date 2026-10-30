"""코스 진행 저장소(D30). 맞힌 문제만 learn_progress 에 한 줄씩 남긴다.

PostgreSQL 설정이 있으면 Postgres, 없으면 메모리. app.state.learn_progress_repository 로 주입한다.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Protocol

import psycopg

from app.auth.identity import database_conninfo


class LearnProgressRepositoryError(RuntimeError):
    """저장소를 쓸 수 없을 때."""


class LearnProgressRepository(Protocol):
    def list_solved(self, user_key: str, course_id: str) -> list[str]: ...

    def mark_solved(self, user_key: str, course_id: str, question_id: str, solved_at: datetime) -> None: ...


class InMemoryLearnProgressRepository:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], dict[str, datetime]] = {}
        self._lock = threading.Lock()

    def list_solved(self, user_key: str, course_id: str) -> list[str]:
        with self._lock:
            rows = self._rows.get((user_key, course_id), {})
            return [qid for qid, _ in sorted(rows.items(), key=lambda kv: (kv[1], kv[0]))]

    def mark_solved(self, user_key: str, course_id: str, question_id: str, solved_at: datetime) -> None:
        with self._lock:
            self._rows.setdefault((user_key, course_id), {}).setdefault(question_id, solved_at)


class PostgresLearnProgressRepository:
    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo

    @classmethod
    def from_env(cls) -> "PostgresLearnProgressRepository":
        conninfo = database_conninfo()
        if not conninfo:
            raise LearnProgressRepositoryError("DATABASE_URL or DATABASE_* settings are required")
        return cls(conninfo)

    def list_solved(self, user_key: str, course_id: str) -> list[str]:
        try:
            with psycopg.connect(self.conninfo) as conn:
                rows = conn.execute(
                    "SELECT question_id FROM learn_progress WHERE app_user_id = %s AND course_id = %s "
                    "ORDER BY solved_at, question_id",
                    (user_key, course_id),
                ).fetchall()
        except psycopg.Error as exc:
            raise LearnProgressRepositoryError("learn progress storage is unavailable") from exc
        return [str(row[0]) for row in rows]

    def mark_solved(self, user_key: str, course_id: str, question_id: str, solved_at: datetime) -> None:
        try:
            with psycopg.connect(self.conninfo) as conn:
                # 처음 맞힌 시각을 남긴다. 다시 맞혀도 바꾸지 않는다.
                conn.execute(
                    "INSERT INTO learn_progress (app_user_id, course_id, question_id, solved_at) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (app_user_id, course_id, question_id) DO NOTHING",
                    (user_key, course_id, question_id, solved_at),
                )
                conn.commit()
        except psycopg.Error as exc:
            raise LearnProgressRepositoryError("learn progress storage is unavailable") from exc


def learn_progress_repository_from_app(app: Any) -> LearnProgressRepository:
    existing = getattr(app.state, "learn_progress_repository", None)
    if existing is not None:
        return existing
    mode = getattr(app.state, "learn_progress_repository_mode", None)
    if mode is None:
        mode = "postgres" if database_conninfo() else "memory"
    repository: LearnProgressRepository
    if str(mode) == "memory":
        repository = InMemoryLearnProgressRepository()
    else:
        repository = PostgresLearnProgressRepository.from_env()
    app.state.learn_progress_repository = repository
    return repository
