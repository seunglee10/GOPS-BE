from __future__ import annotations

import os
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


NOTIFICATION_SETTING_KEYS = frozenset(
    {
        "master",
        "marketOpen",
        "marketClose",
        "extendedHoursMove",
        "targetPrice",
        "rapidMove",
        "volumeSpike",
        "watchlistNews",
        "earningsFiling",
        "executiveChange",
        "socialIssue",
        "regulationLegal",
        "supplyChainMacro",
    }
)

DEFAULT_NOTIFICATION_SETTINGS: dict[str, bool] = {
    "master": True,
    "marketOpen": True,
    "marketClose": False,
    "extendedHoursMove": False,
    "targetPrice": True,
    "rapidMove": True,
    "volumeSpike": False,
    "watchlistNews": True,
    "earningsFiling": True,
    "executiveChange": False,
    "socialIssue": True,
    "regulationLegal": True,
    "supplyChainMacro": False,
}

MAX_COMPANY_OVERRIDES = 50


class NotificationPreferenceRepository:
    def get(self, user_sub: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def patch(
        self,
        user_sub: str,
        *,
        settings: dict[str, bool],
        company_overrides: dict[str, bool],
    ) -> dict[str, Any]:
        raise NotImplementedError


class PostgresNotificationPreferenceRepository(NotificationPreferenceRepository):
    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo

    @classmethod
    def from_env(cls) -> "PostgresNotificationPreferenceRepository":
        conninfo = os.getenv("DATABASE_URL")
        if not conninfo:
            conninfo = make_conninfo(
                host=os.environ["DATABASE_HOST"],
                port=os.getenv("DATABASE_PORT", "5432"),
                dbname=os.environ["DATABASE_NAME"],
                user=os.environ["DATABASE_USER"],
                password=os.environ["DATABASE_PASSWORD"],
            )
        return cls(conninfo)

    def get(self, user_sub: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM user_notification_preferences WHERE user_sub = %s",
                (user_sub,),
            ).fetchone()
            return _json_ready(dict(row)) if row else None

    def patch(
        self,
        user_sub: str,
        *,
        settings: dict[str, bool],
        company_overrides: dict[str, bool],
    ) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO user_notification_preferences (user_sub, settings, company_overrides)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_sub) DO UPDATE
                SET settings = user_notification_preferences.settings || EXCLUDED.settings,
                    company_overrides = user_notification_preferences.company_overrides || EXCLUDED.company_overrides,
                    updated_at = now()
                RETURNING *
                """,
                (user_sub, Jsonb(settings), Jsonb(company_overrides)),
            ).fetchone()
            conn.commit()
            return _json_ready(dict(row))

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.conninfo, row_factory=dict_row)


class InMemoryNotificationPreferenceRepository(NotificationPreferenceRepository):
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def get(self, user_sub: str) -> dict[str, Any] | None:
        row = self.rows.get(user_sub)
        return deepcopy(row) if row else None

    def patch(
        self,
        user_sub: str,
        *,
        settings: dict[str, bool],
        company_overrides: dict[str, bool],
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        current = self.rows.get(user_sub, {
            "user_sub": user_sub,
            "settings": {},
            "company_overrides": {},
            "updated_at": now,
        })
        row = {
            **current,
            "settings": {**current["settings"], **settings},
            "company_overrides": {**current["company_overrides"], **company_overrides},
            "updated_at": now,
        }
        self.rows[user_sub] = deepcopy(row)
        return deepcopy(row)


def preference_response(row: dict[str, Any] | None) -> dict[str, Any]:
    stored_settings = row.get("settings") if isinstance(row, dict) and isinstance(row.get("settings"), dict) else {}
    stored_overrides = (
        row.get("company_overrides")
        if isinstance(row, dict) and isinstance(row.get("company_overrides"), dict)
        else {}
    )
    return {
        "settings": {
            **DEFAULT_NOTIFICATION_SETTINGS,
            **{
                key: bool(value)
                for key, value in stored_settings.items()
                if key in NOTIFICATION_SETTING_KEYS and isinstance(value, bool)
            },
        },
        "companyOverrides": {
            str(symbol).upper(): bool(enabled)
            for symbol, enabled in stored_overrides.items()
            if isinstance(enabled, bool)
        },
        "persisted": row is not None,
        "updatedAt": row.get("updated_at") if isinstance(row, dict) else None,
    }


def _json_ready(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    return value
