from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


ACTIVE_ALERT_LIMIT = 50


@dataclass(frozen=True)
class AlertCreate:
    user_sub: str
    symbol: str
    type: str
    direction: str | None = None
    target_price: Decimal | None = None
    change_pct: Decimal | None = None
    window_min: int | None = None
    repeat: bool = False
    repeat_limit: int | None = 1
    status: str = "active"
    notifications_enabled: bool = True
    proposal_source: str | None = None
    expires_at: datetime | None = None


class AlertRepository:
    def active_alert_count(self, user_sub: str) -> int:
        raise NotImplementedError

    def create_alert(self, alert: AlertCreate) -> dict[str, Any]:
        raise NotImplementedError

    def list_alerts(self, user_sub: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_alert(self, user_sub: str, alert_id: int) -> dict[str, Any] | None:
        raise NotImplementedError

    def update_alert_status(self, user_sub: str, alert_id: int, status: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def update_notifications_enabled(self, user_sub: str, alert_id: int, enabled: bool) -> dict[str, Any] | None:
        raise NotImplementedError

    def record_alert_trigger(self, user_sub: str, alert_id: int) -> dict[str, Any] | None:
        raise NotImplementedError

    def delete_alert(self, user_sub: str, alert_id: int) -> dict[str, Any] | None:
        raise NotImplementedError

    def delete_alerts(self, user_sub: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def active_alerts(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def create_notification(
        self,
        *,
        user_sub: str,
        alert_id: int | None,
        event_id: str,
        notification_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError

    def list_notifications(self, user_sub: str, *, after: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        raise NotImplementedError

    def unread_count(self, user_sub: str) -> int:
        raise NotImplementedError

    def mark_notification_read(self, user_sub: str, notification_id: int) -> dict[str, Any] | None:
        raise NotImplementedError

    def mark_all_notifications_read(self, user_sub: str) -> int:
        raise NotImplementedError

    def delete_notification(self, user_sub: str, notification_id: int) -> dict[str, Any] | None:
        raise NotImplementedError


class PostgresAlertRepository(AlertRepository):
    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo

    @classmethod
    def from_env(cls) -> "PostgresAlertRepository":
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

    def active_alert_count(self, user_sub: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM alerts WHERE user_sub = %s AND status = 'active'",
                (user_sub,),
            ).fetchone()
            return int(row["count"])

    def create_alert(self, alert: AlertCreate) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO alerts (
                    user_sub, symbol, type, direction, target_price, change_pct,
                    window_min, repeat, repeat_limit, status, notifications_enabled,
                    proposal_source, expires_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    alert.user_sub,
                    alert.symbol,
                    alert.type,
                    alert.direction,
                    alert.target_price,
                    alert.change_pct,
                    alert.window_min,
                    alert.repeat,
                    alert.repeat_limit,
                    alert.status,
                    alert.notifications_enabled,
                    alert.proposal_source,
                    alert.expires_at,
                ),
            ).fetchone()
            conn.commit()
            return _json_ready(dict(row))

    def list_alerts(self, user_sub: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE user_sub = %s ORDER BY created_at DESC, id DESC",
                (user_sub,),
            ).fetchall()
            return [_json_ready(dict(row)) for row in rows]

    def get_alert(self, user_sub: str, alert_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM alerts WHERE user_sub = %s AND id = %s",
                (user_sub, alert_id),
            ).fetchone()
            return _json_ready(dict(row)) if row else None

    def update_alert_status(self, user_sub: str, alert_id: int, status: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE alerts
                SET status = %s
                WHERE user_sub = %s AND id = %s
                RETURNING *
                """,
                (status, user_sub, alert_id),
            ).fetchone()
            conn.commit()
            return _json_ready(dict(row)) if row else None

    def update_notifications_enabled(self, user_sub: str, alert_id: int, enabled: bool) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "UPDATE alerts SET notifications_enabled = %s WHERE user_sub = %s AND id = %s RETURNING *",
                (enabled, user_sub, alert_id),
            ).fetchone()
            conn.commit()
            return _json_ready(dict(row)) if row else None

    def record_alert_trigger(self, user_sub: str, alert_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE alerts
                SET triggered_count = triggered_count + 1,
                    status = CASE
                        WHEN repeat_limit IS NOT NULL AND triggered_count + 1 >= repeat_limit THEN 'fired'
                        ELSE status
                    END
                WHERE user_sub = %s
                  AND id = %s
                  AND status = 'active'
                RETURNING *
                """,
                (user_sub, alert_id),
            ).fetchone()
            conn.commit()
            return _json_ready(dict(row)) if row else None

    def delete_alert(self, user_sub: str, alert_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "DELETE FROM alerts WHERE user_sub = %s AND id = %s RETURNING *",
                (user_sub, alert_id),
            ).fetchone()
            conn.commit()
            return _json_ready(dict(row)) if row else None

    def delete_alerts(self, user_sub: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "DELETE FROM alerts WHERE user_sub = %s RETURNING *",
                (user_sub,),
            ).fetchall()
            conn.commit()
            return [_json_ready(dict(row)) for row in rows]

    def active_alerts(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM alerts
                WHERE status = 'active'
                  AND (expires_at IS NULL OR expires_at > now())
                ORDER BY symbol, id
                """
            ).fetchall()
            return [_json_ready(dict(row)) for row in rows]

    def create_notification(
        self,
        *,
        user_sub: str,
        alert_id: int | None,
        event_id: str,
        notification_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO notifications (user_sub, alert_id, event_id, type, payload)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (event_id) DO UPDATE
                SET event_id = EXCLUDED.event_id
                RETURNING *
                """,
                (user_sub, alert_id, event_id, notification_type, Jsonb(_json_ready(payload))),
            ).fetchone()
            conn.commit()
            return _json_ready(dict(row))

    def list_notifications(self, user_sub: str, *, after: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        params: list[Any] = [user_sub]
        if after is not None:
            query = """
                SELECT * FROM notifications
                WHERE user_sub = %s AND id > %s
                ORDER BY id ASC
                LIMIT %s
            """
            params.extend([after, limit])
        else:
            query = """
                SELECT * FROM notifications
                WHERE user_sub = %s
                ORDER BY id DESC
                LIMIT %s
            """
            params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [_json_ready(dict(row)) for row in rows]

    def unread_count(self, user_sub: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM notifications WHERE user_sub = %s AND read_at IS NULL",
                (user_sub,),
            ).fetchone()
            return int(row["count"])

    def mark_notification_read(self, user_sub: str, notification_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE notifications
                SET read_at = COALESCE(read_at, now())
                WHERE user_sub = %s AND id = %s
                RETURNING *
                """,
                (user_sub, notification_id),
            ).fetchone()
            conn.commit()
            return _json_ready(dict(row)) if row else None

    def mark_all_notifications_read(self, user_sub: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                WITH updated AS (
                    UPDATE notifications
                    SET read_at = COALESCE(read_at, now())
                    WHERE user_sub = %s AND read_at IS NULL
                    RETURNING id
                )
                SELECT COUNT(*) AS count FROM updated
                """,
                (user_sub,),
            ).fetchone()
            conn.commit()
            return int(row["count"])

    def delete_notification(self, user_sub: str, notification_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                DELETE FROM notifications
                WHERE user_sub = %s
                  AND id = %s
                  AND read_at IS NOT NULL
                RETURNING *
                """,
                (user_sub, notification_id),
            ).fetchone()
            conn.commit()
            return _json_ready(dict(row)) if row else None

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.conninfo, row_factory=dict_row)


class InMemoryAlertRepository(AlertRepository):
    def __init__(self) -> None:
        self.alerts: dict[int, dict[str, Any]] = {}
        self.notifications: dict[int, dict[str, Any]] = {}
        self._alert_id = 0
        self._notification_id = 0

    def active_alert_count(self, user_sub: str) -> int:
        return sum(1 for alert in self.alerts.values() if alert["user_sub"] == user_sub and alert["status"] == "active")

    def create_alert(self, alert: AlertCreate) -> dict[str, Any]:
        self._alert_id += 1
        row = {
            "id": self._alert_id,
            "user_sub": alert.user_sub,
            "symbol": alert.symbol,
            "type": alert.type,
            "direction": alert.direction,
            "target_price": alert.target_price,
            "change_pct": alert.change_pct,
            "window_min": alert.window_min,
            "repeat": alert.repeat,
            "repeat_limit": alert.repeat_limit,
            "triggered_count": 0,
            "status": alert.status,
            "notifications_enabled": alert.notifications_enabled,
            "proposal_source": alert.proposal_source,
            "created_at": datetime.now(timezone.utc),
            "expires_at": alert.expires_at,
        }
        self.alerts[self._alert_id] = deepcopy(row)
        return _json_ready(row)

    def list_alerts(self, user_sub: str) -> list[dict[str, Any]]:
        rows = [alert for alert in self.alerts.values() if alert["user_sub"] == user_sub]
        rows.sort(key=lambda item: (item["created_at"], item["id"]), reverse=True)
        return [_json_ready(row) for row in rows]

    def get_alert(self, user_sub: str, alert_id: int) -> dict[str, Any] | None:
        row = self.alerts.get(alert_id)
        return _json_ready(row) if row and row["user_sub"] == user_sub else None

    def update_alert_status(self, user_sub: str, alert_id: int, status: str) -> dict[str, Any] | None:
        row = self.alerts.get(alert_id)
        if not row or row["user_sub"] != user_sub:
            return None
        row["status"] = status
        return _json_ready(row)

    def update_notifications_enabled(self, user_sub: str, alert_id: int, enabled: bool) -> dict[str, Any] | None:
        row = self.alerts.get(alert_id)
        if not row or row["user_sub"] != user_sub:
            return None
        row["notifications_enabled"] = bool(enabled)
        return _json_ready(row)

    def record_alert_trigger(self, user_sub: str, alert_id: int) -> dict[str, Any] | None:
        row = self.alerts.get(alert_id)
        if not row or row["user_sub"] != user_sub or row["status"] != "active":
            return None
        row["triggered_count"] = int(row.get("triggered_count") or 0) + 1
        repeat_limit = row.get("repeat_limit")
        if repeat_limit is not None and row["triggered_count"] >= int(repeat_limit):
            row["status"] = "fired"
        return _json_ready(row)

    def delete_alert(self, user_sub: str, alert_id: int) -> dict[str, Any] | None:
        row = self.alerts.get(alert_id)
        if not row or row["user_sub"] != user_sub:
            return None
        return _json_ready(self.alerts.pop(alert_id))

    def delete_alerts(self, user_sub: str) -> list[dict[str, Any]]:
        alert_ids = sorted(alert_id for alert_id, row in self.alerts.items() if row["user_sub"] == user_sub)
        return [_json_ready(self.alerts.pop(alert_id)) for alert_id in alert_ids]

    def active_alerts(self) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        rows = [
            row
            for row in self.alerts.values()
            if row["status"] == "active" and (row.get("expires_at") is None or row["expires_at"] > now)
        ]
        rows.sort(key=lambda item: (item["symbol"], item["id"]))
        return [_json_ready(row) for row in rows]

    def create_notification(
        self,
        *,
        user_sub: str,
        alert_id: int | None,
        event_id: str,
        notification_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        for row in self.notifications.values():
            if row["event_id"] == event_id:
                return _json_ready(row)
        self._notification_id += 1
        row = {
            "id": self._notification_id,
            "user_sub": user_sub,
            "alert_id": alert_id,
            "event_id": event_id,
            "type": notification_type,
            "payload": deepcopy(payload),
            "created_at": datetime.now(timezone.utc),
            "read_at": None,
        }
        self.notifications[self._notification_id] = row
        return _json_ready(row)

    def list_notifications(self, user_sub: str, *, after: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        rows = [row for row in self.notifications.values() if row["user_sub"] == user_sub]
        if after is not None:
            rows = [row for row in rows if row["id"] > after]
            rows.sort(key=lambda item: item["id"])
        else:
            rows.sort(key=lambda item: item["id"], reverse=True)
        return [_json_ready(row) for row in rows[:limit]]

    def unread_count(self, user_sub: str) -> int:
        return sum(1 for row in self.notifications.values() if row["user_sub"] == user_sub and row["read_at"] is None)

    def mark_notification_read(self, user_sub: str, notification_id: int) -> dict[str, Any] | None:
        row = self.notifications.get(notification_id)
        if not row or row["user_sub"] != user_sub:
            return None
        row["read_at"] = row["read_at"] or datetime.now(timezone.utc)
        return _json_ready(row)

    def mark_all_notifications_read(self, user_sub: str) -> int:
        count = 0
        now = datetime.now(timezone.utc)
        for row in self.notifications.values():
            if row["user_sub"] == user_sub and row["read_at"] is None:
                row["read_at"] = now
                count += 1
        return count

    def delete_notification(self, user_sub: str, notification_id: int) -> dict[str, Any] | None:
        row = self.notifications.get(notification_id)
        if not row or row["user_sub"] != user_sub or row["read_at"] is None:
            return None
        return _json_ready(self.notifications.pop(notification_id))


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return value
