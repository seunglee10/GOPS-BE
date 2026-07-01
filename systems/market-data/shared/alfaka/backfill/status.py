import hashlib
import json
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timezone

import redis

from alfaka.common.env import load_dotenv, utc_now_iso
from alfaka.common.redis_keys import RedisKeyBuilder
from alfaka.serving.intervals import normalize_chart_interval


TERMINAL_STATUSES = {"succeeded", "failed", "unavailable"}
ACTIVE_STATUSES = {"queued", "running"}
RETRYABLE_TERMINAL_STATUSES = {"failed", "unavailable"}
BACKFILL_STATUS_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class BackfillRange:
    start: str
    end: str


@dataclass(frozen=True)
class BackfillQueueItem:
    request_id: str
    stream_id: str | None = None
    delivery_count: int = 1


def resolve_backfill_range(start=None, end=None, interval="1m"):
    _ = normalize_chart_interval(interval)
    if not start or not end:
        raise ValueError("Backfill range requires explicit start and end timestamps.")
    start_dt = parse_time(start)
    end_dt = parse_time(end)
    if end_dt <= start_dt:
        raise ValueError("Backfill range end must be after start.")
    return BackfillRange(to_iso(start_dt), to_iso(end_dt))


def range_digest(symbol, interval, start, end):
    payload = f"{symbol}|{interval}|{start}|{end}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def request_id_for(symbol, interval, start, end):
    digest = range_digest(symbol, interval, start, end)
    return f"backfill:{symbol}:{interval}:{digest}"


