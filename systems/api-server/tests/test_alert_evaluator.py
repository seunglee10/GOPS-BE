from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
ORDER_SHARED = ROOT / "systems" / "order" / "shared"
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend"
for path in (str(MARKET_SHARED), str(ORDER_SHARED), str(BACKEND), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

try:
    from app.alerts.evaluator import AlertEvaluator, AlertOutboxSender, _entries_from_xautoclaim
    from app.alerts.notifications import notification_delivery_decision, notification_setting_for_item
    from app.alerts.preferences import preference_response
except Exception as exc:  # pragma: no cover - dependency guard for lean envs
    pytest.skip(f"alert evaluator tests are unavailable: {exc}", allow_module_level=True)


class FakeProjection:
    def __init__(self, alerts):
        self.alerts = alerts
        self.last = {}
        self.seen = set()
        self.deleted = []
        self.upserted = []

    def replace_all(self, active_alerts):
        self.alerts = active_alerts

    def last_price(self, symbol):
        return self.last.get(symbol)

    def set_last_price(self, symbol, price):
        self.last[symbol] = price

    def price_cross_candidates(self, symbol, low, high):
        return [
            alert
            for alert in self.alerts
            if alert["symbol"] == symbol and low <= float(alert["target_price"]) <= high
        ]

    def remember_price(self, symbol, price, timestamp_ms, *, retention_ms):
        self.remembered = (symbol, price, timestamp_ms, retention_ms)

    def spike_alerts(self, symbol):
        return []

    def mark_event_seen(self, event_id, ttl_seconds):
        if event_id in self.seen:
            return False
        self.seen.add(event_id)
        return True

    def delete_alert(self, alert_id, *, symbol=None):
        self.deleted.append((alert_id, symbol))

    def upsert_alert(self, alert):
        self.upserted.append(alert)


class FakeRepository:
    def __init__(self, trigger_result=None, alert=None):
        self.trigger_result = trigger_result
        self.alert = alert
        self.trigger_records = []
        self.notifications = []

    def active_alerts(self):
        return []

    def record_alert_trigger(self, user_sub, alert_id):
        self.trigger_records.append((user_sub, alert_id))
        if self.trigger_result is not None:
            return self.trigger_result
        return {"id": alert_id, "user_sub": user_sub, "symbol": "NVDA", "status": "fired"}

    def create_notification(self, **kwargs):
        self.notifications.append(kwargs)
        return {"id": 1, **kwargs}

    def get_alert(self, user_sub, alert_id):
        return self.alert


class FakeOutbox:
    def __init__(self):
        self.events = []

    def enqueue(self, payload):
        self.events.append(payload)
        return "1-0"


class FakePreferenceRepository:
    def __init__(self, row):
        self.row = row

    def get(self, user_sub):
        return self.row


def test_price_cross_event_prefers_source_event_id_and_fires_once() -> None:
    alert = {
        "id": 7,
        "user_sub": "user-1",
        "symbol": "NVDA",
        "type": "price_cross",
        "direction": "above",
        "target_price": 105,
        "repeat": False,
        "repeat_limit": 1,
        "triggered_count": 0,
    }
    projection = FakeProjection([alert])
    projection.last["NVDA"] = 100
    repo = FakeRepository()
    outbox = FakeOutbox()
    evaluator = AlertEvaluator(repository=repo, projection=projection, outbox=outbox)

    events = evaluator.process_trade(
        {
            "symbol": "NVDA",
            "price": 106,
            "timestamp": "2026-07-06T01:00:00Z",
            "sourceEventId": "source-1",
            "tradeId": "trade-1",
        }
    )
    duplicate = evaluator.process_trade(
        {
            "symbol": "NVDA",
            "price": 106.5,
            "timestamp": "2026-07-06T01:00:01Z",
            "sourceEventId": "source-1",
            "tradeId": "trade-1",
        }
    )

    assert events[0]["eventId"] == "7:source-1:above"
    assert outbox.events[0]["eventId"] == "7:source-1:above"
    assert repo.trigger_records == [("user-1", 7)]
    assert projection.deleted == [(7, "NVDA")]
    assert duplicate == []


def test_repeat_limited_alert_stays_active_until_limit_is_reached() -> None:
    alert = {
        "id": 9,
        "user_sub": "user-1",
        "symbol": "NVDA",
        "type": "price_cross",
        "direction": "above",
        "target_price": 105,
        "repeat": True,
        "repeat_limit": 3,
        "triggered_count": 0,
    }
    updated_alert = {**alert, "triggered_count": 1, "status": "active"}
    projection = FakeProjection([alert])
    projection.last["NVDA"] = 100
    repo = FakeRepository(trigger_result=updated_alert)
    evaluator = AlertEvaluator(repository=repo, projection=projection, outbox=FakeOutbox())

    events = evaluator.process_trade(
        {
            "symbol": "NVDA",
            "price": 106,
            "timestamp": "2026-07-06T01:00:00Z",
            "sourceEventId": "source-2",
        }
    )

    assert events[0]["eventId"] == "9:source-2:above"
    assert repo.trigger_records == [("user-1", 9)]
    assert projection.upserted == [updated_alert]
    assert projection.deleted == []


def test_first_trade_seeds_last_price_without_triggering() -> None:
    projection = FakeProjection(
        [
            {
                "id": 1,
                "user_sub": "user-1",
                "symbol": "AAPL",
                "type": "price_cross",
                "direction": "above",
                "target_price": 105,
                "repeat": False,
            }
        ]
    )
    evaluator = AlertEvaluator(repository=FakeRepository(), projection=projection, outbox=FakeOutbox())

    events = evaluator.process_trade({"symbol": "AAPL", "price": 106, "timestamp": "2026-07-06T01:00:00Z"})

    assert events == []
    assert projection.last["AAPL"] == 106


def test_spike_evaluator_applies_user_rapid_move_threshold_before_firing() -> None:
    alert = {
        "id": 12,
        "user_sub": "user-1",
        "symbol": "NVDA",
        "type": "spike",
        "direction": None,
        "change_pct": 5,
        "window_min": 10,
        "repeat": False,
        "repeat_limit": 1,
        "triggered_count": 0,
    }

    class SpikeProjection(FakeProjection):
        def price_cross_candidates(self, symbol, low, high):
            return []

        def spike_alerts(self, symbol):
            return [alert]

        def baseline_price(self, symbol, timestamp_ms):
            return {"price": 100}

    preferences = FakePreferenceRepository({
        "settings": {
            "settings": {"master": True, "rapidMove": True},
            "thresholds": {"rapidMovePct": 10},
        },
        "company_overrides": {},
    })
    projection = SpikeProjection([alert])
    repo = FakeRepository()
    outbox = FakeOutbox()
    evaluator = AlertEvaluator(
        repository=repo,
        projection=projection,
        outbox=outbox,
        preference_repository=preferences,
    )

    below = evaluator.process_trade({
        "symbol": "NVDA",
        "price": 105,
        "timestamp": "2026-07-06T01:00:00Z",
        "sourceEventId": "below-preference",
    })
    reached = evaluator.process_trade({
        "symbol": "NVDA",
        "price": 110,
        "timestamp": "2026-07-06T01:01:00Z",
        "sourceEventId": "at-preference",
    })

    assert below == []
    assert reached[0]["changePct"] == pytest.approx(10)
    assert reached[0]["thresholdPct"] == 10
    assert reached[0]["alertThresholdPct"] == 5
    assert reached[0]["preferenceThresholdPct"] == 10
    assert repo.trigger_records == [("user-1", 12)]


def test_outbox_sender_publishes_even_when_notification_event_is_duplicate() -> None:
    class Broker:
        def __init__(self):
            self.published = []

        def publish_user(self, user_sub, payload):
            self.published.append((user_sub, payload))

    class Producer:
        def __init__(self):
            self.sent = []

        def send(self, topic, key, value):
            self.sent.append((topic, key, value))
            return None

    repo = FakeRepository()
    broker = Broker()
    producer = Producer()
    sender = AlertOutboxSender(
        redis_client=None,
        repository=repo,
        broker=broker,
        producer=producer,
        triggered_topic="alerts.triggered.v1",
        stream="alerts:outbox",
        group="group",
        consumer_name="consumer",
    )
    payload = {
        "eventId": "event-1",
        "type": "alert.price_cross",
        "alertId": 1,
        "userSub": "user-1",
        "symbol": "NVDA",
    }

    sender.deliver(payload)
    sender.deliver(payload)

    assert len(repo.notifications) == 2
    assert len(broker.published) == 2
    assert len(producer.sent) == 2


def test_outbox_sender_skips_user_notification_but_keeps_execution_event_when_disabled() -> None:
    class Broker:
        def __init__(self):
            self.published = []

        def publish_user(self, user_sub, payload):
            self.published.append((user_sub, payload))

    class Producer:
        def __init__(self):
            self.sent = []

        def send(self, topic, key, value):
            self.sent.append((topic, key, value))
            return None

    repo = FakeRepository(alert={"id": 1, "notifications_enabled": False})
    broker = Broker()
    producer = Producer()
    sender = AlertOutboxSender(
        redis_client=None,
        repository=repo,
        broker=broker,
        producer=producer,
        triggered_topic="alerts.triggered.v1",
        stream="alerts:outbox",
        group="group",
        consumer_name="consumer",
    )
    payload = {
        "eventId": "event-no-notification",
        "type": "alert.price_cross",
        "alertId": 1,
        "userSub": "user-1",
        "symbol": "NVDA",
    }

    result = sender.deliver(payload)

    assert result["reason"] == "notifications_disabled"
    assert repo.notifications == []
    assert broker.published == []
    assert len(producer.sent) == 1


def test_outbox_sender_applies_user_rapid_move_threshold() -> None:
    class Broker:
        def __init__(self):
            self.published = []

        def publish_user(self, user_sub, payload):
            self.published.append((user_sub, payload))

    class Producer:
        def __init__(self):
            self.sent = []

        def send(self, topic, key, value):
            self.sent.append((topic, key, value))
            return None

    preferences = FakePreferenceRepository({
        "settings": {
            "settings": {"master": True, "rapidMove": True},
            "thresholds": {"rapidMovePct": 10},
        },
        "company_overrides": {},
    })
    repo = FakeRepository()
    broker = Broker()
    producer = Producer()
    sender = AlertOutboxSender(
        redis_client=None,
        repository=repo,
        broker=broker,
        producer=producer,
        triggered_topic="alerts.triggered.v1",
        stream="alerts:outbox",
        group="group",
        consumer_name="consumer",
        preference_repository=preferences,
    )

    result = sender.deliver({
        "eventId": "event-below-user-threshold",
        "type": "alert.spike",
        "alertId": 1,
        "userSub": "user-1",
        "symbol": "NVDA",
        "changePct": 5,
    })

    assert result["reason"] == "rapid_move_below_threshold"
    assert repo.notifications == []
    assert broker.published == []
    assert len(producer.sent) == 1


@pytest.mark.parametrize(
    ("settings", "company_overrides", "expected_reason"),
    [
        ({"master": False, "targetPrice": True}, {}, "master_disabled"),
        ({"master": True, "targetPrice": True}, {"NVDA": False}, "company_muted"),
    ],
)
def test_notification_preferences_block_target_price_delivery(settings, company_overrides, expected_reason) -> None:
    preferences = preference_response({
        "settings": {"settings": settings, "thresholds": {}},
        "company_overrides": company_overrides,
    })

    allowed, reason = notification_delivery_decision(
        "alert.price_cross",
        {"symbol": "NVDA"},
        preferences,
    )

    assert allowed is False
    assert reason == expected_reason


def test_agent_event_routing_and_volume_thresholds_follow_new_contract() -> None:
    preferences = preference_response({
        "settings": {
            "settings": {"master": True, "volumeSpike": True, "aiAnomaly": True},
            "thresholds": {"volumeSpikeMultiple": 5},
        },
        "company_overrides": {},
    })
    volume_payload = {
        "decision": {"symbol": "NVDA", "eventType": "volume_spike", "metrics": {"multiplier": 3}},
    }

    assert notification_setting_for_item("AGENT_ALERT", {
        "decision": {"eventType": "risk_anomaly_surge"},
    }) == "aiAnomaly"
    assert notification_setting_for_item("AGENT_ALERT", {
        "decision": {"eventType": "earnings"},
    }) is None
    assert notification_delivery_decision("AGENT_ALERT", volume_payload, preferences) == (
        False,
        "volume_spike_below_threshold",
    )
    assert notification_delivery_decision("AGENT_ALERT", {
        "decision": {"symbol": "NVDA", "eventType": "company_news"},
    }, preferences) == (False, "event_excluded")


def test_outbox_sender_ensures_group_once_after_busygroup() -> None:
    class Redis:
        def __init__(self):
            self.calls = 0

        def xgroup_create(self, stream, group, id="0", mkstream=False):
            self.calls += 1
            raise Exception("BUSYGROUP Consumer Group name already exists")

    redis = Redis()
    sender = AlertOutboxSender(
        redis_client=redis,
        repository=FakeRepository(),
        broker=object(),
        producer=object(),
        triggered_topic="alerts.triggered.v1",
        stream="alerts:outbox",
        group="group",
        consumer_name="consumer",
    )

    sender.ensure_group()
    sender.ensure_group()

    assert redis.calls == 1


def test_xautoclaim_entries_are_read_from_redis_py_list_response() -> None:
    entries = [("1-0", {"payload": "{}"})]

    assert _entries_from_xautoclaim(["0-0", entries, []]) == entries
