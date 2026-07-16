from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

AGENT_ALERTS_CHANNEL = "agent.alerts"
RISK_LOG_MAX_ENTRIES = 500
RISK_LOG_TTL_SECONDS = 7 * 24 * 3600
EVENT_DEDUPE_TTL_SECONDS = 24 * 3600
MARKET_EVENTS_TOPIC = "agents.market-events.v1"


def notification_payload(decision: dict[str, Any], *, source_topic: str | None = None) -> dict[str, Any]:
    level = decision.get("level") or decision.get("severity")
    explicit_show_toast = decision.get("showToast")
    show_toast = False if source_topic == MARKET_EVENTS_TOPIC else (
        bool(explicit_show_toast)
        if "showToast" in decision
        else str(level or "").lower() in {"watch", "alert", "critical"}
    )
    return {
        "type": "AGENT_ALERT",
        "decision": decision,
        "symbol": decision.get("symbol"),
        "level": level,
        "showToast": show_toast,
        "sourceTopic": source_topic,
    }


def risk_log_key(day: str, channel: str = AGENT_ALERTS_CHANNEL) -> str:
    return f"{channel}:log:{day}"


class RedisNotificationPublisher:
    def __init__(self, redis_client, channel: str = AGENT_ALERTS_CHANNEL):
        self.redis = redis_client
        self.channel = channel

    def publish(self, decision: dict[str, Any], *, source_topic: str | None = None) -> dict[str, Any]:
        payload = notification_payload(decision, source_topic=source_topic)
        event_id = str(decision.get("eventId") or "").strip()
        if event_id and not self._claim_event(event_id):
            return {**payload, "duplicate": True}
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        symbol = decision.get("symbol") or "UNKNOWN"
        self.redis.publish(self.channel, encoded)
        self.redis.publish(f"{self.channel}:{symbol}", encoded)
        self.redis.setex(f"{self.channel}:latest:{symbol}", 3600, encoded)
        self._append_risk_log(decision)
        return payload

    def _claim_event(self, event_id: str) -> bool:
        key = f"{self.channel}:dedupe:{event_id}"
        try:
            return bool(self.redis.set(key, "1", nx=True, ex=EVENT_DEDUPE_TTL_SECONDS))
        except (AttributeError, TypeError):
            try:
                if hasattr(self.redis, "setnx"):
                    claimed = bool(self.redis.setnx(key, "1"))
                    if claimed:
                        self.redis.expire(key, EVENT_DEDUPE_TTL_SECONDS)
                    return claimed
                if self.redis.get(key):
                    return False
                self.redis.setex(key, EVENT_DEDUPE_TTL_SECONDS, "1")
                return True
            except Exception:
                return True
        except Exception:
            # Deduplication is a guardrail. Redis publish availability remains
            # the final delivery gate if the guard cannot be written.
            return True

    def _append_risk_log(self, decision: dict[str, Any]) -> None:
        """일별 리스크 이벤트 로그 — 장마감 리포트의 재료 (capped list, 7일 보존)."""
        event_type = str(decision.get("eventType") or "")
        if not event_type.startswith("risk_"):
            return
        day = str(decision.get("observedAt") or "")[:10]
        if len(day) != 10:
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = risk_log_key(day, self.channel)
        try:
            encoded = json.dumps(decision, ensure_ascii=False, separators=(",", ":"))
            self.redis.lpush(key, encoded)
            self.redis.ltrim(key, 0, RISK_LOG_MAX_ENTRIES - 1)
            self.redis.expire(key, RISK_LOG_TTL_SECONDS)
        except Exception:
            # The daily log is best-effort; never break alert delivery for it.
            return
