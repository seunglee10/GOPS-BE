from __future__ import annotations

import os

from alfaka.common.kafka_io import create_json_consumer
from gops_agents.chart_assets.builder import ChartAssetBuilder
from gops_agents.chart_assets.queue import DEFAULT_TOPIC


def run() -> None:
    consumer = create_json_consumer(
        [os.getenv("CHART_ASSET_BUILD_REQUESTS_TOPIC", DEFAULT_TOPIC)],
        os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
        "gops-chart-asset-builder",
        "gops-chart-asset-builder",
        enable_auto_commit=False,
        max_poll_records=1,
        max_poll_interval_ms=os.getenv("CHART_ASSET_BUILD_MAX_POLL_INTERVAL_MS", "7200000"),
    )
    builder = ChartAssetBuilder()
    try:
        for message in consumer:
            builder.process_message(message.value)
            consumer.commit()
    finally:
        consumer.close()


if __name__ == "__main__":
    run()
