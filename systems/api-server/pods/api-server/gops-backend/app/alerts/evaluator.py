from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import redis
except Exception:  # pragma: no cover - dependency guard for lean test envs
    redis = None  # type: ignore[assignment]

from app.alerts.notifications import RedisNotificationBroker
from app.alerts.projection import RedisAlertProjection
from app.alerts.repository import PostgresAlertRepository


DEFAULT_INPUT_TOPIC = "market.layer.trades.v1"
DEFAULT_TRIGGERED_TOPIC = "alerts.triggered.v1"
DEFAULT_DLQ_TOPIC = "alerts.dlq.v1"
DEFAULT_GROUP_ID = "gops-alert-evaluator"
DEFAULT_OUTBOX_STREAM = "alerts:outbox"
DEFAULT_OUTBOX_GROUP = "gops-alert-outbox-senders"
DEFAULT_SPIKE_RETENTION_MINUTES = 240
DEFAULT_DEDUPE_TTL_SECONDS = 7 * 24 * 60 * 60


def _add_alfaka_package_path() -> None:
    candidates = [os.getenv("ALFAKA_PACKAGES_PATH"), "/app/systems/market-data/shared"]
    current_file = Path(__file__).resolve()
    candidates.extend(str(parent / "systems" / "market-data" / "shared") for parent in current_file.parents)
    for candidate in candidates:
        if not candidate:
            continue
        package_path = Path(candidate)
        if (package_path / "alfaka").exists() and str(package_path) not in sys.path:
            sys.path.insert(0, str(package_path))
            return


_add_alfaka_package_path()

from alfaka.common.kafka_io import create_json_consumer, create_json_producer  # noqa: E402
from alfaka.common.symbols import normalize_market_symbol  # noqa: E402


class AlertEvaluator:
    def __init__(
        self,
        *,
        repository: Any,
        projection: RedisAlertProjection,
        outbox: "AlertRedisOutbox",
        dedupe_ttl_seconds: int = DEFAULT_DEDUPE_TTL_SECONDS,
        spike_retention_minutes: int = DEFAULT_SPIKE_RETENTION_MINUTES,
    ) -> None:
        self.repository = repository
        self.projection = projection
        self.outbox = outbox
        self.dedupe_ttl_seconds = dedupe_ttl_seconds
        self.spike_retention_ms = spike_retention_minutes * 60 * 1000

    def reconcile_active_alerts(self) -> None:
        self.projection.replace_all(self.repository.active_alerts())

    def process_trade(self, trade: dict[str, Any]) -> list[dict[str, Any]]:
        parsed = parse_trade(trade)
        if parsed is None:
            return []

        symbol = parsed["symbol"]
        price = parsed["price"]
        timestamp_ms = parsed["timestampMs"]
        previous_price = self.projection.last_price(symbol)
        events: list[dict[str, Any]] = []

        if previous_price is not None and previous_price != price:
            low, high = sorted((previous_price, price))
            for alert in self.projection.price_cross_candidates(symbol, low, high):
                event = self._price_cross_event(alert, parsed, previous_price)
                if event:
                    events.append(event)

        self.projection.remember_price(symbol, price, timestamp_ms, retention_ms=self.spike_retention_ms)
        for alert in self.projection.spike_alerts(symbol):
            event = self._spike_event(alert, parsed)
            if event:
                events.append(event)

        self.projection.set_last_price(symbol, price)
        for event in events:
            if self.projection.mark_event_seen(event["eventId"], self.dedupe_ttl_seconds):
                self.outbox.enqueue(event)
                alert = event.get("alert") or {}
                updated_alert = self.repository.record_alert_trigger(str(alert["user_sub"]), int(alert["id"]))
                if not updated_alert or updated_alert.get("status") != "active":
                    self.projection.delete_alert(alert["id"], symbol=alert.get("symbol"))
                else:
                    self.projection.upsert_alert(updated_alert)
        return events

    def _price_cross_event(
        self,
        alert: dict[str, Any],
        trade: dict[str, Any],
        previous_price: float,
    ) -> dict[str, Any] | None:
        target_price = _float_or_none(alert.get("target_price"))
        if target_price is None:
            return None
        direction = alert.get("direction")
        if direction == "above" and not (previous_price < target_price <= trade["price"]):
            return None
        if direction == "below" and not (previous_price > target_price >= trade["price"]):
            return None
        if direction not in {"above", "below"}:
            return None
        base_event_id = trade.get("sourceEventId") or trade.get("tradeId") or f"{trade['symbol']}:{trade['timestamp']}"
        return {
            "eventId": f"{alert['id']}:{base_event_id}:{direction}",
            "type": "alert.price_cross",
            "alertId": alert["id"],
            "userSub": alert["user_sub"],
            "symbol": trade["symbol"],
            "direction": direction,
            "price": trade["price"],
            "previousPrice": previous_price,
            "targetPrice": target_price,
            "triggeredAt": datetime.now(timezone.utc).isoformat(),
            "sourceEventId": trade.get("sourceEventId"),
            "tradeId": trade.get("tradeId"),
            "alert": alert,
        }

    def _spike_event(self, alert: dict[str, Any], trade: dict[str, Any]) -> dict[str, Any] | None:
        change_pct = _float_or_none(alert.get("change_pct"))
        window_min = _int_or_none(alert.get("window_min"))
        if change_pct is None or window_min is None:
            return None
        baseline = self.projection.baseline_price(trade["symbol"], trade["timestampMs"] - window_min * 60 * 1000)
        baseline_price = _float_or_none(baseline.get("price") if baseline else None)
        if baseline_price is None or baseline_price <= 0:
            return None
        actual_change_pct = ((trade["price"] - baseline_price) / baseline_price) * 100
        direction = alert.get("direction")
        if direction == "above" and actual_change_pct < change_pct:
            return None
        if direction == "below" and actual_change_pct > -change_pct:
            return None
        if direction is None and abs(actual_change_pct) < change_pct:
            return None
        resolved_direction = direction or ("above" if actual_change_pct >= 0 else "below")
        base_event_id = trade.get("sourceEventId") or trade.get("tradeId") or f"{trade['symbol']}:{trade['timestamp']}"
        return {
            "eventId": f"{alert['id']}:{base_event_id}:{resolved_direction}:{window_min}m",
            "type": "alert.spike",
            "alertId": alert["id"],
            "userSub": alert["user_sub"],
            "symbol": trade["symbol"],
            "direction": resolved_direction,
            "price": trade["price"],
            "baselinePrice": baseline_price,
            "changePct": actual_change_pct,
            "thresholdPct": change_pct,
            "windowMin": window_min,
            "triggeredAt": datetime.now(timezone.utc).isoformat(),
            "sourceEventId": trade.get("sourceEventId"),
            "tradeId": trade.get("tradeId"),
            "alert": alert,
        }


