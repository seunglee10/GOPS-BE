import json
import os
import time

import redis

from alfaka.backfill.runner import BackfillRunner
from alfaka.backfill.status import RedisBackfillStore
from alfaka.common.env import load_dotenv


def main():
    load_dotenv()
    store = RedisBackfillStore()
    runner = BackfillRunner(store=store)
    run_once = os.getenv("BACKFILL_WORKER_ONCE", "false").lower() in {"1", "true", "yes"}
    poll_seconds = int(os.getenv("BACKFILL_WORKER_POLL_SECONDS", "5"))

    while True:
        try:
            request_id = store.pop_queued_request_id(timeout=poll_seconds)
        except redis.exceptions.RedisError as exc:
            if run_once:
                raise
            print(f"Backfill worker waiting for Redis: {exc}", flush=True)
            time.sleep(poll_seconds)
            continue
        if not request_id:
            if run_once:
                return
            continue
        record = store.get_status(request_id)
        if not record:
            continue
        print(f"Backfill worker processing {request_id}", flush=True)
        result = runner.run(record)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if run_once:
            return
        time.sleep(0)


if __name__ == "__main__":
    main()
