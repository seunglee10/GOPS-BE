from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from alfaka.common.redis_keys import RedisKeyBuilder

from app.alerts.notifications import RedisNotificationBroker, notification_delivery_decision
from app.alerts.preferences import PostgresNotificationPreferenceRepository, preference_response
from app.alerts.repository import PostgresAlertRepository
from app.market_data.calendar.service import configured_closed_dates, is_session_date
from app.services.alfaka_market_data import get_market_data_provider, read_watchlist_symbols, symbol_summaries_for


SEOUL = ZoneInfo("Asia/Seoul")
NEW_YORK = ZoneInfo("America/New_York")
WATCHLIST_SOURCE = "watchlist"


@dataclass
class ScheduledNotificationService:
    preference_repository: Any
    notification_repository: Any
    broker: Any
    user_provider: Callable[[], Iterable[str]]
    watchlist_provider: Callable[[str], list[dict[str, Any]]]
    earnings_provider: Callable[[date], list[dict[str, Any]]]

    def send_market_close_summaries(self, now: datetime | None = None) -> dict[str, Any]:
        current = _aware(now)
        market_date = current.astimezone(NEW_YORK).date()
        if not is_session_date(market_date, configured_closed_dates(market_date.year, market_date.year)):
            return {"job": "market-close", "marketDate": market_date.isoformat(), "sent": 0, "skipped": "market_closed"}

        sent = 0
        duplicate = 0
        for user_sub in _normalized_users(self.user_provider()):
            preferences = preference_response(self.preference_repository.get(user_sub))
            base_payload = {"kind": "market_close_summary", "marketDate": market_date.isoformat()}
            allowed, _reason = notification_delivery_decision(
                "system.market_close_summary",
                base_payload,
                preferences,
            )
            if not allowed:
                continue

            normalized_items = []
            for item in self.watchlist_provider(user_sub):
                symbol = str(item.get("symbol") or "").upper()
                if preferences["companyOverrides"].get(symbol) is False:
                    continue
                normalized = _summary_item(item)
                if normalized is not None:
                    normalized_items.append(normalized)
            if not normalized_items:
                continue
            payload = {
                **base_payload,
                "title": "장 마감 관심기업 요약",
                "summary": _market_close_summary_text(normalized_items),
                "items": normalized_items,
            }
            notification = self.notification_repository.create_notification_once(
                user_sub=user_sub,
                alert_id=None,
                event_id=f"market-close-summary:{market_date.isoformat()}:{user_sub}",
                notification_type="system.market_close_summary",
                payload=payload,
            )
            if notification is None:
                duplicate += 1
                continue
            self._publish(user_sub, notification, payload)
            sent += 1
        return {
            "job": "market-close",
            "marketDate": market_date.isoformat(),
            "sent": sent,
            "duplicates": duplicate,
        }

    def send_earnings_d1(self, now: datetime | None = None) -> dict[str, Any]:
        current = _aware(now)
        earnings_date = current.astimezone(SEOUL).date() + timedelta(days=1)
        events = {
            str(item.get("symbol") or "").strip().upper(): item
            for item in self.earnings_provider(earnings_date)
            if str(item.get("symbol") or "").strip()
        }
        if not events:
            return {"job": "earnings-d1", "earningsDate": earnings_date.isoformat(), "sent": 0, "events": 0}

        sent = 0
        duplicate = 0
        for user_sub in _normalized_users(self.user_provider()):
            preferences = preference_response(self.preference_repository.get(user_sub))
            watchlist = self.watchlist_provider(user_sub)
            for company in watchlist:
                symbol = str(company.get("symbol") or "").strip().upper()
                if symbol not in events:
                    continue
                payload = {
                    "kind": "earnings_d1",
                    "symbol": symbol,
                    "companyName": company.get("name") or symbol,
                    "earningsDate": earnings_date.isoformat(),
                    "title": f"{company.get('name') or symbol} 실적 발표 D-1",
                    "summary": f"{symbol}의 실적 발표가 내일 예정되어 있습니다.",
                    "source": events[symbol].get("source") or "yahoo-earnings-dates",
                }
                allowed, _reason = notification_delivery_decision(
                    "system.earnings_d1",
                    payload,
                    preferences,
                )
                if not allowed:
                    continue
                notification = self.notification_repository.create_notification_once(
                    user_sub=user_sub,
                    alert_id=None,
                    event_id=f"earnings-d1:{earnings_date.isoformat()}:{user_sub}:{symbol}",
                    notification_type="system.earnings_d1",
                    payload=payload,
                )
                if notification is None:
                    duplicate += 1
                    continue
                self._publish(user_sub, notification, payload)
                sent += 1
        return {
            "job": "earnings-d1",
            "earningsDate": earnings_date.isoformat(),
            "events": len(events),
            "sent": sent,
            "duplicates": duplicate,
        }

    def _publish(self, user_sub: str, notification: dict[str, Any], payload: dict[str, Any]) -> None:
        self.broker.publish_user(user_sub, {
            "type": "notification",
            "notification": notification,
            "event": payload,
        })


