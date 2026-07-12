from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .envelope import ChartAssetBuildEnvelope
from .storage import _database_conninfo


JOBS_TABLE = "chart_assets.geometry_build_jobs"
ITEMS_TABLE = "chart_assets.geometry_build_items"
TERMINAL_ITEM_STATUSES = {"saved", "saved_with_warning", "unchanged", "failed", "skipped"}
TERMINAL_JOB_STATUSES = {"completed", "completed_with_warnings", "completed_with_errors", "failed", "canceled"}


class PostgresChartAssetJobStore:
    def __init__(self, conninfo: str | None = None, *, connect: Callable[..., Any] | None = None) -> None:
        self.conninfo = conninfo or _database_conninfo()
        self._connector = connect or psycopg.connect

    def enqueue(self, envelope: ChartAssetBuildEnvelope) -> dict[str, Any]:
        total = len(envelope.symbols) * len(envelope.intervals)
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {JOBS_TABLE} (
                    job_id, requested_by, submitted_at, status, force_build,
                    requested_intervals, symbol_count, total_items, repair, logs
                ) VALUES (%s, %s, %s, 'queued', %s, %s, %s, %s, '{{}}'::jsonb, '[]'::jsonb)
                ON CONFLICT (job_id) DO NOTHING
                """,
                (
                    envelope.job_id, envelope.requested_by, _timestamp(envelope.submitted_at), envelope.force,
                    Jsonb(list(envelope.intervals)), len(envelope.symbols), total,
                ),
            )
            for symbol in envelope.symbols:
                for interval in envelope.intervals:
                    conn.execute(
                        f"""
                        INSERT INTO {ITEMS_TABLE} (job_id, symbol, "interval", status)
                        VALUES (%s, %s, %s, 'pending')
                        ON CONFLICT (job_id, symbol, "interval") DO NOTHING
                        """,
                        (envelope.job_id, symbol, interval),
                    )
            conn.commit()
        return self.get(envelope.job_id) or {}

    def claim_next(self, worker_id: str, *, lease_seconds: int = 900) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                WITH candidate AS (
                    SELECT item.job_id, item.symbol, item."interval"
                    FROM {ITEMS_TABLE} item
                    JOIN {JOBS_TABLE} job ON job.job_id = item.job_id
                    WHERE job.cancel_requested = false
                      AND job.status IN ('queued', 'running')
                      AND item.attempts < 2
                      AND (
                          item.status = 'pending'
                          OR (item.status = 'running' AND item.lease_expires_at < now())
                      )
                    ORDER BY job.submitted_at, item.job_id, item.symbol, item."interval"
                    FOR UPDATE OF item SKIP LOCKED
                    LIMIT 1
                )
                UPDATE {ITEMS_TABLE} item
                SET status = 'running', stage = 'claimed', attempts = item.attempts + 1,
                    worker_id = %s, lease_expires_at = now() + make_interval(secs => %s),
                    started_at = COALESCE(item.started_at, now()), updated_at = now()
                FROM candidate
                WHERE item.job_id = candidate.job_id
                  AND item.symbol = candidate.symbol
                  AND item."interval" = candidate."interval"
                RETURNING item.job_id, item.symbol, item."interval", item.attempts
                """,
                (worker_id, lease_seconds),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            job = conn.execute(
                f"""
                UPDATE {JOBS_TABLE}
                SET status = 'running', started_at = COALESCE(started_at, now()), updated_at = now()
                WHERE job_id = %s
                RETURNING requested_by, submitted_at, force_build, requested_intervals
                """,
                (row["job_id"],),
            ).fetchone()
            conn.commit()
        intervals = job["requested_intervals"] if isinstance(job["requested_intervals"], list) else json.loads(job["requested_intervals"])
        envelope = ChartAssetBuildEnvelope.create(
            job_id=row["job_id"], requested_by=job["requested_by"],
            submitted_at=_iso(job["submitted_at"]), symbols=[row["symbol"]],
            intervals=intervals, force=job["force_build"],
        )
        return {"envelope": envelope, "symbol": row["symbol"], "interval": row["interval"], "attempts": row["attempts"]}

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            job = conn.execute(f"SELECT * FROM {JOBS_TABLE} WHERE job_id = %s", (job_id,)).fetchone()
            if job is None:
                return None
            items = conn.execute(
                f"SELECT * FROM {ITEMS_TABLE} WHERE job_id = %s ORDER BY updated_at DESC, symbol, \"interval\"",
                (job_id,),
            ).fetchall()
        done = [item for item in items if item["status"] in TERMINAL_ITEM_STATUSES]
        failed = [item for item in items if item["status"] == "failed"]
        warnings = [item for item in items if item["status"] == "saved_with_warning" or item.get("warning")]
        skipped = [item for item in items if item["status"] == "skipped"]
        recent = [_item_payload(item) for item in items[:50]]
        requested_intervals = job["requested_intervals"] if isinstance(job["requested_intervals"], list) else json.loads(job["requested_intervals"])
        return {
            "jobId": job_id, "status": job["status"],
            "requested": {"symbolCount": job["symbol_count"], "intervals": requested_intervals, "force": job["force_build"]},
            "progress": {
                "total": job["total_items"], "done": len(done), "failed": len(failed),
                "skipped": len(skipped), "warnings": len(warnings),
                "current": next((f"{item['symbol']}:{item['interval']}" for item in items if item["status"] == "running"), None),
            },
            "repair": dict(job.get("repair") or {}), "recentItems": recent,
            "failedItems": [_item_payload(item) for item in failed], "logs": list(job.get("logs") or [])[-200:],
            "createdEntities": int(job.get("created_entities") or 0),
            "cancelRequested": bool(job["cancel_requested"]),
            "startedAt": _iso(job.get("started_at")), "finishedAt": _iso(job.get("finished_at")),
        }

    def record_item(self, job_id: str, item: dict[str, Any]) -> dict[str, Any] | None:
        status = str(item.get("status") or "failed")
        if status not in TERMINAL_ITEM_STATUSES:
            raise ValueError(f"Unsupported terminal chart asset item status: {status}")
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE {ITEMS_TABLE}
                SET status = %s, stage = %s, error = %s, warning = %s, reason = %s,
                    elapsed_ms = %s, created_entities = %s, lease_expires_at = NULL,
                    finished_at = now(), updated_at = now()
                WHERE job_id = %s AND symbol = %s AND "interval" = %s
                """,
                (
                    status, str(item.get("stage") or "done"), item.get("error"), item.get("warning"), item.get("reason"),
                    int(item.get("elapsedMs") or 0), int(item.get("createdEntities") or 0),
                    job_id, str(item.get("symbol") or "").upper(), str(item.get("interval") or ""),
                ),
            )
            self._finish_job_if_terminal(conn, job_id)
            conn.commit()
        return self.get(job_id)

    def set_status(self, job_id: str, status: str, **values: Any) -> dict[str, Any] | None:
        assignments = ["status = %s", "updated_at = now()"]
        parameters: list[Any] = [status]
        mapping = {"startedAt": "started_at", "finishedAt": "finished_at", "createdEntities": "created_entities"}
        for key, column in mapping.items():
            if key in values:
                assignments.append(f"{column} = %s")
                parameters.append(_timestamp(values[key]) if key != "createdEntities" else int(values[key] or 0))
        parameters.append(job_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE {JOBS_TABLE} SET {', '.join(assignments)} WHERE job_id = %s", tuple(parameters))
            conn.commit()
        return self.get(job_id)

    def request_cancel(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE {JOBS_TABLE}
                SET cancel_requested = true, status = 'canceled', finished_at = now(), updated_at = now()
                WHERE job_id = %s
                """, (job_id,),
            )
            conn.execute(
                f"""
                UPDATE {ITEMS_TABLE}
                SET status = 'skipped', stage = 'cancel', reason = 'cancel_requested', finished_at = now(), updated_at = now()
                WHERE job_id = %s AND status = 'pending'
                """, (job_id,),
            )
            conn.commit()
        return self.get(job_id)

    def is_cancel_requested(self, job_id: str) -> bool:
        state = self.get(job_id)
        return bool(state and state["cancelRequested"])

    def add_log(self, job_id: str, message: str) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE {JOBS_TABLE}
                SET logs = COALESCE(logs, '[]'::jsonb) || jsonb_build_array(%s::text),
                    updated_at = now()
                WHERE job_id = %s
                """, (str(message)[:500], job_id),
            )
            conn.commit()

    def record_repair(self, job_id: str, result: dict[str, Any]) -> None:
        with self._connect() as conn:
            row = conn.execute(f"SELECT repair FROM {JOBS_TABLE} WHERE job_id = %s FOR UPDATE", (job_id,)).fetchone()
            repair = dict((row or {}).get("repair") or {})
            for key in ("checkedSymbols", "attemptedSymbols", "repairedSymbols", "unavailableSymbols", "missingBarsBefore", "missingBarsAfter", "materializedRows"):
                repair.setdefault(key, 0)
            repair.setdefault("reasonCodes", {})
            repair["checkedSymbols"] += int(bool(result.get("checked")))
            repair["attemptedSymbols"] += int(bool(result.get("attempted")))
            repair["repairedSymbols"] += int(bool(result.get("repaired")))
            repair["unavailableSymbols"] += int(bool(result.get("unavailable")))
            repair["missingBarsBefore"] += int(result.get("missing_before") or 0)
            repair["missingBarsAfter"] += int(result.get("missing_after") or 0)
            repair["materializedRows"] += int(result.get("materialized_rows") or 0)
            reason = str(result.get("reason") or "")
            if reason and reason not in {"coverage_complete", "repaired"}:
                repair["reasonCodes"][reason] = int(repair["reasonCodes"].get(reason) or 0) + 1
            conn.execute(
                f"UPDATE {JOBS_TABLE} SET repair = %s, updated_at = now() WHERE job_id = %s",
                (Jsonb(repair), job_id),
            )
            conn.commit()

    def _finish_job_if_terminal(self, conn: Any, job_id: str) -> None:
        counts = conn.execute(
            f"""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE status IN ('saved','saved_with_warning','unchanged','failed','skipped')) AS done,
                   count(*) FILTER (WHERE status = 'failed') AS failed,
                   count(*) FILTER (WHERE status = 'saved_with_warning' OR warning IS NOT NULL) AS warnings,
                   COALESCE(sum(created_entities), 0) AS entities
            FROM {ITEMS_TABLE} WHERE job_id = %s
            """, (job_id,),
        ).fetchone()
        if counts and counts["total"] == counts["done"]:
            status = "completed_with_errors" if counts["failed"] else "completed_with_warnings" if counts["warnings"] else "completed"
            conn.execute(
                f"UPDATE {JOBS_TABLE} SET status = %s, created_entities = %s, finished_at = now(), updated_at = now() WHERE job_id = %s AND status <> 'canceled'",
                (status, counts["entities"], job_id),
            )

    def _connect(self) -> Any:
        return self._connector(self.conninfo, row_factory=dict_row)


def _item_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": item["symbol"], "interval": item["interval"], "status": item["status"],
        "stage": item["stage"], "error": item.get("error"), "warning": item.get("warning"),
        "reason": item.get("reason"), "elapsedMs": int(item.get("elapsed_ms") or 0),
    }


def _timestamp(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
