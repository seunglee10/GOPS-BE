"""Match shared paper-ledger SIM orders against every ordered replay quote."""

from __future__ import annotations

import json
import hashlib
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any

from kis_trader.paper.postgres import PostgresPaperTradingRepository
from kis_trader.domain.commands import validate_order_request_payload
from kis_trader.runtime_heartbeat import touch_heartbeat


MATCHER_ID = "simulation-paper-matcher-v1"


def main() -> int:
    repository = PostgresPaperTradingRepository.from_env()
    base_url = os.getenv("GOPS_SIMULATOR_URL", "http://gops-simulator:8765").rstrip("/")
    idle_seconds = max(0.05, float(os.getenv("SIMULATION_MATCHER_IDLE_SECONDS", "0.25")))
    batch_size = max(1, min(int(os.getenv("SIMULATION_MATCHER_BATCH_SIZE", "50000")), 50000))
    while True:
        try:
            status = _request(base_url, "/api/control/status")
            run_id = str(status.get("runId") or "") if status.get("mode") == "simulation" else ""
            if not run_id:
                touch_heartbeat()
                time.sleep(idle_seconds)
                continue
            sequence = _load_checkpoint(repository, run_id)
            page = _request(
                base_url,
                "/api/control/execution-events?" + urllib.parse.urlencode({
                    "runId": run_id,
                    "afterSequence": sequence,
                    "limit": batch_size,
                }),
            )
            for quote in page.get("quotes") or []:
                quote_sequence = int(quote.get("sequence") or 0)
                _trigger_conditions(repository, run_id, quote, quote_sequence)
                repository.match_quote(
                    symbol=str(quote.get("symbol") or ""),
                    bid_price=_positive_decimal(quote.get("bid")),
                    ask_price=_positive_decimal(quote.get("ask")),
                    quote_timestamp=str(quote.get("timestamp") or "") or None,
                    quote_event_id=f"simulation:{run_id}:{quote_sequence}",
                    execution_mode="simulation",
                    simulation_run_id=run_id,
                    quote_sequence=quote_sequence,
                    virtual_timestamp=str(quote.get("timestamp") or "") or None,
                )
            next_sequence = int(page.get("nextSequence") or sequence)
            _save_checkpoint(repository, run_id, next_sequence)
            touch_heartbeat()
            if page.get("caughtUp") is True:
                time.sleep(idle_seconds)
        except Exception as exc:
            print(f"simulation paper matcher idle/error: {exc}", flush=True)
            touch_heartbeat()
            time.sleep(max(1.0, idle_seconds))


