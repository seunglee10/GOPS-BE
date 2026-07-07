# 역할: market-data Kafka topic 이름 규칙을 한 곳에서 관리합니다.
# 사용: processor, S3 sink, ClickHouse loader가 interval별 candle topic 계약을 공유합니다.
import os


CLOSED_CANDLE_INTERVALS = ("1m", "5m", "10m", "1h", "4h", "1D", "1W", "1M")

_CLOSED_CANDLE_TOPIC_SUFFIX = {
    "1m": "1m",
    "5m": "5m",
    "10m": "10m",
    "1h": "1h",
    "4h": "4h",
    "1D": "1d",
    "1W": "1w",
    "1M": "1mo",
}

_CLOSED_CANDLE_TOPIC_ENV = {
    "1m": "KAFKA_CLOSED_CANDLE_1M_TOPIC",
    "5m": "KAFKA_CLOSED_CANDLE_5M_TOPIC",
    "10m": "KAFKA_CLOSED_CANDLE_10M_TOPIC",
    "1h": "KAFKA_CLOSED_CANDLE_1H_TOPIC",
    "4h": "KAFKA_CLOSED_CANDLE_4H_TOPIC",
    "1D": "KAFKA_CLOSED_CANDLE_1D_TOPIC",
    "1W": "KAFKA_CLOSED_CANDLE_1W_TOPIC",
    "1M": "KAFKA_CLOSED_CANDLE_1MO_TOPIC",
}


def default_closed_candle_topics():
    return {
        interval: f"market.layer.candles.{suffix}.closed.v1"
        for interval, suffix in _CLOSED_CANDLE_TOPIC_SUFFIX.items()
    }


def closed_candle_topics_from_env(environ=None):
    environ = os.environ if environ is None else environ
    defaults = default_closed_candle_topics()
    if any(name in environ for name in _CLOSED_CANDLE_TOPIC_ENV.values()):
        return {
            interval: environ.get(_CLOSED_CANDLE_TOPIC_ENV[interval], defaults[interval])
            for interval in CLOSED_CANDLE_INTERVALS
        }
    legacy_topic = environ.get("KAFKA_CLOSED_CANDLE_TOPIC")
    if legacy_topic:
        return legacy_topic
    return defaults


def closed_candle_topic_values(environ=None):
    topics = closed_candle_topics_from_env(environ)
    if isinstance(topics, str):
        return [topics]
    return list(dict.fromkeys(topics[interval] for interval in CLOSED_CANDLE_INTERVALS if topics.get(interval)))
