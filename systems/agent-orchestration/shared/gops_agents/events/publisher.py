from __future__ import annotations

import json
from typing import Any

AGENT_ALERTS_CHANNEL = "agent.alerts"


def notification_payload(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "AGENT_ALERT",
        "decision": decision,
        "symbol": decision.get("symbol"),
        "level": decision.get("level"),
        "showToast": decision.get("showToast", False),
    }


class RedisNotificationPublisher:
    def __init__(self, redis_client, channel: str = AGENT_ALERTS_CHANNEL):
        self.redis = redis_client
        self.channel = channel

    def publish(self, decision: dict[str, Any]) -> dict[str, Any]:
        payload = notification_payload(decision)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        symbol = decision.get("symbol") or "UNKNOWN"
        self.redis.publish(self.channel, encoded)
        self.redis.publish(f"{self.channel}:{symbol}", encoded)
        self.redis.setex(f"{self.channel}:latest:{symbol}", 3600, encoded)
        return payload
