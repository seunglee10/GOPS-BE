import json
import os
import socket
import time

import redis

from alfaka.backfill.runner import BackfillRunner
from alfaka.backfill.status import RedisBackfillStore
from alfaka.common.env import load_dotenv


def main():
    load_dotenv()
    store = RedisBackfillStore()
    runner = BackfillRunner(store=store)
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
            print(f"Backfill worker waiting for Redis: {exc}", flush=True)
            time.sleep(poll_seconds)
            continue
        if not item:
            continue
        record = store.get_status(item.request_id)
        if not record:
            store.ack_queue_item(item)
            continue
        if int(item.delivery_count or 1) > max_attempts:
            store.dead_letter_queue_item(item, record, reason="max_attempts_exceeded")
            continue
        record = store.mark_job_claimed(record, item, consumer_name=consumer_name)
        print(f"Backfill worker processing {item.request_id}", flush=True)
        result = runner.run(record)
        print(json.dumps(summarize_worker_result(result), ensure_ascii=False), flush=True)
        if result.get("status") in {"succeeded", "failed", "unavailable"}:
            store.ack_queue_item(item)
        time.sleep(0)


def summarize_worker_result(result):
    payload = result.get("result") if isinstance(result, dict) else None
    payload = payload if isinstance(payload, dict) else {}
    gap_ranges = payload.get("gapRanges") if isinstance(payload.get("gapRanges"), list) else []
    fetch_ranges = payload.get("fetchRanges") if isinstance(payload.get("fetchRanges"), list) else []
    archive_objects = payload.get("archiveObjects") if isinstance(payload.get("archiveObjects"), list) else []
    return {
        "event": "backfill.result",
        "requestId": result.get("requestId"),
        "symbol": result.get("symbol"),
        "interval": result.get("interval"),
        "status": result.get("status"),
        "range": result.get("range"),
        "error": result.get("error"),
        "source": payload.get("source"),
        "jobType": payload.get("jobType") or result.get("jobType"),
        "sourcePreference": payload.get("sourcePreference") or result.get("sourcePreference"),
        "rawRowCount": payload.get("rawRowCount"),
        "processedRowCount": payload.get("processedRowCount"),
        "materializedRowCount": payload.get("materializedRowCount"),
        "skippedInvalidRowCount": payload.get("skippedInvalidRowCount"),
        "archiveStatus": payload.get("archiveStatus"),
        "archiveRowCount": payload.get("archiveRowCount"),
        "archiveObjectCount": payload.get("archiveObjectCount") if "archiveObjectCount" in payload else len(archive_objects),
        "gapRangeCount": len(gap_ranges),
        "gapMissingCount": sum_gap_missing_count(gap_ranges),
        "firstGapRange": first_range(gap_ranges),
        "lastGapRange": last_range(gap_ranges),
        "fetchRangeCount": len(fetch_ranges),
        "firstFetchRange": first_range(fetch_ranges),
        "lastFetchRange": last_range(fetch_ranges),
        "partialHistoryBoundary": payload.get("partialHistoryBoundary"),
        "noDataBefore": payload.get("noDataBefore"),
        "emptyRange": payload.get("emptyRange"),
        "reason": payload.get("reason"),
        "materializedSource": payload.get("materializedSource"),
    }


def first_range(ranges):
    return ranges[0] if ranges else None


def last_range(ranges):
    return ranges[-1] if ranges else None


def sum_gap_missing_count(ranges):
    total = 0
    known = False
    for item in ranges:
        if not isinstance(item, dict):
            continue
        value = item.get("missingCount")
        if isinstance(value, int):
            total += value
            known = True
    return total if known else None


if __name__ == "__main__":
    main()
