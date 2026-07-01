import hashlib
import json
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import redis

from alfaka.common.env import load_dotenv, utc_now_iso
from alfaka.common.redis_keys import RedisKeyBuilder
from alfaka.serving.intervals import (
    INTRADAY_PRELOAD_MIN_START_ENV,
    backfill_target_days,
    intraday_preload_min_start_iso,
    normalize_chart_interval,
    source_interval_for,
)


TERMINAL_STATUSES = {"succeeded", "failed", "unavailable"}
ACTIVE_STATUSES = {"queued", "running"}
RETRYABLE_TERMINAL_STATUSES = {"failed", "unavailable"}
DEFAULT_ACTIVE_STALE_SECONDS = 30 * 60
DEFAULT_MAX_1M_GAPFILL_HOURS = 14 * 24


@dataclass(frozen=True)
class BackfillRange:
    start: str
    end: str


@dataclass(frozen=True)
class BackfillQueueItem:
    request_id: str
    stream_id: str | None = None
    delivery_count: int = 1


def default_backfill_range(now=None, lookback_hours=None, interval="1m"):
    interval = normalize_chart_interval(interval)
    resolved_now = now or datetime.now(timezone.utc)
    if isinstance(resolved_now, str):
        resolved_now = parse_time(resolved_now)
    resolved_now = resolved_now.replace(second=0, microsecond=0)
    hours = int(lookback_hours) if lookback_hours else backfill_target_days(interval) * 24
    start = resolved_now - timedelta(hours=hours)
    if source_interval_for(interval) == "1m":
        guard = initial_load_range_guard("1m")
        start = max(start, parse_time(guard["minStart"]))
    return BackfillRange(to_iso(start), to_iso(resolved_now))


