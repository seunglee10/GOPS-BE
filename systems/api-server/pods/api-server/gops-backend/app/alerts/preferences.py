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
        "targetPrice",
        "rapidMove",
        "volumeSpike",
        "marketOpen",
        "marketClose",
        "extendedHoursMove",
        "earningsD1",
        "socialIssue",
        "aiAnomaly",
    }
)

DEFAULT_NOTIFICATION_SETTINGS: dict[str, bool] = {
    "master": True,
    "targetPrice": True,
    "rapidMove": True,
    "volumeSpike": False,
    "marketOpen": True,
    "marketClose": False,
    "extendedHoursMove": False,
    "earningsD1": True,
    "socialIssue": True,
    "aiAnomaly": True,
}

NOTIFICATION_THRESHOLD_ALLOWED_VALUES: dict[str, frozenset[int]] = {
    "rapidMovePct": frozenset({3, 5, 10}),
    "volumeSpikeMultiple": frozenset({2, 3, 5}),
}

DEFAULT_NOTIFICATION_THRESHOLDS: dict[str, int] = {
    "rapidMovePct": 5,
    "volumeSpikeMultiple": 3,
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
        thresholds: dict[str, int],
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
        thresholds: dict[str, int],
        company_overrides: dict[str, bool],
    ) -> dict[str, Any]:
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM user_notification_preferences WHERE user_sub = %s FOR UPDATE",
                (user_sub,),
            ).fetchone()
            current = preference_response(_json_ready(dict(existing)) if existing else None)
            stored_settings = {
                "settings": {**current["settings"], **settings},
                "thresholds": {**current["thresholds"], **thresholds},
            }
            stored_overrides = {**current["companyOverrides"], **company_overrides}
            row = conn.execute(
                """
                INSERT INTO user_notification_preferences (user_sub, settings, company_overrides)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_sub) DO UPDATE
                SET settings = EXCLUDED.settings,
                    company_overrides = EXCLUDED.company_overrides,
                    updated_at = now()
                RETURNING *
                """,
                (user_sub, Jsonb(stored_settings), Jsonb(stored_overrides)),
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
        thresholds: dict[str, int],
        company_overrides: dict[str, bool],
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        current = self.rows.get(user_sub, {
            "user_sub": user_sub,
            "settings": {},
            "company_overrides": {},
            "updated_at": now,
        })
        normalized = preference_response(current if user_sub in self.rows else None)
        row = {
            **current,
            "settings": {
                "settings": {**normalized["settings"], **settings},
                "thresholds": {**normalized["thresholds"], **thresholds},
            },
            "company_overrides": {**normalized["companyOverrides"], **company_overrides},
            "updated_at": now,
        }
        self.rows[user_sub] = deepcopy(row)
        return deepcopy(row)


def preference_response(row: dict[str, Any] | None) -> dict[str, Any]:
    stored_container = row.get("settings") if isinstance(row, dict) and isinstance(row.get("settings"), dict) else {}
    stored_settings = (
        stored_container.get("settings")
        if isinstance(stored_container.get("settings"), dict)
        else stored_container
    )
    stored_thresholds = (
        stored_container.get("thresholds")
        if isinstance(stored_container.get("thresholds"), dict)
        else {}
    )
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
        "thresholds": {
            **DEFAULT_NOTIFICATION_THRESHOLDS,
            **{
                key: int(value)
                for key, value in stored_thresholds.items()
                if (
                    key in NOTIFICATION_THRESHOLD_ALLOWED_VALUES
                    and not isinstance(value, bool)
                    and isinstance(value, (int, float))
                    and int(value) == value
                    and int(value) in NOTIFICATION_THRESHOLD_ALLOWED_VALUES[key]
                )
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
