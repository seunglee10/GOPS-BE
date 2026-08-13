from __future__ import annotations

import json
import os
import sys
import time
import traceback
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import redis
except Exception:  # pragma: no cover - dependency guard for lean test envs
    redis = None  # type: ignore[assignment]

from app.alerts.notifications import RedisNotificationBroker, notification_delivery_decision
from app.alerts.preferences import PostgresNotificationPreferenceRepository, preference_response
from app.alerts.projection import RedisAlertProjection
from app.alerts.repository import PostgresAlertRepository
from app.market_data.calendar.service import market_session_bounds, parse_datetime
from app.market_data.heatmap.service import get_heatmap_service


DEFAULT_INPUT_TOPIC = "market.layer.trades.v1"
DEFAULT_TRIGGERED_TOPIC = "alerts.triggered.v1"
DEFAULT_DLQ_TOPIC = "alerts.dlq.v1"
DEFAULT_GROUP_ID = "gops-alert-evaluator"
DEFAULT_OUTBOX_STREAM = "alerts:outbox"
DEFAULT_OUTBOX_GROUP = "gops-alert-outbox-senders"
DEFAULT_SPIKE_RETENTION_MINUTES = 240
DEFAULT_DEDUPE_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_MARKET_MOVE_FRESHNESS_SECONDS = 90


def _add_alfaka_package_path() -> None:
    candidates = [os.getenv("ALFAKA_PACKAGES_PATH"), "/app/systems/market-data/shared"]
    current_file = Path(__file__).resolve()
    candidates.extend(str(parent / "systems" / "market-data" / "shared") for parent in current_file.parents)
    for candidate in candidates:
        if not candidate:
            continue
        package_path = Path(candidate)
        if (package_path / "alfaka").exists() and str(package_path) not in sys.path:
            sys.path.insert(0, str(package_path))
            return


_add_alfaka_package_path()

from market_data.common.kafka_io import create_json_consumer, create_json_producer  # noqa: E402
from market_data.common.redis_keys import RedisKeyBuilder  # noqa: E402
from market_data.common.symbols import normalize_market_symbol  # noqa: E402
from market_data.serving.indicators import rsi  # noqa: E402
from market_data.serving.clickhouse_provider import ClickHouseMarketDataProvider  # noqa: E402


DEFAULT_INPUT_TOPICS = (
    "market.layer.trades.v1",
    "market.layer.candles.1m.closed.v1",
    "market.layer.candles.5m.closed.v1",
    "market.layer.candles.10m.closed.v1",
    "market.layer.candles.1h.closed.v1",
    "market.layer.candles.4h.closed.v1",
    "market.layer.candles.1d.closed.v1",
)


