import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import redis

from alfaka.common.env import load_dotenv, utc_now_iso
from alfaka.common.redis_keys import RedisKeyBuilder
from alfaka.serving.intervals import backfill_target_days, normalize_chart_interval


TERMINAL_STATUSES = {"succeeded", "failed", "unavailable"}
ACTIVE_STATUSES = {"queued", "running"}
RETRYABLE_TERMINAL_STATUSES = {"failed", "unavailable"}


@dataclass(frozen=True)
class BackfillRange:
    start: str
    end: str


def default_backfill_range(now=None, lookback_hours=None, interval="1m"):
    resolved_now = now or datetime.now(timezone.utc)
    if isinstance(resolved_now, str):
        resolved_now = parse_time(resolved_now)
    resolved_now = resolved_now.replace(second=0, microsecond=0)
    hours = int(lookback_hours) if lookback_hours else backfill_target_days(interval) * 24
    start = resolved_now - timedelta(hours=hours)
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
    def __init__(self, redis_client=None, redis_url=None, keys=None, ttl_seconds=None):
        load_dotenv()
        self.redis = redis_client or redis.from_url(redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
        self.keys = keys or RedisKeyBuilder()
        self.ttl_seconds = int(ttl_seconds or os.getenv("BACKFILL_STATUS_TTL_SECONDS", "86400"))

    def create_request(self, symbol, interval, start=None, end=None, mode="default", source="api", force=False):
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
            "requestId": request_id,
            "symbol": symbol,
            "interval": interval,
            "range": {"start": backfill_range.start, "end": backfill_range.end},
            "status": "queued",
            "mode": mode,
            "source": source,
            "requestedAt": utc_now_iso(),
            "updatedAt": utc_now_iso(),
            "startedAt": None,
            "finishedAt": None,
            "error": None,
            "force": bool(force),
        }
        self.set_status(record)
        self.redis.set(latest_key, request_id, ex=self.ttl_seconds)
        self.redis.lpush(self.keys.backfill_queue(), request_id)
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

    def pop_queued_request_id(self, timeout=5):
        try:
            result = self.redis.brpop(self.keys.backfill_queue(), timeout=timeout)
        except redis.exceptions.TimeoutError:
            return None
        if not result:
            return None
        return result[1] if isinstance(result, (list, tuple)) else result


def parse_time(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def to_iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