class ClickHouseEarningsCalendar:
    def __init__(self, clickhouse_provider: Any) -> None:
        self.provider = clickhouse_provider

    def events_on(self, target_date: date) -> list[dict[str, Any]]:
        table = self.provider.table("yahoo_earnings_estimates")
        query = f"""
        SELECT
          symbol,
          toString(period_end) AS earningsDate,
          'yahoo-earnings-dates' AS source
        FROM {table}
        WHERE metric = 'eps'
          AND period_end = {{targetDate:Date}}
          AND JSONExtractString(raw, 'sourceFrame') = 'earnings_dates'
        GROUP BY symbol, period_end
        ORDER BY symbol
        FORMAT JSONEachRow
        """
        return self.provider.query_json_each_row(query, {"targetDate": target_date.isoformat()})


def build_scheduled_notification_service() -> ScheduledNotificationService:
    market_provider = get_market_data_provider()
    redis_client = market_provider.redis_provider.redis
    keys = RedisKeyBuilder()

    def users() -> Iterable[str]:
        return redis_client.smembers(keys.subscription_users(WATCHLIST_SOURCE))

    def watchlist(user_sub: str) -> list[dict[str, Any]]:
        symbols = read_watchlist_symbols(user_sub)
        return symbol_summaries_for(symbols) if symbols else []

    earnings_calendar = ClickHouseEarningsCalendar(market_provider.clickhouse_provider)
    return ScheduledNotificationService(
        preference_repository=PostgresNotificationPreferenceRepository.from_env(),
        notification_repository=PostgresAlertRepository.from_env(),
        broker=RedisNotificationBroker(redis_client),
        user_provider=users,
        watchlist_provider=watchlist,
        earnings_provider=earnings_calendar.events_on,
    )


def _summary_item(value: dict[str, Any]) -> dict[str, Any] | None:
    symbol = str(value.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    change_percent = _number_or_none(value.get("changePercent"))
    return {
        "symbol": symbol,
        "name": value.get("name") or symbol,
        "lastPrice": _number_or_none(value.get("lastPrice")),
        "changePercent": change_percent,
    }


def _market_close_summary_text(items: list[dict[str, Any]]) -> str:
    changes = [item["changePercent"] for item in items if item.get("changePercent") is not None]
    gainers = sum(1 for value in changes if value > 0)
    losers = sum(1 for value in changes if value < 0)
    return f"관심 기업 {len(items)}개 · 상승 {gainers} · 하락 {losers}"


def _normalized_users(values: Iterable[Any]) -> list[str]:
    users = set()
    for value in values:
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        normalized = str(value or "").strip()
        if normalized and normalized != "__legacy__":
            users.add(normalized)
    return sorted(users)


def _number_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Send scheduled GOPS notifications.")
    parser.add_argument("job", choices=("market-close", "earnings-d1"))
    args = parser.parse_args()
    service = build_scheduled_notification_service()
    result = (
        service.send_market_close_summaries()
        if args.job == "market-close"
        else service.send_earnings_d1()
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
