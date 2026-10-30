"""사용자 화면 설정 저장소(D34). 사용자마다 JSONB 한 행.

data 는 프론트가 쓰는 화면 설정(layout, chart, draw, cur, sideW, dwW, brand, experience)을
그대로 담는다. 서버는 구조를 보지 않고 크기와 experience 값만 검사한다(routes.py).
version 은 낙관적 동시성용이다. 행이 없으면 0, 저장할 때마다 1씩 오른다.

PostgreSQL 설정이 있으면 Postgres, 없으면 메모리. app.state.user_preferences_repository 로 주입한다.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.auth.identity import database_conninfo

EXPERIENCE_VALUES = frozenset({"new", "some", "pro"})


class PreferencesRepositoryError(RuntimeError):
    """저장소를 쓸 수 없을 때."""


@dataclass(frozen=True)
class UserPreferences:
    version: int = 0
    data: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime | None = None


class VersionConflict(Exception):
    def __init__(self, current: UserPreferences) -> None:
        super().__init__("version_conflict")
        self.current = current


def experience_of(prefs: UserPreferences | None) -> str | None:
    value = (prefs.data if prefs else {}).get("experience")
    return value if isinstance(value, str) and value in EXPERIENCE_VALUES else None


class PreferencesRepository(Protocol):
    def get(self, user_key: str) -> UserPreferences: ...

    def save(self, user_key: str, expected_version: int, data: dict[str, Any], updated_at: datetime) -> UserPreferences:
        """expected_version 이 지금 값과 같을 때만 저장하고 version+1 을 돌려준다. 다르면 VersionConflict."""
        ...


class InMemoryPreferencesRepository:
    def __init__(self) -> None:
        self._rows: dict[str, UserPreferences] = {}
        self._lock = threading.Lock()

    def get(self, user_key: str) -> UserPreferences:
        with self._lock:
            return self._rows.get(user_key) or UserPreferences()

    def save(self, user_key: str, expected_version: int, data: dict[str, Any], updated_at: datetime) -> UserPreferences:
        with self._lock:
            current = self._rows.get(user_key) or UserPreferences()
            if current.version != expected_version:
                raise VersionConflict(current)
            saved = UserPreferences(version=current.version + 1, data=data, updated_at=updated_at)
            self._rows[user_key] = saved
            return saved


def _row(row: dict[str, Any] | None) -> UserPreferences | None:
    if not row:
        return None
    return UserPreferences(version=int(row["version"]), data=dict(row["data"] or {}), updated_at=row["updated_at"])


class PostgresPreferencesRepository:
    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo

    @classmethod
    def from_env(cls) -> "PostgresPreferencesRepository":
        conninfo = database_conninfo()
        if not conninfo:
            raise PreferencesRepositoryError("DATABASE_URL or DATABASE_* settings are required")
        return cls(conninfo)

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.conninfo, row_factory=dict_row)

    def get(self, user_key: str) -> UserPreferences:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT version, data, updated_at FROM user_preferences WHERE app_user_id = %s",
                    (user_key,),
                ).fetchone()
        except psycopg.Error as exc:
            raise PreferencesRepositoryError("user preferences storage is unavailable") from exc
        return _row(row) or UserPreferences()

    def save(self, user_key: str, expected_version: int, data: dict[str, Any], updated_at: datetime) -> UserPreferences:
        try:
            with self._connect() as conn:
                if expected_version == 0:
                    row = conn.execute(
                        "INSERT INTO user_preferences (app_user_id, version, data, updated_at) "
                        "VALUES (%s, 1, %s, %s) ON CONFLICT (app_user_id) DO NOTHING "
                        "RETURNING version, data, updated_at",
                        (user_key, Jsonb(data), updated_at),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "UPDATE user_preferences SET version = version + 1, data = %s, updated_at = %s "
                        "WHERE app_user_id = %s AND version = %s RETURNING version, data, updated_at",
                        (Jsonb(data), updated_at, user_key, expected_version),
                    ).fetchone()
                if row is None:
                    conn.rollback()
                    current = conn.execute(
                        "SELECT version, data, updated_at FROM user_preferences WHERE app_user_id = %s",
                        (user_key,),
                    ).fetchone()
                    raise VersionConflict(_row(current) or UserPreferences())
                conn.commit()
        except psycopg.Error as exc:
            raise PreferencesRepositoryError("user preferences storage is unavailable") from exc
        return _row(row) or UserPreferences()


def preferences_repository_from_app(app: Any) -> PreferencesRepository:
    existing = getattr(app.state, "user_preferences_repository", None)
    if existing is not None:
        return existing
    mode = getattr(app.state, "user_preferences_repository_mode", None)
    if mode is None:
        mode = "postgres" if database_conninfo() else "memory"
    repository: PreferencesRepository
    if str(mode) == "memory":
        repository = InMemoryPreferencesRepository()
    else:
        repository = PostgresPreferencesRepository.from_env()
    app.state.user_preferences_repository = repository
    return repository