class AlertRedisOutbox:
    def __init__(self, redis_client: Any, stream: str = DEFAULT_OUTBOX_STREAM, maxlen: int = 100_000) -> None:
        self.redis = redis_client
        self.stream = os.getenv("ALERT_OUTBOX_STREAM", stream)
        self.maxlen = int(os.getenv("ALERT_OUTBOX_MAXLEN", str(maxlen)))

    def enqueue(self, payload: dict[str, Any]) -> str:
        return self.redis.xadd(
            self.stream,
            {"payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)},
            maxlen=self.maxlen,
            approximate=True,
        )


class AlertOutboxSender:
    def __init__(
        self,
        *,
        redis_client: Any,
        repository: Any,
        broker: RedisNotificationBroker,
        producer: Any,
        triggered_topic: str,
        stream: str,
        group: str,
        consumer_name: str,
    ) -> None:
        self.redis = redis_client
        self.repository = repository
        self.broker = broker
        self.producer = producer
        self.triggered_topic = triggered_topic
        self.stream = stream
        self.group = group
        self.consumer_name = consumer_name

    def ensure_group(self) -> None:
        try:
            self.redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def process_once(self, *, count: int = 100, block_ms: int = 100) -> int:
        self.ensure_group()
        messages = self.redis.xreadgroup(
            self.group,
            self.consumer_name,
            {self.stream: ">"},
            count=count,
            block=block_ms,
        )
        delivered = self._process_stream_messages(messages)
        if delivered:
            return delivered

        min_idle_ms = int(os.getenv("ALERT_OUTBOX_AUTOCLAIM_IDLE_MS", "30000"))
        claimed = self.redis.xautoclaim(self.stream, self.group, self.consumer_name, min_idle_ms, "0-0", count=count)
        entries = _entries_from_xautoclaim(claimed)
        return self._process_entries(entries)

    def _process_stream_messages(self, messages: Any) -> int:
        delivered = 0
        for _stream_name, entries in messages:
            delivered += self._process_entries(entries)
        return delivered

    def _process_entries(self, entries: Any) -> int:
        delivered = 0
        for message_id, fields in entries or []:
            payload = _payload_from_stream_fields(fields)
            self.deliver(payload)
            self.redis.xack(self.stream, self.group, message_id)
            delivered += 1
        return delivered

    def deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
        notification = self.repository.create_notification(
            user_sub=str(payload["userSub"]),
            alert_id=int(payload["alertId"]) if payload.get("alertId") is not None else None,
            event_id=str(payload["eventId"]),
            notification_type=str(payload["type"]),
            payload=payload,
        )
        websocket_payload = {
            "type": "notification",
            "notification": notification,
            "event": payload,
        }
        self.broker.publish_user(str(payload["userSub"]), websocket_payload)
        future = self.producer.send(self.triggered_topic, key=str(payload.get("symbol") or payload["alertId"]), value=payload)
        if hasattr(future, "get"):
            future.get(timeout=float(os.getenv("ALERT_KAFKA_SEND_TIMEOUT_SECONDS", "10")))
        return notification


def run() -> None:
    if redis is None:
        raise RuntimeError("redis package is not installed")
    kafka_servers = _required_env("KAFKA_BOOTSTRAP_SERVERS")
    redis_url = _required_env("REDIS_URL")
    input_topic = os.getenv("ALERT_EVALUATOR_INPUT_TOPIC", os.getenv("KAFKA_TRADES_LAYER_TOPIC", DEFAULT_INPUT_TOPIC))
    triggered_topic = os.getenv("ALERT_TRIGGERED_TOPIC", DEFAULT_TRIGGERED_TOPIC)
    group_id = os.getenv("ALERT_EVALUATOR_GROUP_ID", DEFAULT_GROUP_ID)
    dlq_topic = os.getenv("ALERT_DLQ_TOPIC", DEFAULT_DLQ_TOPIC)
    reconcile_seconds = int(os.getenv("ALERT_RECONCILE_INTERVAL_SECONDS", "60"))
    outbox_stream = os.getenv("ALERT_OUTBOX_STREAM", DEFAULT_OUTBOX_STREAM)
    outbox_group = os.getenv("ALERT_OUTBOX_GROUP", DEFAULT_OUTBOX_GROUP)
    consumer_name = os.getenv("HOSTNAME", "alert-evaluator")

    redis_client = redis.from_url(redis_url, decode_responses=True)
    repository = PostgresAlertRepository.from_env()
    projection = RedisAlertProjection(redis_client)
    producer = create_json_producer(kafka_servers, "gops-alert-evaluator")
    consumer = create_json_consumer(
        [input_topic],
        kafka_servers,
        group_id,
        "gops-alert-evaluator",
        enable_auto_commit=False,
    )
    outbox = AlertRedisOutbox(redis_client, stream=outbox_stream)
    evaluator = AlertEvaluator(repository=repository, projection=projection, outbox=outbox)
    sender = AlertOutboxSender(
        redis_client=redis_client,
        repository=repository,
        broker=RedisNotificationBroker(redis_client),
        producer=producer,
        triggered_topic=triggered_topic,
        stream=outbox_stream,
        group=outbox_group,
        consumer_name=consumer_name,
    )

    evaluator.reconcile_active_alerts()
    next_reconcile_at = time.monotonic() + reconcile_seconds
    print(
        "Alert evaluator started: "
        f"kafka={kafka_servers} input={input_topic} triggered={triggered_topic} redis={redis_url}",
        flush=True,
    )
    while True:
        try:
            records = consumer.poll(timeout_ms=1000)
            processed = False
            for _partition, partition_records in records.items():
                for record in partition_records:
                    evaluator.process_trade(record.value)
                    processed = True
            if processed:
                consumer.commit()
            sender.process_once(count=100, block_ms=50)
            if time.monotonic() >= next_reconcile_at:
                evaluator.reconcile_active_alerts()
                next_reconcile_at = time.monotonic() + reconcile_seconds
        except Exception as exc:
            traceback.print_exc()
            try:
                producer.send(
                    dlq_topic,
                    key="alert-evaluator",
                    value={"error": str(exc), "occurredAt": datetime.now(timezone.utc).isoformat()},
                )
            except Exception:
                traceback.print_exc()
            time.sleep(1)


def parse_trade(payload: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    symbol_value = payload.get("symbol")
    price = _float_or_none(payload.get("price") or payload.get("p"))
    if not isinstance(symbol_value, str) or price is None:
        return None
    try:
        symbol = normalize_market_symbol(symbol_value)
    except Exception:
        return None
    timestamp = str(payload.get("timestamp") or payload.get("t") or datetime.now(timezone.utc).isoformat())
    return {
        "symbol": symbol,
        "price": price,
        "timestamp": timestamp,
        "timestampMs": _timestamp_ms(timestamp),
        "sourceEventId": payload.get("sourceEventId"),
        "tradeId": payload.get("tradeId") or payload.get("i"),
    }


def _payload_from_stream_fields(fields: dict[str, Any]) -> dict[str, Any]:
    payload = fields.get("payload")
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        loaded = json.loads(payload)
        if isinstance(loaded, dict):
            return loaded
    if isinstance(payload, dict):
        return payload
    raise ValueError("alert outbox payload is invalid")


def _entries_from_xautoclaim(value: Any) -> list[Any]:
    if isinstance(value, tuple | list):
        if len(value) >= 2 and isinstance(value[1], list):
            return value[1]
        if value and all(isinstance(item, tuple | list) and len(item) == 2 for item in value):
            return list(value)
        return []
    return []


def _timestamp_ms(value: str) -> int:
    try:
        normalized = value.replace("Z", "+00:00")
        return int(datetime.fromisoformat(normalized).timestamp() * 1000)
    except Exception:
        return int(time.time() * 1000)


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


if __name__ == "__main__":
    run()
