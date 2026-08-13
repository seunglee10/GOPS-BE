import json
import os
import socket
import time

import redis

from market_data.backfill.runner import BackfillRunner
from market_data.backfill.status import RedisBackfillStore, TERMINAL_STATUSES
from market_data.common.env import load_dotenv


def main():
    load_dotenv()
    store = RedisBackfillStore()
    runner = BackfillRunner(store=store)
    run_once = os.getenv("BACKFILL_WORKER_ONCE", "false").lower() in {"1", "true", "yes"}
    poll_seconds = int(os.getenv("BACKFILL_WORKER_POLL_SECONDS", "5"))
    consumer_name = os.getenv("BACKFILL_WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}"
    reclaim_idle_ms = int(os.getenv("BACKFILL_STREAM_RECLAIM_IDLE_MS", "600000"))
    max_attempts = int(os.getenv("BACKFILL_MAX_ATTEMPTS", "3"))

    while True:
        try:
            item = store.read_next_queue_item(
                consumer_name=consumer_name,
                timeout=poll_seconds,
                reclaim_idle_ms=reclaim_idle_ms,
                max_attempts=max_attempts,
            )
        except redis.exceptions.RedisError as exc:
            if run_once:
                raise
            print(f"Backfill worker waiting for Redis: {exc}", flush=True)
            time.sleep(poll_seconds)
            continue
        if not item:
            if run_once:
                return
            continue
        record = store.get_status(item.request_id)
        if not record:
            store.ack_queue_item(item)
            continue
        if record.get("status") in TERMINAL_STATUSES:
            store.ack_queue_item(item)
            continue
        if int(item.delivery_count or 1) > max_attempts:
            store.dead_letter_queue_item(item, record, reason="max_attempts_exceeded")
            if run_once:
                return
            continue
        record = store.mark_job_claimed(record, item, consumer_name=consumer_name)
        print(f"Backfill worker processing {item.request_id}", flush=True)
        result = runner.run(record)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if result.get("status") in {"succeeded", "failed", "unavailable"}:
            store.ack_queue_item(item)
        if run_once:
            return
        time.sleep(0)


if __name__ == "__main__":
    main()
