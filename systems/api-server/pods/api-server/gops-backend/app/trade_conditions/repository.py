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

from app.alerts.repository import AlertCreate, AlertRepository


@dataclass(frozen=True)
class TradeConditionCreate:
    user_sub: str
    source: str
    symbol: str
    side: str
    direction: str
    trigger_price: Decimal
    limit_price: Decimal
    quantity: int
    exchange: str = "NASD"
    execution_enabled: bool = True
    alerts_enabled: bool = True
    validity: str = "DAY"
    market_hours: str = "REGULAR"
    proposal_id: str | None = None
    analysis_id: str | None = None
    expires_at: datetime | None = None


class DuplicateProposalError(RuntimeError):
    pass


class TradeConditionRepository:
    def create_condition(self, condition: TradeConditionCreate) -> dict[str, Any]:
        raise NotImplementedError

    def list_conditions(self, user_sub: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_condition(self, user_sub: str, condition_id: int) -> dict[str, Any] | None:
        raise NotImplementedError

    def update_condition(
        self,
        user_sub: str,
        condition_id: int,
        *,
        status: str | None = None,
        alerts_enabled: bool | None = None,
    ) -> dict[str, Any] | None:
        raise NotImplementedError

    def delete_condition(self, user_sub: str, condition_id: int) -> dict[str, Any] | None:
        raise NotImplementedError

    def claim_trigger(self, alert_id: int, event_id: str, triggered_at: str | None = None) -> dict[str, Any] | None:
        raise NotImplementedError

    def finish_execution(self, condition_id: int, *, status: str, order_id: str | None = None, error_reason: str | None = None) -> dict[str, Any] | None:
        raise NotImplementedError


class InMemoryTradeConditionRepository(TradeConditionRepository):
    def __init__(self, alert_repository: AlertRepository) -> None:
        self.alert_repository = alert_repository
        self.conditions: dict[int, dict[str, Any]] = {}
        self._condition_id = 0

    def create_condition(self, condition: TradeConditionCreate) -> dict[str, Any]:
        if condition.proposal_id:
            existing = next((
                row for row in self.conditions.values()
                if row["user_sub"] == condition.user_sub and row.get("proposal_id") == condition.proposal_id
            ), None)
            if existing:
                raise DuplicateProposalError(condition.proposal_id)
        alert = self.alert_repository.create_alert(_alert_create(condition))
        self._condition_id += 1
        now = datetime.now(timezone.utc)
        row = {
            "id": self._condition_id,
            "user_sub": condition.user_sub,
            "source": condition.source,
            "proposal_id": condition.proposal_id,
            "analysis_id": condition.analysis_id,
            "alert_id": int(alert["id"]),
            "side": condition.side,
            "limit_price": condition.limit_price,
            "quantity": condition.quantity,
            "exchange": condition.exchange,
            "execution_enabled": condition.execution_enabled,
            "status": "watching",
            "validity": condition.validity,
            "market_hours": condition.market_hours,
            "trigger_event_id": None,
            "triggered_at": None,
            "order_id": None,
            "error_reason": None,
            "version": 1,
            "created_at": now,
            "updated_at": now,
            "expires_at": condition.expires_at,
        }
        self.conditions[self._condition_id] = row
        return _combined(row, alert)

    def list_conditions(self, user_sub: str) -> list[dict[str, Any]]:
        self._expire_due(user_sub)
        rows = [self._combined_row(row) for row in self.conditions.values() if row["user_sub"] == user_sub]
        rows.sort(key=lambda item: (str(item.get("created_at")), int(item["id"])), reverse=True)
        return rows

    def get_condition(self, user_sub: str, condition_id: int) -> dict[str, Any] | None:
        self._expire_due(user_sub, condition_id)
        row = self.conditions.get(condition_id)
        return self._combined_row(row) if row and row["user_sub"] == user_sub else None

    def update_condition(self, user_sub: str, condition_id: int, *, status: str | None = None, alerts_enabled: bool | None = None) -> dict[str, Any] | None:
        row = self.conditions.get(condition_id)
        if not row or row["user_sub"] != user_sub:
            return None
        if status is not None and row["status"] not in {"watching", "paused"}:
            return None
        alert = self.alert_repository.get_alert(user_sub, int(row["alert_id"]))
        if not alert:
            return None
        if status is not None:
            row["status"] = status
            alert = self.alert_repository.update_alert_status(user_sub, int(row["alert_id"]), "active" if status == "watching" else "disabled") or alert
        if alerts_enabled is not None:
            alert = self.alert_repository.update_notifications_enabled(user_sub, int(row["alert_id"]), alerts_enabled) or alert
        row["version"] = int(row["version"]) + 1
        row["updated_at"] = datetime.now(timezone.utc)
        return _combined(row, alert)

    def delete_condition(self, user_sub: str, condition_id: int) -> dict[str, Any] | None:
        row = self.conditions.get(condition_id)
        if not row or row["user_sub"] != user_sub:
            return None
        alert = self.alert_repository.delete_alert(user_sub, int(row["alert_id"]))
        self.conditions.pop(condition_id, None)
        return _combined(row, alert or {})

    def claim_trigger(self, alert_id: int, event_id: str, triggered_at: str | None = None) -> dict[str, Any] | None:
        row = next((item for item in self.conditions.values() if int(item["alert_id"]) == int(alert_id)), None)
        expires_at = row.get("expires_at") if row else None
        if isinstance(expires_at, datetime) and expires_at <= datetime.now(timezone.utc):
            row["status"] = "expired"
            row["updated_at"] = datetime.now(timezone.utc)
            self.alert_repository.update_alert_status(str(row["user_sub"]), int(row["alert_id"]), "disabled")
            return None
        first_claim = bool(row and row["status"] == "watching" and not row.get("trigger_event_id"))
        same_event_retry = bool(row and row["status"] == "executing" and row.get("trigger_event_id") == event_id)
        if not first_claim and not same_event_retry:
            return None
        if first_claim:
            row["status"] = "executing" if row["execution_enabled"] else "triggered"
            row["trigger_event_id"] = event_id
            row["triggered_at"] = triggered_at or datetime.now(timezone.utc).isoformat()
        row["updated_at"] = datetime.now(timezone.utc)
        return self._combined_row(row)

    def finish_execution(self, condition_id: int, *, status: str, order_id: str | None = None, error_reason: str | None = None) -> dict[str, Any] | None:
        row = self.conditions.get(condition_id)
        if not row:
            return None
        row["status"] = status
        row["order_id"] = order_id
        row["error_reason"] = error_reason
        row["updated_at"] = datetime.now(timezone.utc)
        return self._combined_row(row)

    def _combined_row(self, row: dict[str, Any]) -> dict[str, Any]:
        alert = self.alert_repository.get_alert(str(row["user_sub"]), int(row["alert_id"])) or {}
        return _combined(row, alert)

    def _expire_due(self, user_sub: str, condition_id: int | None = None) -> None:
        now = datetime.now(timezone.utc)
        for row in self.conditions.values():
            if row["user_sub"] != user_sub or (condition_id is not None and row["id"] != condition_id):
                continue
            expires_at = row.get("expires_at")
            if row["status"] in {"watching", "paused"} and isinstance(expires_at, datetime) and expires_at <= now:
                row["status"] = "expired"
                row["updated_at"] = now
                self.alert_repository.update_alert_status(user_sub, int(row["alert_id"]), "disabled")


class PostgresTradeConditionRepository(TradeConditionRepository):
    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo

    @classmethod
    def from_env(cls) -> "PostgresTradeConditionRepository":
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

    def create_condition(self, condition: TradeConditionCreate) -> dict[str, Any]:
        try:
            with self._connect() as conn:
                alert = conn.execute(
                    """
                    INSERT INTO alerts (
                        user_sub, symbol, type, direction, target_price, repeat,
                        repeat_limit, status, notifications_enabled, expires_at
                    ) VALUES (%s, %s, 'price_cross', %s, %s, false, 1, 'active', %s, %s)
                    RETURNING *
                    """,
                    (
                        condition.user_sub,
                        condition.symbol,
                        _alert_direction(condition.direction),
                        condition.trigger_price,
                        condition.alerts_enabled,
                        condition.expires_at,
                    ),
                ).fetchone()
                row = conn.execute(
                    """
                    INSERT INTO trade_conditions (
                        user_sub, source, proposal_id, analysis_id, alert_id, side,
                        limit_price, quantity, exchange, execution_enabled, status,
                        validity, market_hours, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'watching', %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        condition.user_sub,
                        condition.source,
                        condition.proposal_id,
                        condition.analysis_id,
                        alert["id"],
                        condition.side,
                        condition.limit_price,
                        condition.quantity,
                        condition.exchange,
                        condition.execution_enabled,
                        condition.validity,
                        condition.market_hours,
                        condition.expires_at,
                    ),
                ).fetchone()
                conn.commit()
                return _combined(dict(row), dict(alert))
        except psycopg.errors.UniqueViolation as exc:
            raise DuplicateProposalError(str(condition.proposal_id or "duplicate")) from exc

    def list_conditions(self, user_sub: str) -> list[dict[str, Any]]:
        self._expire_due(user_sub)
        with self._connect() as conn:
            rows = conn.execute(f"{_SELECT_JOIN} WHERE tc.user_sub = %s ORDER BY tc.created_at DESC, tc.id DESC", (user_sub,)).fetchall()
            return [_json_ready(dict(row)) for row in rows]

    def get_condition(self, user_sub: str, condition_id: int) -> dict[str, Any] | None:
        self._expire_due(user_sub, condition_id)
        with self._connect() as conn:
            row = conn.execute(f"{_SELECT_JOIN} WHERE tc.user_sub = %s AND tc.id = %s", (user_sub, condition_id)).fetchone()
            return _json_ready(dict(row)) if row else None

    def update_condition(self, user_sub: str, condition_id: int, *, status: str | None = None, alerts_enabled: bool | None = None) -> dict[str, Any] | None:
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM trade_conditions WHERE user_sub = %s AND id = %s FOR UPDATE",
                (user_sub, condition_id),
            ).fetchone()
            if not existing:
                return None
            if status is not None and existing["status"] not in {"watching", "paused"}:
                return None
            if status is not None:
                conn.execute(
                    "UPDATE trade_conditions SET status = %s, version = version + 1, updated_at = now() WHERE id = %s",
                    (status, condition_id),
                )
                conn.execute(
                    "UPDATE alerts SET status = %s WHERE id = %s",
                    ("active" if status == "watching" else "disabled", existing["alert_id"]),
                )
            if alerts_enabled is not None:
                conn.execute(
                    "UPDATE alerts SET notifications_enabled = %s WHERE id = %s",
                    (alerts_enabled, existing["alert_id"]),
                )
            conn.commit()
        return self.get_condition(user_sub, condition_id)

    def delete_condition(self, user_sub: str, condition_id: int) -> dict[str, Any] | None:
        existing = self.get_condition(user_sub, condition_id)
        if not existing:
            return None
        with self._connect() as conn:
            conn.execute("DELETE FROM trade_conditions WHERE user_sub = %s AND id = %s", (user_sub, condition_id))
            conn.execute("DELETE FROM alerts WHERE user_sub = %s AND id = %s", (user_sub, existing["alert_id"]))
            conn.commit()
        return existing

    def claim_trigger(self, alert_id: int, event_id: str, triggered_at: str | None = None) -> dict[str, Any] | None:
        with self._connect() as conn:
            expired = conn.execute(
                """
                UPDATE trade_conditions
                SET status = 'expired', updated_at = now()
                WHERE alert_id = %s AND status IN ('watching', 'paused')
                  AND expires_at IS NOT NULL AND expires_at <= now()
                RETURNING alert_id
                """,
                (alert_id,),
            ).fetchone()
            if expired:
                conn.execute("UPDATE alerts SET status = 'disabled' WHERE id = %s", (expired["alert_id"],))
                conn.commit()
                return None
            row = conn.execute(
                """
                UPDATE trade_conditions
                SET status = CASE WHEN execution_enabled THEN 'executing' ELSE 'triggered' END,
                    trigger_event_id = %s,
                    triggered_at = COALESCE(triggered_at, %s::timestamptz, now()),
                    updated_at = now()
                WHERE alert_id = %s AND (
                    (status = 'watching' AND trigger_event_id IS NULL)
                    OR (status = 'executing' AND trigger_event_id = %s)
                ) AND (expires_at IS NULL OR expires_at > now())
                RETURNING user_sub, id
                """,
                (event_id, triggered_at, alert_id, event_id),
            ).fetchone()
            conn.commit()
        return self.get_condition(str(row["user_sub"]), int(row["id"])) if row else None

    def finish_execution(self, condition_id: int, *, status: str, order_id: str | None = None, error_reason: str | None = None) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE trade_conditions
                SET status = %s, order_id = %s, error_reason = %s, updated_at = now()
                WHERE id = %s
                RETURNING user_sub
                """,
                (status, order_id, error_reason, condition_id),
            ).fetchone()
            conn.commit()
        return self.get_condition(str(row["user_sub"]), condition_id) if row else None

    def _connect(self):
        return psycopg.connect(self.conninfo, row_factory=dict_row)

    def _expire_due(self, user_sub: str, condition_id: int | None = None) -> None:
        condition_clause = " AND id = %s" if condition_id is not None else ""
        params: tuple[Any, ...] = (user_sub, condition_id) if condition_id is not None else (user_sub,)
        with self._connect() as conn:
            expired = conn.execute(
                f"""
                UPDATE trade_conditions
                SET status = 'expired', updated_at = now()
                WHERE user_sub = %s AND status IN ('watching', 'paused')
                  AND expires_at IS NOT NULL AND expires_at <= now(){condition_clause}
                RETURNING alert_id
                """,
                params,
            ).fetchall()
            for row in expired:
                conn.execute("UPDATE alerts SET status = 'disabled' WHERE id = %s", (row["alert_id"],))
            conn.commit()


_SELECT_JOIN = """
SELECT tc.*, a.symbol, a.direction, a.target_price, a.notifications_enabled,
       a.triggered_count, a.status AS alert_status
FROM trade_conditions tc
JOIN alerts a ON a.id = tc.alert_id
"""


def _alert_create(condition: TradeConditionCreate) -> AlertCreate:
    return AlertCreate(
        user_sub=condition.user_sub,
        symbol=condition.symbol,
        type="price_cross",
        direction=_alert_direction(condition.direction),
        target_price=condition.trigger_price,
        repeat=False,
        repeat_limit=1,
        status="active",
        notifications_enabled=condition.alerts_enabled,
        expires_at=condition.expires_at,
    )


def _alert_direction(direction: str) -> str:
    return "below" if direction == "atOrBelow" else "above"


def _combined(condition: dict[str, Any], alert: dict[str, Any]) -> dict[str, Any]:
    return _json_ready({
        **deepcopy(condition),
        "symbol": alert.get("symbol"),
        "direction": alert.get("direction"),
        "target_price": alert.get("target_price"),
        "notifications_enabled": alert.get("notifications_enabled", True),
        "triggered_count": alert.get("triggered_count", 0),
        "alert_status": alert.get("status"),
    })


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
