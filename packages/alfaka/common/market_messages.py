# 역할: Alpaca 원본 메시지를 Kafka Raw Envelope와 topic 이름으로 변환합니다.
# 사용: 수집기는 이 규칙으로 bars/updatedBars/trades를 Raw Topic에 넣습니다.
# 결과: Flink/stream processor가 같은 envelope 계약을 읽습니다.
from alfaka.common.env import utc_now_iso


MESSAGE_TYPE_TO_CHANNEL = {
    "b": "bars",
    "u": "updatedBars",
    "t": "trades",
    "q": "quotes",
    "d": "dailyBars",
    "s": "statuses",
    "l": "lulds",
    "c": "corrections",
    "x": "cancelErrors",
}

MESSAGE_TYPE_TO_RAW_TOPIC_SUFFIX = {
    "b": "bars",
    "u": "updated-bars",
    "t": "trades",
    "q": "quotes",
    "d": "daily-bars",
    "s": "statuses",
    "l": "lulds",
    "c": "corrections",
    "x": "cancel-errors",
}

CONTROL_MESSAGE_TYPES = {"success", "subscription", "error"}


def build_raw_envelope(message, feed):
    message_type = message.get("T")
    channel = MESSAGE_TYPE_TO_CHANNEL.get(message_type, "unknown")
    symbol = message.get("S", "UNKNOWN")

    return {
        "source": "alpaca",
        "feed": feed,
        "channel": channel,
        "symbol": symbol,
        "eventTime": message.get("t"),
        "receivedAt": utc_now_iso(),
        "raw": message,
    }


def raw_topic_name(prefix, message_type):
    topic_suffix = MESSAGE_TYPE_TO_RAW_TOPIC_SUFFIX.get(message_type, "unknown")
    return f"{prefix}.{topic_suffix}"