class AlertEvaluator:
    def __init__(
        self,
        *,
        repository: Any,
        projection: RedisAlertProjection,
        outbox: "AlertRedisOutbox",
        preference_repository: Any | None = None,
        dedupe_ttl_seconds: int = DEFAULT_DEDUPE_TTL_SECONDS,
        spike_retention_minutes: int = DEFAULT_SPIKE_RETENTION_MINUTES,
        reminder_dispatcher: "ReminderDispatcher | None" = None,
        history_loader: Any | None = None,
    ) -> None:
        self.repository = repository
        self.projection = projection
        self.outbox = outbox
        self.preference_repository = preference_repository
        self.dedupe_ttl_seconds = dedupe_ttl_seconds
        self.spike_retention_ms = spike_retention_minutes * 60 * 1000
        self.reminder_dispatcher = reminder_dispatcher
        self.history_loader = history_loader
        self._warmed_pairs: set[tuple[str, str]] = set()

    def reconcile_active_alerts(self) -> None:
        active_alerts = self.repository.active_alerts()
        self.projection.replace_all(active_alerts)
        if self.history_loader is None:
            return
        pairs = {
            (str(alert["symbol"]), str((alert.get("condition") or {}).get("interval") or "1D"))
            for alert in active_alerts
            if alert.get("type") in {"volume_absolute", "volume_relative", "rsi_threshold"}
        }
        if self.reminder_dispatcher is not None:
            for symbol in self.reminder_dispatcher.watchlist_symbols():
                pairs.update({(symbol, "5m"), (symbol, "1D")})
        for symbol, interval in sorted(pairs - self._warmed_pairs):
            try:
                rows = self.history_loader(symbol, interval, 240)
                for row in rows or []:
                    candle = parse_candle({**row, "symbol": row.get("symbol") or symbol, "interval": interval})
                    if candle is not None:
                        self.projection.remember_candle(candle)
                if rows:
                    self._warmed_pairs.add((symbol, interval))
            except Exception:
                traceback.print_exc()

    def process_message(self, payload: dict[str, Any], topic: str) -> list[dict[str, Any]]:
        if ".candles." in topic and ".closed." in topic:
            return self.process_candle(payload, topic)
        return self.process_trade(payload)

    def process_trade(self, trade: dict[str, Any]) -> list[dict[str, Any]]:
        parsed = parse_trade(trade)
        if parsed is None:
            return []

        symbol = parsed["symbol"]
        price = parsed["price"]
        timestamp_ms = parsed["timestampMs"]
        previous_price = self.projection.last_price(symbol)
        events: list[dict[str, Any]] = []

        if previous_price is not None and previous_price != price:
            low, high = sorted((previous_price, price))
            for alert in self.projection.price_cross_candidates(symbol, low, high):
                event = self._price_cross_event(alert, parsed, previous_price)
                if event:
                    events.append(event)

        self.projection.remember_price(symbol, price, timestamp_ms, retention_ms=self.spike_retention_ms)
        for alert in self.projection.spike_alerts(symbol):
            event = self._spike_event(alert, parsed)
            if event:
                events.append(event)

        self.projection.set_last_price(symbol, price)
        self._enqueue_events(events)
        return events

    def process_candle(self, payload: dict[str, Any], topic: str) -> list[dict[str, Any]]:
        candle = parse_candle(payload, topic)
        if candle is None:
            return []
        candles = self.projection.remember_candle(candle)
        events: list[dict[str, Any]] = []
        for alert in self.projection.metric_alerts(candle["symbol"], candle["interval"]):
            event = self._metric_event(alert, candle, candles)
            if event is not None:
                events.append(event)
        if self.reminder_dispatcher is not None:
            events.extend(self.reminder_dispatcher.events_for_candle(candle, candles))
        self._enqueue_events(events)
        return events

    def _enqueue_events(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            if self.projection.mark_event_seen(event["eventId"], self.dedupe_ttl_seconds):
                self.outbox.enqueue(event)

    def _price_cross_event(
        self,
        alert: dict[str, Any],
        trade: dict[str, Any],
        previous_price: float,
    ) -> dict[str, Any] | None:
        target_price = _float_or_none(alert.get("target_price"))
        if target_price is None:
            return None
        direction = alert.get("direction")
        if direction == "above" and not (previous_price < target_price <= trade["price"]):
            return None
        if direction == "below" and not (previous_price > target_price >= trade["price"]):
            return None
        if direction not in {"above", "below"}:
            return None
        base_event_id = trade.get("sourceEventId") or trade.get("tradeId") or f"{trade['symbol']}:{trade['timestamp']}"
        return {
            "eventId": f"{alert['id']}:{base_event_id}:{direction}",
            "type": "alert.price_cross",
            "alertId": alert["id"],
            "userSub": alert["user_sub"],
            "symbol": trade["symbol"],
            "direction": direction,
            "price": trade["price"],
            "previousPrice": previous_price,
            "targetPrice": target_price,
            "triggeredAt": datetime.now(timezone.utc).isoformat(),
            "sourceEventId": trade.get("sourceEventId"),
            "tradeId": trade.get("tradeId"),
            "alert": alert,
        }


    def _spike_event(self, alert: dict[str, Any], trade: dict[str, Any]) -> dict[str, Any] | None:
        change_pct = _float_or_none(alert.get("change_pct"))
        window_min = _int_or_none(alert.get("window_min"))
        if change_pct is None or window_min is None:
            return None
        baseline = self.projection.baseline_price(trade["symbol"], trade["timestampMs"] - window_min * 60 * 1000)
        baseline_price = _float_or_none(baseline.get("price") if baseline else None)
        if baseline_price is None or baseline_price <= 0:
            return None
        actual_change_pct = ((trade["price"] - baseline_price) / baseline_price) * 100
        effective_change_pct = change_pct
        direction = alert.get("direction")
        satisfied = (
            actual_change_pct >= effective_change_pct
            if direction == "above"
            else actual_change_pct <= -effective_change_pct
            if direction == "below"
            else abs(actual_change_pct) >= effective_change_pct
        )
        previous_state = self.projection.condition_state(alert["id"])
        self.projection.set_condition_state(alert["id"], satisfied)
        if previous_state is None or previous_state is True or not satisfied:
            return None
        resolved_direction = direction or ("above" if actual_change_pct >= 0 else "below")
        base_event_id = trade.get("sourceEventId") or trade.get("tradeId") or f"{trade['symbol']}:{trade['timestamp']}"
        return {
            "eventId": f"{alert['id']}:{base_event_id}:{resolved_direction}:{window_min}m",
            "type": "alert.spike",
            "alertId": alert["id"],
            "userSub": alert["user_sub"],
            "symbol": trade["symbol"],
            "direction": resolved_direction,
            "price": trade["price"],
            "baselinePrice": baseline_price,
            "changePct": actual_change_pct,
            "thresholdPct": effective_change_pct,
            "alertThresholdPct": change_pct,
            "windowMin": window_min,
            "triggeredAt": datetime.now(timezone.utc).isoformat(),
            "sourceEventId": trade.get("sourceEventId"),
            "tradeId": trade.get("tradeId"),
            "alert": alert,
        }

    def _metric_event(
        self,
        alert: dict[str, Any],
        candle: dict[str, Any],
        candles: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        condition = alert.get("condition") if isinstance(alert.get("condition"), dict) else {}
        kind = str(condition.get("kind") or alert.get("type") or "")
        operator = str(condition.get("operator") or alert.get("direction") or "")
        threshold = _float_or_none(condition.get("threshold"))
        if threshold is None:
            return None

        value: float | None = None
        metrics: dict[str, Any] = {"interval": candle["interval"]}
        if kind == "volume_absolute":
            value = _float_or_none(candle.get("volume"))
            metrics["volume"] = value
        elif kind == "volume_relative":
            lookback = max(5, min(_int_or_none(condition.get("lookback")) or 20, 200))
            history = [
                item for item in candles[:-1]
                if _float_or_none(item.get("volume")) is not None
            ][-lookback:]
            if len(history) < lookback:
                return None
            baseline = sum(float(item["volume"]) for item in history) / len(history)
            if baseline <= 0:
                return None
            value = float(candle["volume"]) / baseline
            metrics.update({"volume": candle["volume"], "baselineVolume": baseline, "volumeMultiple": value, "lookback": lookback})
        elif kind == "rsi_threshold":
            period = max(2, min(_int_or_none(condition.get("period")) or 14, 100))
            values = rsi([_float_or_none(item.get("close")) for item in candles], period)
            value = values[-1] if values else None
            metrics.update({"rsi": value, "period": period})
        else:
            return None
        if value is None:
            return None

        satisfied = value >= threshold if operator == "above" else value <= threshold
        previous_state = self.projection.condition_state(alert["id"])
        self.projection.set_condition_state(alert["id"], satisfied)
        if previous_state is None or previous_state is True or not satisfied:
            return None

        event_type = {
            "volume_absolute": "alert.volume_absolute",
            "volume_relative": "alert.volume_relative",
            "rsi_threshold": "alert.rsi_threshold",
        }[kind]
        return {
            "eventId": f"{alert['id']}:candle:{candle['timestamp']}:{kind}:{operator}",
            "type": event_type,
            "alertId": alert["id"],
            "userSub": alert["user_sub"],
            "symbol": candle["symbol"],
            "direction": operator,
            "value": value,
            "threshold": threshold,
            "interval": candle["interval"],
            "metrics": metrics,
            "triggeredAt": datetime.now(timezone.utc).isoformat(),
            "sourceEventId": candle.get("sourceEventId"),
            "alert": alert,
        }


class HeatmapMarketSnapshotLookup:
    """Look up a symbol from the exact cached snapshot used by the heatmap."""

    def __init__(self, heatmap_service: Any, cache_seconds: float = 1.0) -> None:
        self.heatmap_service = heatmap_service
        self.cache_seconds = cache_seconds
        self._loaded_at = 0.0
        self._items: dict[str, dict[str, Any]] = {}

    def __call__(self, symbol: str) -> dict[str, Any] | None:
        now = time.monotonic()
        if not self._items or now - self._loaded_at >= self.cache_seconds:
            snapshot = self.heatmap_service.snapshot("sp500")
            quote_as_of = snapshot.get("quoteAsOf")
            self._items = {
                str(item.get("symbol") or "").upper(): {**item, "quoteAsOf": quote_as_of}
                for item in snapshot.get("items") or []
                if isinstance(item, dict) and item.get("symbol")
            }
            self._loaded_at = now
        return self._items.get(symbol.upper())


class ReminderDispatcher:
    def __init__(
        self,
        *,
        redis_client: Any,
        preference_repository: Any,
        market_snapshot_provider: Any | None = None,
        market_move_mode: str | None = None,
        market_move_allowlist: set[str] | None = None,
        now_provider: Any | None = None,
    ) -> None:
        self.redis = redis_client
        self.preference_repository = preference_repository
        self.keys = RedisKeyBuilder()
        self.market_snapshot_provider = market_snapshot_provider
        configured_mode = market_move_mode or os.getenv("MARKET_MOVE_NOTIFICATION_MODE", "off")
        self.market_move_mode = configured_mode.strip().lower() if configured_mode.strip().lower() in {"off", "shadow", "live"} else "off"
        configured_allowlist = market_move_allowlist
        if configured_allowlist is None:
            configured_allowlist = set(_csv_values(os.getenv("MARKET_MOVE_NOTIFICATION_USER_ALLOWLIST")))
        self.market_move_allowlist = {str(value).strip() for value in configured_allowlist if str(value).strip()}
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def events_for_candle(
        self,
        candle: dict[str, Any],
        candles: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if candle["interval"] == "1m":
            return self._market_move_events(candle["symbol"], self._subscribed_users(candle["symbol"]))
        users = self._watchlist_users(candle["symbol"])
        if candle["interval"] == "5m":
            return self._volume_events(candle, candles, users)
        if candle["interval"] == "1D":
            return self._rsi_events(candle, candles, users)
        return []

    def _market_move_events(self, symbol: str, users: list[str]) -> list[dict[str, Any]]:
        if self.market_move_mode == "off" or self.market_snapshot_provider is None or not users:
            return []
        current = self.now_provider()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        else:
            current = current.astimezone(timezone.utc)
        market_zone = ZoneInfo(os.getenv("MARKET_TIMEZONE") or "America/New_York")
        session = market_session_bounds(current.astimezone(market_zone).date())
        if session is None or not (session["openAt"] <= current <= session["closeAt"]):
            return []

        snapshot = self.market_snapshot_provider(symbol)
        if not isinstance(snapshot, dict):
            return []
        last_price = _float_or_none(snapshot.get("lastPrice"))
        previous_close = _float_or_none(snapshot.get("previousClose"))
        change_percent = _float_or_none(snapshot.get("changePercent"))
        quote_as_of_text = str(snapshot.get("quoteAsOf") or "")
        snapshot_quote_as_of = parse_datetime(quote_as_of_text)
        price_as_of = parse_datetime(snapshot.get("priceUpdatedAt") or quote_as_of_text)
        if (
            last_price is None
            or previous_close is None
            or previous_close <= 0
            or change_percent is None
            or snapshot_quote_as_of is None
            or price_as_of is None
        ):
            return []
        age = current - price_as_of
        freshness_seconds = max(1, int(os.getenv("MARKET_MOVE_QUOTE_FRESHNESS_SECONDS", str(DEFAULT_MARKET_MOVE_FRESHNESS_SECONDS))))
        if age < timedelta(seconds=-5) or age > timedelta(seconds=freshness_seconds):
            return []

        market_date = str(session["marketDate"])
        direction = "up" if change_percent >= 0 else "down"
        events: list[dict[str, Any]] = []
        for user_sub in users:
            preferences = preference_response(self.preference_repository.get(user_sub))
            threshold = _float_or_none(preferences["thresholds"].get("rapidMovePct")) or 5.0
            if abs(change_percent) < threshold or not self._allowed(preferences, "rapidMove", symbol):
                continue
            threshold_key = format(threshold, "g")
            payload = {
                "eventId": f"market-move:{market_date}:{user_sub}:{symbol}:{direction}:{threshold_key}",
                "type": "system.market_move",
                "userSub": user_sub,
                "kind": "market_move",
                "symbol": symbol,
                "direction": direction,
                "thresholdPct": threshold,
                "referenceType": "previous_regular_close",
                "previousClose": previous_close,
                "lastPrice": last_price,
                "changePercent": change_percent,
                "marketDate": market_date,
                "quoteAsOf": quote_as_of_text,
                "effectiveAt": quote_as_of_text,
                "expiresAt": (price_as_of + timedelta(seconds=freshness_seconds)).isoformat(),
                "title": f"{symbol} 정규장 급{'등' if direction == 'up' else '락'}",
                "summary": f"전일 정규장 종가 대비 {change_percent:+.2f}% {'상승' if direction == 'up' else '하락'}했습니다.",
                "triggeredAt": current.isoformat(),
            }
            if self.market_move_mode == "shadow":
                self._remember_shadow(payload)
                continue
            if self.market_move_allowlist and user_sub not in self.market_move_allowlist:
                continue
            events.append(payload)
        return events

    def _remember_shadow(self, payload: dict[str, Any]) -> None:
        key = f"alerts:v1:market-move-shadow:{payload['eventId']}"
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        try:
            self.redis.set(key, encoded, nx=True, ex=24 * 60 * 60)
        except TypeError:
            if self.redis.setnx(key, encoded):
                self.redis.expire(key, 24 * 60 * 60)

    def _volume_events(
        self,
        candle: dict[str, Any],
        candles: list[dict[str, Any]],
        users: list[str],
    ) -> list[dict[str, Any]]:
        history = [
            float(item["volume"])
            for item in candles[:-1]
            if _float_or_none(item.get("volume")) is not None
        ][-20:]
        if len(history) < 20:
            return []
        baseline = sum(history) / len(history)
        if baseline <= 0:
            return []
        multiple = float(candle["volume"]) / baseline
        events = []
        for user_sub in users:
            preferences = preference_response(self.preference_repository.get(user_sub))
            threshold = _float_or_none(preferences["thresholds"].get("volumeSpikeMultiple")) or 3.0
            satisfied = multiple >= threshold
            if not self._condition_entry(user_sub, "volumeSpike", candle["symbol"], satisfied):
                continue
            if not self._allowed(preferences, "volumeSpike", candle["symbol"]):
                continue
            events.append({
                "eventId": f"reminder:volume:{user_sub}:{candle['symbol']}:{candle['timestamp']}",
                "type": "system.volume_spike",
                "userSub": user_sub,
                "symbol": candle["symbol"],
                "kind": "volume_spike",
                "interval": "5m",
                "metrics": {
                    "volume": candle["volume"],
                    "baselineVolume": baseline,
                    "volumeMultiple": multiple,
                    "lookback": 20,
                },
                "title": f"{candle['symbol']} 거래량 이상 급증",
                "summary": f"5분 거래량이 최근 평균의 {multiple:.1f}배입니다.",
                "triggeredAt": datetime.now(timezone.utc).isoformat(),
            })
        return events

    def _condition_entry(self, user_sub: str, setting: str, symbol: str, satisfied: bool) -> bool:
        key = f"alerts:v1:reminder-state:{setting}:{user_sub}:{symbol}"
        previous = self.redis.get(key)
        self.redis.set(key, "1" if satisfied else "0")
        return previous is not None and str(previous) == "0" and satisfied

    def _rsi_events(
        self,
        candle: dict[str, Any],
        candles: list[dict[str, Any]],
        users: list[str],
    ) -> list[dict[str, Any]]:
        values = rsi([_float_or_none(item.get("close")) for item in candles], 14)
        if len(values) < 2 or values[-1] is None or values[-2] is None:
            return []
        current = float(values[-1])
        previous = float(values[-2])
        band = "overbought" if current >= 70 and previous < 70 else "oversold" if current <= 30 and previous > 30 else None
        if band is None:
            return []
        label = "과매수" if band == "overbought" else "과매도"
        events = []
        for user_sub in users:
            preferences = preference_response(self.preference_repository.get(user_sub))
            if not self._allowed(preferences, "rsiBand", candle["symbol"]):
                continue
            events.append({
                "eventId": f"reminder:rsi:{user_sub}:{candle['symbol']}:{candle['timestamp']}:{band}",
                "type": "system.rsi_band",
                "userSub": user_sub,
                "symbol": candle["symbol"],
                "kind": "rsi_band",
                "interval": "1D",
                "metrics": {"rsi": current, "previousRsi": previous, "period": 14, "band": band},
                "title": f"{candle['symbol']} RSI {label} 진입",
                "summary": f"일봉 RSI(14)가 {current:.1f}로 {label} 구간에 진입했습니다.",
                "triggeredAt": datetime.now(timezone.utc).isoformat(),
            })
        return events

    def _watchlist_users(self, symbol: str) -> list[str]:
        values = self.redis.smembers(self.keys.subscription_source_watchlist(symbol))
        users = set()
        for value in values:
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            normalized = str(value or "").strip()
            if normalized and normalized != "__legacy__":
                users.add(normalized)
        return sorted(users)

    def _subscribed_users(self, symbol: str) -> list[str]:
        values = set(self.redis.smembers(self.keys.subscription_source_watchlist(symbol)))
        values.update(self.redis.smembers(self.keys.subscription_source_portfolio(symbol)))
        users = set()
        for value in values:
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            normalized = str(value or "").strip()
            if normalized and normalized != "__legacy__":
                users.add(normalized)
        return sorted(users)

    def watchlist_symbols(self) -> list[str]:
        values = self.redis.smembers(self.keys.subscription_source_symbols("watchlist"))
        symbols = set()
        for value in values:
            normalized = value.decode("utf-8") if isinstance(value, bytes) else str(value or "")
            normalized = normalized.strip().upper()
            if normalized:
                symbols.add(normalized)
        return sorted(symbols)

    @staticmethod
    def _allowed(preferences: dict[str, Any], setting: str, symbol: str) -> bool:
        return (
            preferences["settings"].get("master") is True
            and preferences["settings"].get(setting) is True
            and preferences["companyOverrides"].get(symbol) is not False
        )

class AlertRedisOutbox:
    def __init__(self, redis_client: Any, stream: str = DEFAULT_OUTBOX_STREAM, maxlen: int = 100_000) -> None:
        self.redis = redis_client
        self.stream = os.getenv("ALERT_OUTBOX_STREAM", stream)
        self.maxlen = int(os.getenv("ALERT_OUTBOX_MAXLEN", str(maxlen)))

    def enqueue(self, payload: dict[str, Any]) -> str:
        return self.redis.xadd(
            self.stream,
            {"payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)},
            maxlen=self.maxlen,
            approximate=True,
        )


class AlertOutboxSender:
    def __init__(
        self,
        *,
        redis_client: Any,
        repository: Any,
        broker: RedisNotificationBroker,
        producer: Any,
        triggered_topic: str,
        stream: str,
        group: str,
        consumer_name: str,
        preference_repository: Any | None = None,
        projection: RedisAlertProjection | None = None,
    ) -> None:
        self.redis = redis_client
        self.repository = repository
        self.broker = broker
        self.producer = producer
        self.triggered_topic = triggered_topic
        self.stream = stream
        self.group = group
        self.consumer_name = consumer_name
        self.preference_repository = preference_repository
        self.projection = projection
        self._group_ready = False

    def ensure_group(self) -> None:
        if self._group_ready:
            return
        try:
            self.redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_ready = True

    def process_once(self, *, count: int = 100, block_ms: int = 100) -> int:
        self.ensure_group()
        messages = self.redis.xreadgroup(
            self.group,
            self.consumer_name,
            {self.stream: ">"},
            count=count,
            block=block_ms,
        )
        delivered = self._process_stream_messages(messages)
        if delivered:
            return delivered

        min_idle_ms = int(os.getenv("ALERT_OUTBOX_AUTOCLAIM_IDLE_MS", "30000"))
        claimed = self.redis.xautoclaim(self.stream, self.group, self.consumer_name, min_idle_ms, "0-0", count=count)
        entries = _entries_from_xautoclaim(claimed)
        return self._process_entries(entries)

    def _process_stream_messages(self, messages: Any) -> int:
        delivered = 0
        for _stream_name, entries in messages:
            delivered += self._process_entries(entries)
        return delivered

    def _process_entries(self, entries: Any) -> int:
        delivered = 0
        for message_id, fields in entries or []:
            payload = _payload_from_stream_fields(fields)
            self.deliver(payload)
            self.redis.xack(self.stream, self.group, message_id)
            delivered += 1
        return delivered

    def deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
        alert = payload.get("alert") if isinstance(payload.get("alert"), dict) else {}
        get_alert = getattr(self.repository, "get_alert", None)
        if callable(get_alert) and payload.get("alertId") is not None:
            current_alert = get_alert(str(payload["userSub"]), int(payload["alertId"]))
            if isinstance(current_alert, dict):
                alert = current_alert
        notification = None
        notification_allowed = alert.get("notifications_enabled", True) is not False
        skip_reason = "notifications_disabled"
        if notification_allowed and self.preference_repository is not None:
            preferences = preference_response(self.preference_repository.get(str(payload["userSub"])))
            notification_allowed, skip_reason = notification_delivery_decision(
                str(payload["type"]),
                payload,
                preferences,
            )
        if notification_allowed:
            persist = getattr(self.repository, "persist_triggered_notification", None)
            if callable(persist):
                notification, updated_alert = persist(
                    user_sub=str(payload["userSub"]),
                    alert_id=int(payload["alertId"]) if payload.get("alertId") is not None else None,
                    event_id=str(payload["eventId"]),
                    notification_type=str(payload["type"]),
                    payload=payload,
                )
            else:
                notification = self.repository.create_notification_once(
                    user_sub=str(payload["userSub"]),
                    alert_id=int(payload["alertId"]) if payload.get("alertId") is not None else None,
                    event_id=str(payload["eventId"]),
                    notification_type=str(payload["type"]),
                    payload=payload,
                )
                updated_alert = (
                    self.repository.record_alert_trigger(str(payload["userSub"]), int(payload["alertId"]))
                    if notification is not None and payload.get("alertId") is not None
                    else None
                )
            if notification is not None:
                if payload.get("alertId") is not None:
                    if self.projection is not None:
                        if not updated_alert or updated_alert.get("status") != "active":
                            self.projection.delete_alert(payload["alertId"], symbol=payload.get("symbol"))
                        else:
                            self.projection.upsert_alert(updated_alert)
                websocket_payload = {
                    "type": "notification",
                    "notification": notification,
                    "event": payload,
                    "updatedAlert": updated_alert,
                }
                self.broker.publish_user(str(payload["userSub"]), websocket_payload)
            else:
                skip_reason = "duplicate_event"
        future = self.producer.send(self.triggered_topic, key=str(payload.get("symbol") or payload["alertId"]), value=payload)
        if hasattr(future, "get"):
            future.get(timeout=float(os.getenv("ALERT_KAFKA_SEND_TIMEOUT_SECONDS", "10")))
        return notification or {"skipped": True, "reason": skip_reason, "eventId": payload.get("eventId")}


def run() -> None:
    if redis is None:
        raise RuntimeError("redis package is not installed")
    kafka_servers = _required_env("KAFKA_BOOTSTRAP_SERVERS")
    redis_url = _required_env("REDIS_URL")
    input_topics = _csv_values(os.getenv("ALERT_EVALUATOR_INPUT_TOPICS"))
    if not input_topics:
        legacy_input = os.getenv("ALERT_EVALUATOR_INPUT_TOPIC")
        input_topics = [legacy_input] if legacy_input else list(DEFAULT_INPUT_TOPICS)
    triggered_topic = os.getenv("ALERT_TRIGGERED_TOPIC", DEFAULT_TRIGGERED_TOPIC)
    group_id = os.getenv("ALERT_EVALUATOR_GROUP_ID", DEFAULT_GROUP_ID)
    dlq_topic = os.getenv("ALERT_DLQ_TOPIC", DEFAULT_DLQ_TOPIC)
    reconcile_seconds = int(os.getenv("ALERT_RECONCILE_INTERVAL_SECONDS", "60"))
    outbox_stream = os.getenv("ALERT_OUTBOX_STREAM", DEFAULT_OUTBOX_STREAM)
    outbox_group = os.getenv("ALERT_OUTBOX_GROUP", DEFAULT_OUTBOX_GROUP)
    consumer_name = os.getenv("HOSTNAME", "alert-evaluator")

    redis_client = redis.from_url(redis_url, decode_responses=True)
    repository = PostgresAlertRepository.from_env()
    preference_repository = PostgresNotificationPreferenceRepository.from_env()
    projection = RedisAlertProjection(redis_client)
    producer = create_json_producer(kafka_servers, "gops-alert-evaluator")
    consumer = create_json_consumer(
        input_topics,
        kafka_servers,
        group_id,
        "gops-alert-evaluator",
        enable_auto_commit=False,
    )
    outbox = AlertRedisOutbox(redis_client, stream=outbox_stream)
    evaluator = AlertEvaluator(
        repository=repository,
        projection=projection,
        outbox=outbox,
        preference_repository=preference_repository,
        reminder_dispatcher=ReminderDispatcher(
            redis_client=redis_client,
            preference_repository=preference_repository,
            market_snapshot_provider=HeatmapMarketSnapshotLookup(get_heatmap_service()),
        ),
        history_loader=ClickHouseMarketDataProvider().candles,
    )
    sender = AlertOutboxSender(
        redis_client=redis_client,
        repository=repository,
        broker=RedisNotificationBroker(redis_client),
        producer=producer,
        triggered_topic=triggered_topic,
        stream=outbox_stream,
        group=outbox_group,
        consumer_name=consumer_name,
        preference_repository=preference_repository,
        projection=projection,
    )

    evaluator.reconcile_active_alerts()
    next_reconcile_at = time.monotonic() + reconcile_seconds
    print(
        "Alert evaluator started: "
        f"kafka={kafka_servers} inputs={input_topics} triggered={triggered_topic} redis={redis_url}",
        flush=True,
    )
    while True:
        try:
            records = consumer.poll(timeout_ms=1000)
            processed = False
            for _partition, partition_records in records.items():
                for record in partition_records:
                    evaluator.process_message(record.value, record.topic)
                    processed = True
            if processed:
                consumer.commit()
            sender.process_once(count=100, block_ms=50)
            if time.monotonic() >= next_reconcile_at:
                evaluator.reconcile_active_alerts()
                next_reconcile_at = time.monotonic() + reconcile_seconds
        except Exception as exc:
            traceback.print_exc()
            try:
                producer.send(
                    dlq_topic,
                    key="alert-evaluator",
                    value={"error": str(exc), "occurredAt": datetime.now(timezone.utc).isoformat()},
                )
            except Exception:
                traceback.print_exc()
            time.sleep(1)


def parse_trade(payload: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    symbol_value = payload.get("symbol")
    price = _float_or_none(payload.get("price") or payload.get("p"))
    if not isinstance(symbol_value, str) or price is None:
        return None
    try:
        symbol = normalize_market_symbol(symbol_value)
    except Exception:
        return None
    timestamp = str(payload.get("timestamp") or payload.get("t") or datetime.now(timezone.utc).isoformat())
    return {
        "symbol": symbol,
        "price": price,
        "timestamp": timestamp,
        "timestampMs": _timestamp_ms(timestamp),
        "sourceEventId": payload.get("sourceEventId"),
        "tradeId": payload.get("tradeId") or payload.get("i"),
    }


def parse_candle(payload: dict[str, Any], topic: str = "") -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    symbol_value = payload.get("symbol")
    close = _float_or_none(payload.get("close") or payload.get("c"))
    volume = _float_or_none(payload.get("volume") if payload.get("volume") is not None else payload.get("v"))
    if not isinstance(symbol_value, str) or close is None or volume is None or volume < 0:
        return None
    try:
        symbol = normalize_market_symbol(symbol_value)
    except Exception:
        return None
    interval = _normalize_candle_interval(payload.get("interval"), topic)
    if interval is None:
        return None
    timestamp = str(payload.get("timestamp") or payload.get("t") or datetime.now(timezone.utc).isoformat())
    return {
        "symbol": symbol,
        "interval": interval,
        "timestamp": timestamp,
        "timestampMs": _timestamp_ms(timestamp),
        "open": _float_or_none(payload.get("open") or payload.get("o")),
        "high": _float_or_none(payload.get("high") or payload.get("h")),
        "low": _float_or_none(payload.get("low") or payload.get("l")),
        "close": close,
        "volume": volume,
        "sourceEventId": payload.get("sourceEventId"),
    }


def _normalize_candle_interval(value: Any, topic: str) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        match = re.search(r"\.candles\.([^.]+)\.closed\.", topic)
        raw = match.group(1) if match else ""
    normalized = "1D" if raw.lower() == "1d" else raw.lower()
    return normalized if normalized in {"1m", "5m", "10m", "1h", "4h", "1D"} else None


def _payload_from_stream_fields(fields: dict[str, Any]) -> dict[str, Any]:
    payload = fields.get("payload")
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        loaded = json.loads(payload)
        if isinstance(loaded, dict):
            return loaded
    if isinstance(payload, dict):
        return payload
    raise ValueError("alert outbox payload is invalid")


def _entries_from_xautoclaim(value: Any) -> list[Any]:
    if isinstance(value, tuple | list):
        if len(value) >= 2 and isinstance(value[1], list):
            return value[1]
        if value and all(isinstance(item, tuple | list) and len(item) == 2 for item in value):
            return list(value)
        return []
    return []


def _timestamp_ms(value: str) -> int:
    try:
        normalized = value.replace("Z", "+00:00")
        return int(datetime.fromisoformat(normalized).timestamp() * 1000)
    except Exception:
        return int(time.time() * 1000)


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _csv_values(value: str | None) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


if __name__ == "__main__":
    run()
