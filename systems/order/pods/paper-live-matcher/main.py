"""Match persistent paper limit orders against normalized realtime quotes."""

from __future__ import annotations

import os
import time
import traceback

import redis
from kafka import TopicPartition
from kafka.structs import OffsetAndMetadata

from market_data.common.env import load_dotenv
from market_data.common.kafka_io import create_json_consumer
from market_data.realtime.subscription_cohorts import RealtimeSubscriptionCohortService
from kis_trader.paper.postgres import PostgresPaperTradingRepository
from kis_trader.paper.matcher import match_quote_payload
from kis_trader.runtime_heartbeat import touch_heartbeat


def main() -> int:
    load_dotenv()
    repository = PostgresPaperTradingRepository.from_env()
    subscription_service = _subscription_service()
    _sync_subscriptions(repository, subscription_service)

    topic = os.getenv("PAPER_ORDER_QUOTES_TOPIC", os.getenv("KAFKA_QUOTES_LAYER_TOPIC", "market.layer.quotes.v1"))
    consumer = create_json_consumer(
        [topic],
        os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
        os.getenv("PAPER_ORDER_MATCHER_GROUP_ID", "gops-paper-live-matcher-v1"),
        os.getenv("PAPER_ORDER_MATCHER_CLIENT_ID", "gops-paper-live-matcher"),
        enable_auto_commit=False,
        max_poll_records=os.getenv("PAPER_ORDER_MATCHER_MAX_POLL_RECORDS", "100"),
    )
    subscription_sync_seconds = max(1.0, float(os.getenv("PAPER_SUBSCRIPTION_SYNC_SECONDS", "5")))
    next_subscription_sync = time.monotonic() + subscription_sync_seconds
    touch_heartbeat()
    try:
        while True:
            if time.monotonic() >= next_subscription_sync:
                _sync_subscriptions(repository, subscription_service)
                next_subscription_sync = time.monotonic() + subscription_sync_seconds
            batches = consumer.poll(timeout_ms=1000, max_records=100)
            if not batches:
                touch_heartbeat()
                continue
            for partition, messages in batches.items():
                for message in messages:
                    try:
                        matched = match_quote_message(repository, message.value, message)
                        if matched:
                            print(
                                f"paper orders filled symbol={message.value.get('symbol')} count={len(matched)}",
                                flush=True,
                            )
                            _sync_subscriptions(repository, subscription_service)
                            next_subscription_sync = time.monotonic() + subscription_sync_seconds
                        consumer.commit({
                            TopicPartition(message.topic, message.partition): OffsetAndMetadata(message.offset + 1, "")
                        })
                    except Exception:
                        traceback.print_exc()
                        consumer.seek(partition, message.offset)
                        break
                touch_heartbeat()
    finally:
        consumer.close()


def match_quote_message(repository, payload, message=None):
    fallback_event_id = (
        f"{message.topic}/{message.partition}/{message.offset}" if message is not None else None
    )
    return match_quote_payload(repository, payload, fallback_event_id=fallback_event_id)


def _subscription_service() -> RealtimeSubscriptionCohortService:
    redis_url = os.environ["REDIS_URL"]
    client = redis.from_url(redis_url, decode_responses=True)
    return RealtimeSubscriptionCohortService(client, auto_reconcile=False)


def _sync_subscriptions(repository, service: RealtimeSubscriptionCohortService) -> None:
    service.replace_paper_order_source(repository.active_order_symbols())
    service.replace_paper_portfolio_source(repository.active_position_symbols())


if __name__ == "__main__":
    raise SystemExit(main())