def resolve_backfill_range(start=None, end=None, interval="1m"):
    if start and end:
        return BackfillRange(to_iso(parse_time(start)), to_iso(parse_time(end)))
    return default_backfill_range(interval=interval)


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
        mode="default",
        source="api",
        force=False,
        job_type="gapfill",
        source_preference="coverage-first",
        priority="normal",
    ):
        interval = normalize_chart_interval(interval)
        backfill_range = resolve_backfill_range(start, end, interval)
        validate_backfill_request_range(interval, backfill_range, force=force, job_type=job_type)
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
            "requestId": request_id,
            "symbol": symbol,
            "interval": interval,
            "range": {"start": backfill_range.start, "end": backfill_range.end},
            "status": "queued",
            "mode": mode,
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

    def create_initial_load_requests(
        self,
        symbols,
        interval,
        start,
        end,
        chunk_days=None,
        max_enqueued=None,
        max_backlog=None,
        source_preference="coverage-first",
        priority="bulk",
        force=False,
    ):
        interval = normalize_chart_interval(interval)
        if interval not in {"1m", "1D"}:
            raise ValueError("Initial Load v1 supports canonical source intervals 1m and 1D only.")
        validate_initial_load_range(interval, start, end)
        symbol_list = [str(symbol).strip().upper() for symbol in symbols or [] if str(symbol).strip()]
        chunks = chunk_backfill_range(start, end, interval, chunk_days=chunk_days)
        max_enqueued = int(max_enqueued if max_enqueued is not None else os.getenv("BACKFILL_INITIAL_LOAD_MAX_ENQUEUE", "100"))
        max_backlog = int(max_backlog if max_backlog is not None else os.getenv("BACKFILL_INITIAL_LOAD_MAX_BACKLOG", "1000"))
        backlog_before = queue_backlog_count(self.queue_metrics())
        if backlog_before >= max_backlog:
            return {
                "jobType": "initial_load",
                "interval": interval,
                "symbolCount": len(symbol_list),
                "chunkCount": len(chunks) * len(symbol_list),
                "createdCount": 0,
                "skippedExistingCount": 0,
                "remainingCount": len(chunks) * len(symbol_list),
                "backlogBefore": backlog_before,
                "maxBacklog": max_backlog,
                "throttled": True,
                "requests": [],
                "skippedExisting": [],
            }

        capacity = max(0, min(max_enqueued, max_backlog - backlog_before))
        created = []
        skipped_existing = []
        for symbol in symbol_list:
            for chunk in chunks:
                if len(created) >= capacity:
                    break
                existing = self.find_existing_initial_load_status(symbol, interval, chunk)
                repair_existing_without_evidence = False
                if existing and not force and should_skip_existing_initial_load(existing):
                    skipped_existing.append({
                        "requestId": existing["requestId"],
                        "symbol": symbol,
                        "interval": interval,
                        "range": existing.get("range"),
                        "status": existing.get("status"),
                    })
                    continue
                if existing and not force and existing.get("status") == "succeeded":
                    repair_existing_without_evidence = True
                record, deduplicated = self.create_request(
                    symbol,
                    interval,
                    start=chunk.start,
                    end=chunk.end,
                    mode="queue",
                    source="initial-load",
                    force=force or repair_existing_without_evidence,
                    job_type="initial_load",
                    source_preference=source_preference,
                    priority=priority,
                )
                created.append({
                    "requestId": record["requestId"],
                    "symbol": symbol,
                    "interval": interval,
                    "range": record.get("range"),
                    "deduplicated": deduplicated,
                })
            if len(created) >= capacity:
                break

        total_chunks = len(chunks) * len(symbol_list)
        remaining_count = total_chunks - len(skipped_existing)
        return {
            "jobType": "initial_load",
            "interval": interval,
            "symbolCount": len(symbol_list),
            "chunkCount": total_chunks,
            "createdCount": len(created),
            "skippedExistingCount": len(skipped_existing),
            "remainingCount": remaining_count,
            "backlogBefore": backlog_before,
            "maxBacklog": max_backlog,
            "throttled": len(created) < remaining_count,
            "requests": created,
            "skippedExisting": skipped_existing[:10],
        }

    def find_existing_initial_load_status(self, symbol, interval, chunk):
        base_request_id = request_id_for(symbol, interval, chunk.start, chunk.end)
        existing = self.get_status(base_request_id)
        digest = range_digest(symbol, interval, chunk.start, chunk.end)
        lock_id = self.redis.get(self.keys.backfill_lock(symbol, interval, digest))
        if lock_id and lock_id != base_request_id:
            locked = self.get_status(lock_id)
            if locked and locked.get("idempotencyKey") == base_request_id:
                if should_skip_existing_initial_load(locked):
                    return locked
                if not existing or existing.get("status") in RETRYABLE_TERMINAL_STATUSES:
                    return locked
        return existing

    def get_status(self, request_id):
        if not request_id:
            return None
        value = self.redis.get(self.keys.backfill_status(request_id))
        if not value:
            return None
        record = json.loads(value)
        return self.refresh_stale_status(record)

    def latest_status(self, symbol, interval):
        request_id = self.redis.get(self.keys.backfill_latest(symbol, interval))
        return self.get_status(request_id) if request_id else None

    def refresh_stale_status(self, record):
        if not is_stale_active_record(record):
            return record
        return self.update_status(
            record,
            "failed",
            error="Backfill marked stale after missing heartbeat; retry with a bounded range.",
        )

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
            request_id = pending_entry_request_id(entry) or self._request_id_for_stream_id(entry_id)
            record = self.get_status(request_id) if request_id else self._record_for_stream_id(entry_id)
            request_id = request_id or (record or {}).get("requestId")
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
        request_id = fields.get("requestId") or self._request_id_for_stream_id(stream_id) or (self._record_for_stream_id(stream_id) or {}).get("requestId")
        delivery_count = int(fields.get("deliveryCount") or fields.get("delivery_count") or 2)
        return BackfillQueueItem(request_id=request_id, stream_id=stream_id, delivery_count=delivery_count)

    def _request_id_for_stream_id(self, stream_id):
        fields = self._stream_fields_for_stream_id(stream_id)
        return fields.get("requestId") if fields else None

    def _stream_fields_for_stream_id(self, stream_id):
        if not stream_id:
            return {}
        try:
            rows = self.redis.xrange(self.keys.backfill_stream(), min=stream_id, max=stream_id, count=1)
        except Exception:
            return {}
        if not rows:
            return {}
        _row_id, fields = rows[0]
        return fields or {}

    def _record_for_stream_id(self, stream_id):
        if not stream_id:
            return None
        request_id = self._request_id_for_stream_id(stream_id)
        if request_id:
            record = self.get_status(request_id)
            if record:
                return record
        # Redis Streams do not provide an efficient reverse lookup by stream ID.
        # Status records store streamId so tests and low-volume recovery can find
        # the matching request; production workers should carry requestId in the
        # pending message fields.
        for key, value in getattr(self.redis, "values", {}).items():
            if ":backfill:status:" not in key and not key.startswith("backfill:status:"):
                continue
            try:
                record = json.loads(value)
            except Exception:
                continue
            if record.get("streamId") == stream_id:
                return record
        return None


