# 역할: Alpaca 원본 메시지를 Kafka Raw Envelope와 topic 이름으로 변환합니다.
# 사용: 수집기는 이 규칙으로 bars/updatedBars/trades를 Raw Topic에 넣습니다.
# 결과: Flink/stream processor가 같은 envelope 계약을 읽습니다.
import hashlib
import json

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
    received_at = utc_now_iso()
    symbol = message.get("S") or "_MARKET"

    return {
        "source": "alpaca",
        "feed": feed,
        "channel": channel,
        "symbol": symbol,
        "eventTime": message.get("t"),
        "receivedAt": received_at,
        "sourceEventId": source_event_id(message, feed, channel, symbol, received_at),
        "raw": message,
    }


def raw_topic_name(prefix, message_type):
    topic_suffix = MESSAGE_TYPE_TO_RAW_TOPIC_SUFFIX.get(message_type, "unknown")
    return f"{prefix}.{topic_suffix}"


def source_event_id(message, feed, channel, symbol, received_at):
    event_time = message.get("t") or received_at
    if channel == "trades":
        return f"alpaca/{feed}/trades/{symbol}/{message.get('i', 'no-id')}/{event_time}"
    if channel == "quotes":
        return f"alpaca/{feed}/quotes/{symbol}/{event_time}"
    if channel == "bars":
        return f"alpaca/{feed}/bars/{symbol}/{event_time}"
    if channel == "updatedBars":
        return f"alpaca/{feed}/updatedBars/{symbol}/{event_time}"
    if channel == "dailyBars":
        return f"alpaca/{feed}/dailyBars/{symbol}/{event_time}"
    if channel == "statuses":
        event_time = message.get("t") or f"payload-{payload_digest(message)}"
        status_id = message.get("sc") or message.get("status") or message.get("msg") or "status"
        return f"alpaca/{feed}/statuses/{symbol}/{event_time}/{status_id}"
    if channel == "corrections":
        return f"alpaca/{feed}/corrections/{symbol}/{message.get('i') or event_time}"
    if channel == "cancelErrors":
        return f"alpaca/{feed}/cancelErrors/{symbol}/{message.get('i') or event_time}"
    return f"alpaca/{feed}/{channel}/{symbol}/{event_time}"


def payload_digest(message):
    payload = json.dumps(message, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
