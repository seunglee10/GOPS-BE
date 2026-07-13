from __future__ import annotations

import logging
import os
import socket
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from gops_agents.chart_assets.builder import ChartAssetBuilder
from gops_agents.chart_assets.queue import build_chart_asset_queue_from_env


LOGGER = logging.getLogger(__name__)


def run() -> None:
    concurrency = max(1, int(os.getenv("CHART_ASSET_BUILD_CONCURRENCY", "2")))
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    queue = build_chart_asset_queue_from_env()
    builder = ChartAssetBuilder(concurrency=concurrency)
    futures = set()
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="chart-geometry") as executor:
        while True:
            while len(futures) < concurrency:
                claim = queue.claim_next(worker_id, lease_seconds=900)
                if claim is None:
                    break
                futures.add(executor.submit(process_claim, builder, claim))
            if not futures:
                time.sleep(1.0)
                continue
            completed, futures = wait(futures, timeout=1.0, return_when=FIRST_COMPLETED)
            for future in completed:
                try:
                    future.result()
                except Exception:
                    LOGGER.exception("chart geometry build item failed outside the item boundary")


def process_claim(builder: ChartAssetBuilder, claim: dict) -> dict:
    return builder.run_item(claim["envelope"], claim["symbol"], claim["interval"])


if __name__ == "__main__":
    run()