def parse_time(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def to_iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_job_type(value):
    job_type = (value or "gapfill").strip().lower().replace("-", "_")
    allowed = {"initial_load", "gapfill", "replay_repair", "correction_replay"}
    return job_type if job_type in allowed else "gapfill"


def validate_backfill_request_range(interval, backfill_range, *, force=False, job_type="gapfill"):
    if normalize_job_type(job_type) != "gapfill":
        return
    source_interval = source_interval_for(interval)
    if source_interval != "1m":
        return
    start = parse_time(backfill_range.start)
    end = parse_time(backfill_range.end)
    if end <= start:
        raise ValueError("Backfill range end must be after start.")
    max_hours = float(os.getenv("BACKFILL_MAX_GAPFILL_1M_RANGE_HOURS", str(DEFAULT_MAX_1M_GAPFILL_HOURS)))
    if max_hours <= 0:
        return
    duration_hours = (end - start).total_seconds() / 3600
    if duration_hours > max_hours:
        force_suffix = " force=true" if force else ""
        raise ValueError(
            f"Rejected oversized 1m gapfill{force_suffix}: requested {duration_hours:.1f}h, "
            f"max {max_hours:.1f}h. Use initial-load or S3 materialize jobs for bulk rebuilds."
        )


def is_stale_active_record(record):
    if not record or record.get("status") not in ACTIVE_STATUSES:
        return False
    if normalize_job_type(record.get("jobType")) != "gapfill":
        return False
    stale_seconds = float(os.getenv("BACKFILL_ACTIVE_STALE_SECONDS", str(DEFAULT_ACTIVE_STALE_SECONDS)))
    if stale_seconds <= 0:
        return False
    reference = record.get("heartbeatAt") or record.get("updatedAt") or record.get("startedAt") or record.get("requestedAt")
    if not reference:
        return False
    try:
        reference_time = parse_time(reference)
    except ValueError:
        return False
    age = datetime.now(timezone.utc) - reference_time
    return age.total_seconds() > stale_seconds


def should_skip_existing_initial_load(record):
    status = record.get("status")
    if status in ACTIVE_STATUSES:
        return True
    if status == "succeeded":
        return initial_load_has_s3_evidence(record)
    return status not in RETRYABLE_TERMINAL_STATUSES


def initial_load_has_s3_evidence(record):
    result = record.get("result") or {}
    if result.get("emptyRange") and result.get("emptyMarker"):
        return True
    processed_objects = result.get("processedObjects") or record.get("processedObjects") or []
    if processed_objects:
        return True
    source = result.get("source") or record.get("source")
    return source in {"alpaca", "s3-processed", "s3-processed-replay"} and bool(result.get("materializedRowCount"))


def initial_load_range_guard(interval):
    interval = normalize_chart_interval(interval)
    if interval != "1m":
        return None
    raw_start = intraday_preload_min_start_iso()
    return {
        "env": INTRADAY_PRELOAD_MIN_START_ENV,
        "minStart": to_iso(parse_time(raw_start)),
        "reason": "1m_initial_load_preload_scope",
    }


def validate_initial_load_range(interval, start, end):
    guard = initial_load_range_guard(interval)
    if not guard:
        return
    start_dt = parse_time(start)
    min_start_dt = parse_time(guard["minStart"])
    if start_dt < min_start_dt:
        raise ValueError(
            f"Initial Load 1m start {to_iso(start_dt)} is before "
            f"{guard['env']}={guard['minStart']}; 1m preload before the configured 6-year floor is disabled. "
            "Load 1D first, then expand 1m through bounded reviewed windows."
        )


def default_consumer_name():
    return os.getenv("BACKFILL_WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}"


def initial_load_chunk_days(interval, chunk_days=None):
    if chunk_days is not None:
        return int(chunk_days)
    interval = normalize_chart_interval(interval)
    if interval == "1D":
        return int(os.getenv("BACKFILL_INITIAL_LOAD_1D_CHUNK_DAYS", "370"))
    return int(os.getenv("BACKFILL_INITIAL_LOAD_1M_CHUNK_DAYS", "5"))


def chunk_backfill_range(start, end, interval, chunk_days=None):
    start_dt = parse_time(start)
    end_dt = parse_time(end)
    if end_dt <= start_dt:
        raise ValueError("Backfill chunk end must be after start.")
    days = initial_load_chunk_days(interval, chunk_days=chunk_days)
    if days <= 0:
        raise ValueError("Backfill chunk days must be positive.")
    chunks = []
    cursor = start_dt
    while cursor < end_dt:
        chunk_end = min(cursor + timedelta(days=days), end_dt)
        chunks.append(BackfillRange(to_iso(cursor), to_iso(chunk_end)))
        cursor = chunk_end
    return chunks


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
