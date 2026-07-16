# 역할: Alpaca 메시지를 Kafka input envelope와 topic 이름으로 변환합니다.
# 사용: 수집기는 이 규칙으로 market.input.realtime.* topic에 넣습니다.
# 결과: Python/Kubernetes stream processor가 같은 envelope 계약을 읽습니다.
import hashlib
import json

from alfaka.common.env import utc_now_iso
from alfaka.alpaca.feed_profiles import market_session_for_timestamp
from alfaka.common.canonical import LIVE_PRICE_ADJUSTMENT, candle_metadata
from alfaka.common.symbols import is_crypto_symbol, normalize_provider_symbol


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
    "b": "bars.1m",
    "u": "updated-bars.1m",
    "t": "trades",
    "q": "quotes",
    "d": "daily-bars",
    "s": "events",
    "l": "events",
    "c": "events",
    "x": "events",
}

CONTROL_MESSAGE_TYPES = {"success", "subscription", "error"}


def build_raw_envelope(message, feed, feed_profile=None, market_session=None):
    """Alpaca 원본 메시지를 GOPS 내부 raw envelope 계약으로 감쌉니다."""
    message_type = message.get("T")
    channel = MESSAGE_TYPE_TO_CHANNEL.get(message_type, "unknown")
    received_at = utc_now_iso()
    provider_symbol = message.get("S") or "_MARKET"
    symbol = normalize_provider_symbol(provider_symbol)
    event_time = message.get("t")
    asset_class = "crypto" if is_crypto_symbol(symbol) else "us_equity"
    raw_simulation = message.get("simulator")
    simulation = (
        dict(raw_simulation)
        if isinstance(raw_simulation, dict) and raw_simulation.get("source") == "gops-simulator"
        else None
    )
    simulation_session = simulation.get("marketSession") if simulation else None
    resolved_session = market_session or simulation_session or ("crypto" if asset_class == "crypto" else "regular" if channel == "dailyBars" else market_session_for_timestamp(event_time or received_at))

    envelope = {
        "source": "alpaca",
        "feed": feed,
        "feedProfile": feed_profile or feed,
        "marketSession": resolved_session,
        "channel": channel,
        "symbol": symbol,
        "assetClass": asset_class,
        "eventTime": event_time,
        "receivedAt": received_at,
        "sourceEventId": source_event_id(message, feed, channel, symbol, received_at),
        "raw": message,
    }
    if provider_symbol != symbol:
        envelope["providerSymbol"] = provider_symbol
    if simulation:
        envelope["simulation"] = simulation
    if channel in {"bars", "updatedBars", "dailyBars"}:
        envelope.update(candle_metadata(LIVE_PRICE_ADJUSTMENT))
    return envelope


def raw_topic_name(prefix, message_type):
    """Alpaca 메시지 타입에 맞는 raw Kafka topic 이름을 만듭니다."""
    normalized_prefix = (prefix or "market.input").rstrip(".")
    topic_suffix = MESSAGE_TYPE_TO_RAW_TOPIC_SUFFIX.get(message_type, "unknown")
    if normalized_prefix == "market.input":
        return f"{normalized_prefix}.realtime.{topic_suffix}.v1"
    return f"{normalized_prefix}.{topic_suffix}"


def source_event_id(message, feed, channel, symbol, received_at):
    """중복 적재를 줄이기 위해 원본 이벤트를 식별할 안정적인 ID를 만듭니다."""
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
    """고유 ID가 없는 상태 메시지에 붙일 짧은 payload 해시를 계산합니다."""
    payload = json.dumps(message, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