class RedisBackfillStore:
    def __init__(
        self,
        redis_client=None,
        redis_url=None,
        keys=None,
        ttl_seconds=None,
        queue_backend=None,
        stream_group=None,
        stream_maxlen=None,
    ):
        load_dotenv()
        self.redis = redis_client or redis.from_url(redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
        self.keys = keys or RedisKeyBuilder()
        self.ttl_seconds = int(ttl_seconds or os.getenv("BACKFILL_STATUS_TTL_SECONDS", "604800"))
        self.queue_backend = (queue_backend or os.getenv("BACKFILL_QUEUE_BACKEND", "streams")).strip().lower()
        self.stream_group = stream_group or os.getenv("BACKFILL_STREAM_GROUP", "backfill-workers")
        self.stream_maxlen = int(stream_maxlen or os.getenv("BACKFILL_STREAM_MAXLEN", "100000"))

    def create_request(
        self,
        symbol,
        interval,
        start=None,
        end=None,
        source="api",
        force=False,
        job_type="gapfill",
        source_preference="coverage-first",
        priority="normal",
    ):
        interval = normalize_chart_interval(interval)
        backfill_range = resolve_backfill_range(start, end, interval)
        digest = range_digest(symbol, interval, backfill_range.start, backfill_range.end)
        base_request_id = request_id_for(symbol, interval, backfill_range.start, backfill_range.end)
        request_id = base_request_id
        if force:
            force_digest = hashlib.sha1(utc_now_iso().encode("utf-8")).hexdigest()[:8]
            request_id = f"{base_request_id}:force:{force_digest}"
        lock_key = self.keys.backfill_lock(symbol, interval, digest)
        latest_key = self.keys.backfill_latest(symbol, interval)
        locked = self.redis.set(lock_key, request_id, nx=not force, ex=self.ttl_seconds)

        if not locked:
            existing_id = self.redis.get(lock_key) or self.redis.get(latest_key) or request_id
            existing = self.get_status(existing_id)
            if existing and existing.get("status") not in RETRYABLE_TERMINAL_STATUSES:
                return existing, True
            retry_digest = hashlib.sha1(utc_now_iso().encode("utf-8")).hexdigest()[:8]
            request_id = f"{base_request_id}:retry:{retry_digest}"
            self.redis.set(lock_key, request_id, ex=self.ttl_seconds)

        record = {
            "schemaVersion": BACKFILL_STATUS_SCHEMA_VERSION,
            "requestId": request_id,
            "symbol": symbol,
            "interval": interval,
            "range": {"start": backfill_range.start, "end": backfill_range.end},
            "status": "queued",
            "source": source,
            "jobType": normalize_job_type(job_type),
            "sourcePreference": source_preference,
            "priority": priority,
            "idempotencyKey": base_request_id,
            "attempt": 0,
            "claimedBy": None,
            "claimedAt": None,
            "heartbeatAt": None,
            "checkpoint": None,
            "streamId": None,
            "queueBackend": self.queue_backend,
            "requestedAt": utc_now_iso(),
            "updatedAt": utc_now_iso(),
            "startedAt": None,
            "finishedAt": None,
            "error": None,
            "force": bool(force),
        }
        self.set_status(record)
        self.redis.set(latest_key, request_id, ex=self.ttl_seconds)
        record = self.enqueue_request(record)
        return record, False

    def get_status(self, request_id):
        if not request_id:
            return None
        value = self.redis.get(self.keys.backfill_status(request_id))
        return json.loads(value) if value else None

    def latest_status(self, symbol, interval):
        request_id = self.redis.get(self.keys.backfill_latest(symbol, interval))
        return self.get_status(request_id) if request_id else None

    def set_status(self, record):
        record = {**record, "updatedAt": utc_now_iso()}
        self.redis.set(self.keys.backfill_status(record["requestId"]), json.dumps(record, ensure_ascii=False, separators=(",", ":")), ex=self.ttl_seconds)
        self.redis.set(self.keys.backfill_latest(record["symbol"], record["interval"]), record["requestId"], ex=self.ttl_seconds)
        return record

    def update_status(self, record, status, **fields):
        next_record = {**record, **fields, "status": status}
        if status == "running" and not next_record.get("startedAt"):
            next_record["startedAt"] = utc_now_iso()
        if status in TERMINAL_STATUSES and not next_record.get("finishedAt"):
            next_record["finishedAt"] = utc_now_iso()
        return self.set_status(next_record)

    def record_no_data_before(self, symbol, interval, boundary):
        interval = normalize_chart_interval(interval)
        value = to_iso(parse_time(boundary))
        key = self.keys.backfill_no_data_before(symbol, interval)
        current = self.redis.get(key)
        if current and parse_time(current) >= parse_time(value):
            return current
        self.redis.set(key, value, ex=self.ttl_seconds)
        return value

    def enqueue_request(self, record):
        if self.queue_backend == "list":
            self.redis.lpush(self.keys.backfill_queue(), record["requestId"])
            return self.set_status({**record, "queueBackend": "list"})

        self.ensure_stream_group()
        fields = {
            "requestId": record["requestId"],
            "symbol": record["symbol"],
            "interval": record["interval"],
            "jobType": record.get("jobType", "gapfill"),
            "priority": record.get("priority", "normal"),
        }
        stream_id = self.redis.xadd(
            self.keys.backfill_stream(),
            fields,
            maxlen=self.stream_maxlen,
            approximate=True,
        )
        return self.set_status({**record, "queueBackend": "streams", "streamId": stream_id})

    def ensure_stream_group(self):
        try:
            self.redis.xgroup_create(self.keys.backfill_stream(), self.stream_group, id="0", mkstream=True)
        except redis_response_error_type() as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def read_next_queue_item(self, consumer_name=None, timeout=5, reclaim_idle_ms=None, max_attempts=None):
        consumer_name = consumer_name or default_consumer_name()
        if self.queue_backend == "list":
            request_id = self.pop_queued_request_id(timeout=timeout)
            return BackfillQueueItem(request_id=request_id) if request_id else None

        self.ensure_stream_group()
        if reclaim_idle_ms is not None:
            reclaimed = self.reclaim_stale_queue_item(consumer_name, min_idle_ms=reclaim_idle_ms, max_attempts=max_attempts)
            if reclaimed:
                return reclaimed
        try:
            response = self.redis.xreadgroup(
                self.stream_group,
                consumer_name,
                {self.keys.backfill_stream(): ">"},
                count=1,
                block=int(timeout * 1000),
            )
        except redis_timeout_error_type():
            return None
        return self._queue_item_from_xread_response(response)

    def reclaim_stale_queue_item(self, consumer_name=None, min_idle_ms=600000, max_attempts=None):
        consumer_name = consumer_name or default_consumer_name()
        if self.queue_backend == "list":
            return None
        self.ensure_stream_group()
        pending = self.redis.xpending_range(
            self.keys.backfill_stream(),
            self.stream_group,
            min="-",
            max="+",
            count=10,
        )
        for entry in pending or []:
            entry_id = pending_entry_id(entry)
            if not entry_id:
                continue
            idle_ms = pending_entry_idle_ms(entry)
            delivery_count = pending_entry_delivery_count(entry)
            if idle_ms is not None and idle_ms < min_idle_ms:
                continue
            record = self._record_for_stream_id(entry_id)
            request_id = pending_entry_request_id(entry) or (record or {}).get("requestId")
            if not request_id:
                continue
            item = BackfillQueueItem(request_id=request_id, stream_id=entry_id, delivery_count=delivery_count)
            if max_attempts is not None and delivery_count >= max_attempts:
                self.dead_letter_queue_item(item, record, reason="max_attempts_exceeded")
                continue
            claimed = self.redis.xclaim(
                self.keys.backfill_stream(),
                self.stream_group,
                consumer_name,
                min_idle_time=min_idle_ms,
                message_ids=[entry_id],
            )
            parsed = self._queue_item_from_claimed_messages(claimed)
            if parsed:
                return parsed
        return None

    def mark_job_claimed(self, record, item, consumer_name=None):
        consumer_name = consumer_name or default_consumer_name()
        now = utc_now_iso()
        return self.update_status(
            record,
            "running",
            streamId=item.stream_id or record.get("streamId"),
            attempt=max(int(record.get("attempt") or 0) + 1, int(item.delivery_count or 1)),
            claimedBy=consumer_name,
            claimedAt=now,
            heartbeatAt=now,
        )

    def heartbeat(self, record, checkpoint=None):
        fields = {"heartbeatAt": utc_now_iso()}
        if checkpoint is not None:
            fields["checkpoint"] = checkpoint
        return self.set_status({**record, **fields})

    def ack_queue_item(self, item):
        if not item:
            return 0
        if self.queue_backend == "list" or not item.stream_id:
            return 0
        return self.redis.xack(self.keys.backfill_stream(), self.stream_group, item.stream_id)

    def dead_letter_queue_item(self, item, record=None, reason="unknown"):
        record = record or self.get_status(item.request_id)
        payload = {
            "requestId": item.request_id,
            "streamId": item.stream_id or "",
            "reason": reason,
            "deadLetteredAt": utc_now_iso(),
        }
        if record:
            payload["symbol"] = record.get("symbol", "")
            payload["interval"] = record.get("interval", "")
        self.redis.xadd(self.keys.backfill_dead_letter_stream(), payload, maxlen=self.stream_maxlen, approximate=True)
        if record:
            self.update_status(record, "failed", error=f"Backfill dead-lettered: {reason}")
        return self.ack_queue_item(item)

    def queue_metrics(self, pending_sample_size=100):
        observed_at = utc_now_iso()
        if self.queue_backend == "list":
            queue_key = self.keys.backfill_queue()
            return {
                "queueBackend": "list",
                "observedAt": observed_at,
                "list": {
                    "key": queue_key,
                    "length": safe_int_call(getattr(self.redis, "llen", None), queue_key),
                },
                "stream": None,
                "deadLetter": {
                    "key": self.keys.backfill_dead_letter_stream(),
                    "length": safe_int_call(getattr(self.redis, "xlen", None), self.keys.backfill_dead_letter_stream()),
                },
            }

        self.ensure_stream_group()
        stream_key = self.keys.backfill_stream()
        group_info = self._stream_group_info(stream_key)
        pending_entries = self.redis.xpending_range(
            stream_key,
            self.stream_group,
            min="-",
            max="+",
            count=pending_sample_size,
        )
        pending_count = int(first_present(group_info, ("pending", "pending_count"), len(pending_entries or [])) or 0)
        undelivered_count = first_present(group_info, ("lag", "entries-read-lag"), None)
        if undelivered_count is not None:
            undelivered_count = int(undelivered_count)
        oldest_pending = oldest_pending_entry(pending_entries)
        return {
            "queueBackend": "streams",
            "observedAt": observed_at,
            "stream": {
                "key": stream_key,
                "group": self.stream_group,
                "retainedLength": safe_int_call(getattr(self.redis, "xlen", None), stream_key),
                "pendingCount": pending_count,
                "undeliveredCount": undelivered_count,
                "backlogCount": pending_count + undelivered_count if undelivered_count is not None else None,
                "consumerCount": int(first_present(group_info, ("consumers", "consumer_count"), 0) or 0),
                "lastDeliveredId": first_present(group_info, ("last-delivered-id", "last_delivered_id"), None),
                "oldestPending": oldest_pending,
                "pendingSampleSize": len(pending_entries or []),
            },
            "list": None,
            "deadLetter": {
                "key": self.keys.backfill_dead_letter_stream(),
                "length": safe_int_call(getattr(self.redis, "xlen", None), self.keys.backfill_dead_letter_stream()),
            },
        }

    def _stream_group_info(self, stream_key):
        try:
            groups = self.redis.xinfo_groups(stream_key)
        except Exception:
            return {}
        for group in groups or []:
            if first_present(group, ("name", "group"), None) == self.stream_group:
                return group
        return {}

    def pop_queued_request_id(self, timeout=5):
        if self.queue_backend != "list":
            item = self.read_next_queue_item(consumer_name="legacy-pop", timeout=timeout)
            if not item:
                return None
            self.ack_queue_item(item)
            return item.request_id
        try:
            result = self.redis.brpop(self.keys.backfill_queue(), timeout=timeout)
        except redis_timeout_error_type():
            return None
        if not result:
            return None
        return result[1] if isinstance(result, (list, tuple)) else result

    def _queue_item_from_xread_response(self, response):
        if not response:
            return None
        _stream, messages = response[0]
        if not messages:
            return None
        stream_id, fields = messages[0]
        return BackfillQueueItem(
            request_id=fields.get("requestId"),
            stream_id=stream_id,
            delivery_count=int(fields.get("deliveryCount") or fields.get("delivery_count") or 1),
        )

    def _queue_item_from_claimed_messages(self, messages):
        if not messages:
            return None
        stream_id, fields = messages[0]
        request_id = fields.get("requestId") or (self._record_for_stream_id(stream_id) or {}).get("requestId")
        delivery_count = int(fields.get("deliveryCount") or fields.get("delivery_count") or 2)
        return BackfillQueueItem(request_id=request_id, stream_id=stream_id, delivery_count=delivery_count)

    def _record_for_stream_id(self, stream_id):
        return None


def parse_time(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def to_iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_job_type(value):
    job_type = (value or "gapfill").strip().lower().replace("-", "_")
    allowed = {"gapfill"}
    return job_type if job_type in allowed else "gapfill"


def default_consumer_name():
    return os.getenv("BACKFILL_WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}"


def queue_backlog_count(metrics):
    if not metrics:
        return 0
    stream = metrics.get("stream") or {}
    backlog = stream.get("backlogCount")
    if backlog is not None:
        return int(backlog)
    pending = stream.get("pendingCount")
    undelivered = stream.get("undeliveredCount")
    if pending is not None or undelivered is not None:
        return int(pending or 0) + int(undelivered or 0)
    list_metrics = metrics.get("list") or {}
    if list_metrics.get("length") is not None:
        return int(list_metrics["length"])
    return int(stream.get("retainedLength") or 0)


def redis_response_error_type():
    return getattr(getattr(redis, "exceptions", object), "ResponseError", Exception)


def redis_timeout_error_type():
    return getattr(getattr(redis, "exceptions", object), "TimeoutError", TimeoutError)


def pending_entry_id(entry):
    if isinstance(entry, dict):
        return entry.get("message_id") or entry.get("messageId") or entry.get("id")
    if isinstance(entry, (list, tuple)) and entry:
        return entry[0]
    return None


def pending_entry_idle_ms(entry):
    if isinstance(entry, dict):
        for key in ("time_since_delivered", "idle", "idle_ms"):
            if key in entry:
                return entry[key]
        return None
    if isinstance(entry, (list, tuple)) and len(entry) >= 3:
        return entry[2]
    return None


def pending_entry_delivery_count(entry):
    if isinstance(entry, dict):
        return int(entry.get("times_delivered") or entry.get("delivery_count") or 1)
    if isinstance(entry, (list, tuple)) and len(entry) >= 4:
        return int(entry[3] or 1)
    return 1


def pending_entry_request_id(entry):
    if isinstance(entry, dict):
        return entry.get("requestId") or entry.get("request_id")
    return None


def first_present(mapping, keys, default=None):
    for key in keys:
        if isinstance(mapping, dict) and key in mapping:
            return mapping[key]
    return default


def safe_int_call(fn, *args):
    if not fn:
        return None
    try:
        value = fn(*args)
    except Exception:
        return None
    return int(value) if value is not None else None


def oldest_pending_entry(entries):
    oldest = None
    for entry in entries or []:
        idle_ms = pending_entry_idle_ms(entry)
        if idle_ms is None:
            continue
        if oldest is None or idle_ms > oldest["idleMs"]:
            oldest = {
                "streamId": pending_entry_id(entry),
                "requestId": pending_entry_request_id(entry),
                "idleMs": int(idle_ms),
                "deliveryCount": pending_entry_delivery_count(entry),
            }
    return oldest
