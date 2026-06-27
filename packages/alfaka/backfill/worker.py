import json
import os
import time

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
        request_id = store.pop_queued_request_id(timeout=poll_seconds)
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
