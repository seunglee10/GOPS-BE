"""Thread-safe in-memory repository used by unit tests and fake smoke flows."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import RLock
from typing import Any
from uuid import uuid4

from kis_trader.domain.commands import OrderCommand
from kis_trader.domain.envelope import build_order_fill_envelope, build_order_status_envelope, enrich_order_envelope_identity
from kis_trader.domain.status import OrderStatus, assert_transition_allowed, is_terminal_status
from kis_trader.domain.topics import (
    ORDER_EVENTS_TOPIC,
    ORDERS_COMMANDS_TOPIC,
    ORDERS_DLQ_TOPIC,
    ORDERS_FILLS_TOPIC,
    SUBMIT_RESULTS_TOPIC,
    build_order_message_key,
)

FILL_STATUSES = {OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED}
from kis_trader.security.redaction import redact_sensitive

from .fills import canonical_fill_observation
from .repository import IdempotencyConflictError, OrderCreationResult, OrderNotFoundError, SubmissionIntent, utc_now_iso


class InMemoryOrderRepository:
    def __init__(self) -> None:
        self._lock = RLock()
        self.orders: dict[str, dict[str, Any]] = {}
        self.order_events: dict[str, dict[str, Any]] = {}
        self.outbox_events: dict[str, dict[str, Any]] = {}
        self.idempotency_requests: dict[str, dict[str, Any]] = {}
        self.broker_submissions_by_request: dict[str, dict[str, Any]] = {}
        self.broker_submissions_by_client: dict[str, dict[str, Any]] = {}
        self.executions: dict[str, dict[str, Any]] = {}
        self.order_coach_fill_history: list[dict[str, Any]] = []
        self.dlq_events: list[dict[str, Any]] = []
        self.audit_logs: list[dict[str, Any]] = []
        self.inbox_events: dict[tuple[str, str], dict[str, Any]] = {}

    def create_received_order(
        self,
        *,
        idempotency_key_hash: str,
        body_hash: str,
        command: OrderCommand,
        user_sub: str | None = None,
    ) -> OrderCreationResult:
        with self._lock:
            existing = self.idempotency_requests.get(idempotency_key_hash)
            if existing is not None:
                if existing["body_hash"] != body_hash:
                    raise IdempotencyConflictError("idempotency key reused with a different request body")
                order = self.orders[existing["order_id"]]
                response = dict(existing["response"])
                response["idempotent_replay"] = True
                return OrderCreationResult(False, True, dict(order), response, None)

            order = {
                "order_id": command.order_id,
                "request_id": command.request_id,
                "client_order_id": command.client_order_id,
                "account_alias": command.account_alias,
                "market": command.market,
                "symbol": command.symbol,
                "side": command.side,
                "qty": str(command.qty),
                "price": str(command.price),
                "exchange": command.exchange,
                "order_division": command.order_division,
                "status": OrderStatus.RECEIVED.value,
                "broker_order_id": command.broker_order_id,
                "reason": None,
                "occurred_at": command.occurred_at,
                "updated_at": utc_now_iso(),
                "user_sub": user_sub,
            }
            self.orders[command.order_id] = order
            self._append_order_event(command.order_id, OrderStatus.RECEIVED, None, command)
            outbox_event_id = self._insert_outbox_event(
                ORDERS_COMMANDS_TOPIC,
                command.order_id,
                OrderStatus.RECEIVED,
                enrich_order_envelope_identity(command.to_envelope(), order),
                message_key=build_order_message_key(command.account_alias, command.symbol),
            )
            response = {
                "order_id": command.order_id,
                "request_id": command.request_id,
                "client_order_id": command.client_order_id,
                "status": OrderStatus.RECEIVED.value,
                "idempotent_replay": False,
                "outbox_event_id": outbox_event_id,
            }
            self.idempotency_requests[idempotency_key_hash] = {
                "key_hash": idempotency_key_hash,
                "body_hash": body_hash,
                "order_id": command.order_id,
                "status": "COMPLETED",
                "response": redact_sensitive(response),
                "created_at": utc_now_iso(),
            }
            return OrderCreationResult(True, False, dict(order), response, outbox_event_id)

    def ensure_published_order_for_command(self, command: OrderCommand) -> None:
        with self._lock:
            if command.order_id in self.orders:
                return
            self.orders[command.order_id] = {
                "order_id": command.order_id,
                "request_id": command.request_id,
                "client_order_id": command.client_order_id,
                "account_alias": command.account_alias,
                "market": command.market,
                "symbol": command.symbol,
                "side": command.side,
                "qty": str(command.qty),
                "price": str(command.price),
                "exchange": command.exchange,
                "order_division": command.order_division,
                "status": OrderStatus.PUBLISHED.value,
                "broker_order_id": command.broker_order_id,
                "reason": None,
                "occurred_at": command.occurred_at,
                "updated_at": utc_now_iso(),
            }
            self._append_order_event(command.order_id, OrderStatus.PUBLISHED, "reconstructed from command", command)

    def find_idempotent_response(self, idempotency_key_hash: str, body_hash: str) -> dict[str, Any] | None:
        with self._lock:
            existing = self.idempotency_requests.get(idempotency_key_hash)
            if existing is None or existing["body_hash"] != body_hash:
                return None
            return dict(existing["response"])

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.orders.get(order_id)
            return dict(row) if row else None

    def list_order_events(self, order_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = [dict(event) for event in self.order_events.values() if event["order_id"] == order_id]
        return sorted(rows, key=lambda row: row["created_at"])

    def update_order_status(self, order_id: str, status: OrderStatus, reason: str | None = None) -> str:
        with self._lock:
            if order_id not in self.orders:
                raise OrderNotFoundError(f"unknown order_id: {order_id}")
            current = OrderStatus(self.orders[order_id]["status"])
            assert_transition_allowed(current, status)
            self.orders[order_id]["status"] = status.value
            self.orders[order_id]["reason"] = reason
            self.orders[order_id]["updated_at"] = utc_now_iso()
            event_id = self._append_order_event(order_id, status, reason)
            if status in FILL_STATUSES:
                order = self.orders[order_id]
                self._insert_outbox_event(
                    ORDERS_FILLS_TOPIC,
                    order_id,
                    status,
                    build_order_fill_envelope(order, reason=reason),
                    message_key=build_order_message_key(order["account_alias"], order["symbol"]),
                )
            return event_id

    def fetch_pending_outbox(self, limit: int | None = None, topic: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            events = [
                dict(event)
                for event in self.outbox_events.values()
                if event["published_at"] is None and (topic is None or event["topic"] == topic)
            ]
        events.sort(key=lambda event: event["created_at"])
        return events[:limit] if limit is not None else events

    def claim_pending_outbox(
        self,
        *,
        worker_id: str,
        limit: int | None = None,
        topic: str | None = None,
        lease_seconds: int = 30,
    ) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        with self._lock:
            eligible = []
            for event in self.outbox_events.values():
                if event["published_at"] is not None or (topic is not None and event["topic"] != topic):
                    continue
                next_attempt_at = datetime.fromisoformat(event["next_attempt_at"])
                locked_at = datetime.fromisoformat(event["locked_at"]) if event["locked_at"] else None
                if next_attempt_at > now or (locked_at is not None and locked_at >= now - timedelta(seconds=lease_seconds)):
                    continue
                eligible.append(event)
            eligible.sort(key=lambda event: (event["created_at"], event["event_id"]))
            claimed = eligible[:limit] if limit is not None else eligible
            for event in claimed:
                event["delivery_status"] = "publishing"
                event["attempt_count"] += 1
                event["locked_at"] = now.isoformat()
                event["lock_owner"] = worker_id
            return [dict(event) for event in claimed]

    def mark_outbox_published(self, event_id: str, *, worker_id: str | None = None) -> None:
        with self._lock:
            event = self.outbox_events[event_id]
            if worker_id is not None and event["lock_owner"] not in (None, worker_id):
                return
            event["published_at"] = utc_now_iso()
            event["delivery_status"] = "published"
            event["locked_at"] = None
            event["lock_owner"] = None
            event["last_error"] = None
            if event["topic"] == ORDERS_COMMANDS_TOPIC:
                self.update_order_status(event["order_id"], OrderStatus.PUBLISHED)

    def mark_outbox_failed(
        self,
        event_id: str,
        error: Exception | str,
        *,
        worker_id: str | None = None,
        retry_delay_seconds: int = 5,
    ) -> None:
        with self._lock:
            event = self.outbox_events[event_id]
            if worker_id is not None and event["lock_owner"] not in (None, worker_id):
                return
            event["delivery_status"] = "retry"
            event["next_attempt_at"] = (datetime.now(timezone.utc) + timedelta(seconds=retry_delay_seconds)).isoformat()
            event["last_error"] = str(error)[:4000]
            event["locked_at"] = None
            event["lock_owner"] = None

    def inbox_event_seen(self, consumer_name: str, event_id: str) -> bool:
        with self._lock:
            return (consumer_name, event_id) in self.inbox_events

    def record_inbox_event(
        self,
        consumer_name: str,
        event_id: str,
        *,
        payload_digest: str | None = None,
    ) -> bool:
        with self._lock:
            key = (consumer_name, event_id)
            if key in self.inbox_events:
                return False
            self.inbox_events[key] = {
                "consumer_name": consumer_name,
                "event_id": event_id,
                "payload_digest": payload_digest,
                "processed_at": utc_now_iso(),
            }
            return True

    def claim_submission_intent(self, command: OrderCommand) -> SubmissionIntent:
        with self._lock:
            existing = self.find_submission(command.request_id, command.client_order_id)
            if existing is not None:
                return SubmissionIntent(False, existing)
            submission = {
                "submission_id": f"sub_{uuid4().hex}",
                "request_id": command.request_id,
                "client_order_id": command.client_order_id,
                "order_id": command.order_id,
                "status": "INTENT_RECORDED",
                "redacted_command": redact_sensitive(_json_ready(asdict(command))),
                "redacted_response": None,
                "reason": None,
                "broker_order_id": command.broker_order_id,
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            }
            self.broker_submissions_by_request[command.request_id] = submission
            self.broker_submissions_by_client[command.client_order_id] = submission
            return SubmissionIntent(True, dict(submission))

    def find_submission(self, request_id: str, client_order_id: str) -> dict[str, Any] | None:
        with self._lock:
            submission = self.broker_submissions_by_request.get(request_id) or self.broker_submissions_by_client.get(
                client_order_id
            )
            return dict(submission) if submission else None

    def record_submission_result(
        self,
        submission: dict[str, Any],
        status: OrderStatus,
        *,
        response: dict[str, Any] | None = None,
        reason: str | None = None,
        broker_order_id: str | None = None,
    ) -> None:
        with self._lock:
            stored = self.broker_submissions_by_request[submission["request_id"]]
            stored["status"] = status.value
            stored["redacted_response"] = redact_sensitive(response or {})
            stored["reason"] = reason
            stored["broker_order_id"] = broker_order_id or stored.get("broker_order_id")
            stored["updated_at"] = utc_now_iso()
            if stored["broker_order_id"]:
                self.orders[stored["order_id"]]["broker_order_id"] = stored["broker_order_id"]
            self.update_order_status(stored["order_id"], status, reason)
            payload = {
                "status": status.value,
                "reason": reason,
                "broker_order_id": stored.get("broker_order_id"),
                "response": stored["redacted_response"],
            }
            order = self.orders[stored["order_id"]]
            envelope = build_order_status_envelope(
                order,
                event_type="order.submit.resulted",
                producer="kis-broker-adapter",
                source=SUBMIT_RESULTS_TOPIC,
                payload=payload,
            )
            self._insert_outbox_event(
                SUBMIT_RESULTS_TOPIC,
                stored["order_id"],
                status,
                envelope,
                message_key=build_order_message_key(order["account_alias"], order["symbol"]),
            )

    def record_dlq(self, *, source: str, message: Any, error: Exception) -> None:
        with self._lock:
            self.dlq_events.append(
                {
                    "source": source,
                    "topic": ORDERS_DLQ_TOPIC,
                    "error_type": type(error).__name__,
                    "error_message": str(error),
                    "payload": redact_sensitive(message),
                    "created_at": utc_now_iso(),
                }
            )

    def record_audit(self, action: str, order_id: str, reason: str | None = None) -> None:
        with self._lock:
            order = self.orders.get(order_id, {})
            self.audit_logs.append(
                {
                    "action": action,
                    "order_id": order_id,
                    "request_id": order.get("request_id"),
                    "client_order_id": order.get("client_order_id"),
                    "account_alias": order.get("account_alias"),
                    "symbol": order.get("symbol"),
                    "reason": reason,
                    "created_at": utc_now_iso(),
                }
            )

    def find_orders_by_status(self, statuses: set[OrderStatus]) -> list[dict[str, Any]]:
        status_values = {status.value for status in statuses}
        with self._lock:
            return [dict(order) for order in self.orders.values() if order["status"] in status_values]

    def find_open_orders(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(order) for order in self.orders.values() if not is_terminal_status(order["status"])]

    def update_reconciled_order(
        self,
        order_id: str,
        status: OrderStatus,
        reason: str | None,
        *,
        execution_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            if execution_id is not None and execution_id not in self.executions:
                self.executions[execution_id] = redact_sensitive(payload or {})
            self.update_order_status(order_id, status, reason)
            order = self.orders[order_id]
            observation = canonical_fill_observation(
                order, payload, execution_id=execution_id
            )
            if observation is not None:
                previous = [
                    row for row in self.order_coach_fill_history
                    if row["fill_id"] == observation["fill_id"]
                ]
                latest = previous[-1] if previous else None
                if latest is None or latest["cumulative_filled_qty"] < observation["cumulative_filled_qty"]:
                    self.order_coach_fill_history.append({
                        **observation,
                        "id": len(self.order_coach_fill_history) + 1,
                        "observation_version": int(latest["observation_version"]) + 1 if latest else 1,
                    })
            envelope = build_order_status_envelope(
                order,
                event_type="order.broker.event.reconciled",
                producer="broker-event-reconciler",
                source=ORDER_EVENTS_TOPIC,
                payload=redact_sensitive(payload or {}),
            )
            self._insert_outbox_event(
                ORDER_EVENTS_TOPIC,
                order_id,
                status,
                envelope,
                message_key=build_order_message_key(order["account_alias"], order["symbol"]),
            )

    def metrics_snapshot(self) -> dict[str, Any]:
        with self._lock:
            unknown = [order for order in self.orders.values() if order["status"] == OrderStatus.SUBMIT_FAILED_UNKNOWN.value]
            reconciliation_required = [
                order for order in self.orders.values() if order["status"] == OrderStatus.RECONCILIATION_REQUIRED.value
            ]
            return {
                "orders_total": len(self.orders),
                "outbox_unpublished": len(self.fetch_pending_outbox()),
                "dlq_count": len(self.dlq_events),
                "submit_failed_unknown_count": len(unknown),
                "reconciliation_required_count": len(reconciliation_required),
                "audit_log_count": len(self.audit_logs),
            }

    def _append_order_event(
        self,
        order_id: str,
        status: OrderStatus,
        reason: str | None,
        command: OrderCommand | None = None,
    ) -> str:
        event_id = f"evt_{uuid4().hex}"
        order = self.orders.get(order_id, {})
        self.order_events[event_id] = {
            "event_id": event_id,
            "order_id": order_id,
            "request_id": order.get("request_id") or (command.request_id if command else None),
            "client_order_id": order.get("client_order_id") or (command.client_order_id if command else None),
            "account_alias": order.get("account_alias") or (command.account_alias if command else None),
            "symbol": order.get("symbol") or (command.symbol if command else None),
            "status": status.value,
            "reason": reason,
            "created_at": utc_now_iso(),
        }
        return event_id

    def _insert_outbox_event(
        self,
        topic: str,
        order_id: str,
        status: OrderStatus,
        payload: dict[str, Any],
        *,
        message_key: str,
    ) -> str:
        event_id = f"out_{uuid4().hex}"
        self.outbox_events[event_id] = {
            "event_id": event_id,
            "topic": topic,
            "message_key": message_key,
            "key": message_key,
            "order_id": order_id,
            "status": status.value,
            "payload": redact_sensitive(_json_ready(payload)),
            "delivery_status": "pending",
            "attempt_count": 0,
            "next_attempt_at": utc_now_iso(),
            "last_error": None,
            "locked_at": None,
            "lock_owner": None,
            "published_at": None,
            "created_at": utc_now_iso(),
        }
        return event_id


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_ready(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_json_ready(child) for child in value]
    return value