def _request(base_url: str, path: str) -> dict[str, Any]:
    request = urllib.request.Request(f"{base_url}{path}", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"simulator HTTP {exc.code}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("simulator returned an invalid payload")
    return payload


def _load_checkpoint(repository: PostgresPaperTradingRepository, run_id: str) -> int:
    with repository._connect() as conn:
        row = conn.execute(
            "SELECT run_id, sequence FROM simulation_matcher_checkpoints WHERE matcher_id = %s",
            (MATCHER_ID,),
        ).fetchone()
        return int(row["sequence"] or 0) if row and row["run_id"] == run_id else 0


def _save_checkpoint(repository: PostgresPaperTradingRepository, run_id: str, sequence: int) -> None:
    with repository._connect() as conn:
        conn.execute(
            """
            INSERT INTO simulation_matcher_checkpoints (matcher_id, run_id, sequence, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (matcher_id) DO UPDATE
            SET run_id = EXCLUDED.run_id,
                sequence = CASE
                    WHEN simulation_matcher_checkpoints.run_id = EXCLUDED.run_id
                    THEN GREATEST(simulation_matcher_checkpoints.sequence, EXCLUDED.sequence)
                    ELSE EXCLUDED.sequence
                END,
                updated_at = now()
            """,
            (MATCHER_ID, run_id, max(0, int(sequence))),
        )
        conn.commit()


def _positive_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _trigger_conditions(
    repository: PostgresPaperTradingRepository,
    run_id: str,
    quote: dict[str, Any],
    sequence: int,
) -> None:
    symbol = str(quote.get("symbol") or "").strip().upper()
    event_id = f"simulation:{run_id}:{sequence}"
    bid = _positive_decimal(quote.get("bid"))
    ask = _positive_decimal(quote.get("ask"))
    if not symbol or (bid is None and ask is None):
        return
    with repository._connect() as conn:
        candidates = conn.execute(
            """
            SELECT tc.*, a.direction, a.target_price
            FROM trade_conditions tc
            JOIN alerts a ON a.id = tc.alert_id
            WHERE tc.execution_mode = 'simulation'
              AND tc.simulation_run_id = %s
              AND tc.simulation_submitted_sequence < %s
              AND a.symbol = %s
              AND (
                tc.status = 'watching'
                OR (tc.status = 'executing' AND tc.trigger_event_id = %s)
              )
            ORDER BY tc.id
            FOR UPDATE OF tc SKIP LOCKED
            """,
            (run_id, sequence, symbol, event_id),
        ).fetchall()
        triggered: list[dict[str, Any]] = []
        for row in candidates:
            price = ask if row["side"] == "buy" else bid
            if price is None:
                continue
            threshold = Decimal(row["target_price"])
            crossed = price <= threshold if row["direction"] == "below" else price >= threshold
            if not crossed:
                continue
            if row["status"] == "watching":
                next_status = "executing" if row["execution_enabled"] else "triggered"
                conn.execute(
                    """
                    UPDATE trade_conditions
                    SET status = %s, trigger_event_id = %s,
                        triggered_at = COALESCE(%s::timestamptz, now()), updated_at = now()
                    WHERE id = %s AND status = 'watching'
                    """,
                    (next_status, event_id, quote.get("timestamp"), row["id"]),
                )
                conn.execute(
                    """
                    UPDATE alerts
                    SET status = 'disabled', triggered_count = triggered_count + 1,
                        last_triggered_at = COALESCE(%s::timestamptz, now())
                    WHERE id = %s
                    """,
                    (quote.get("timestamp"), row["alert_id"]),
                )
            if row["execution_enabled"]:
                triggered.append(dict(row))
        conn.commit()
    for condition in triggered:
        try:
            request_payload = {
                "market": "overseas",
                "symbol": symbol,
                "side": condition["side"],
                "qty": str(condition["quantity"]),
                "price": str(condition["limit_price"]),
                "exchange": condition["exchange"],
                "order_division": "00",
            }
            order_request = validate_order_request_payload(
                request_payload,
                default_account_alias="paper-account",
            )
            condition_key = f"simulation-condition:{run_id}:{condition['id']}"
            created = repository.create_order(
                user_id=str(condition["user_sub"]),
                idempotency_key_hash=hashlib.sha256(condition_key.encode("utf-8")).hexdigest(),
                body_hash=hashlib.sha256(json.dumps(request_payload, sort_keys=True).encode("utf-8")).hexdigest(),
                request=order_request,
                execution_mode="simulation",
                simulation_run_id=run_id,
                simulation_submitted_sequence=sequence - 1,
                virtual_submitted_at=str(quote.get("timestamp") or "") or None,
                order_type="limit",
            )
            _finish_condition(repository, int(condition["id"]), "completed", created.order["order_id"], None)
        except Exception as exc:
            _finish_condition(repository, int(condition["id"]), "failed", None, str(exc))


def _finish_condition(
    repository: PostgresPaperTradingRepository,
    condition_id: int,
    status: str,
    order_id: str | None,
    error_reason: str | None,
) -> None:
    with repository._connect() as conn:
        conn.execute(
            """
            UPDATE trade_conditions
            SET status = %s, order_id = %s, error_reason = %s, updated_at = now()
            WHERE id = %s
            """,
            (status, order_id, error_reason, condition_id),
        )
        conn.commit()


if __name__ == "__main__":
    raise SystemExit(main())
