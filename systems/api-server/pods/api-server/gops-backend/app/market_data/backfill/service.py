from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from alfaka.backfill.gapfill import TradingCalendar, expected_bucket_starts
from alfaka.backfill.runner import BackfillRunner
from alfaka.backfill.status import ACTIVE_STATUSES, RedisBackfillStore, parse_time as parse_backfill_time
from alfaka.serving.intervals import (
    interval_seconds,
    minimum_renderable_returned_bars,
    minimum_renderable_source_bars,
    normalize_chart_interval,
    source_interval_for,
)


logger = logging.getLogger(__name__)


class BackfillService:
    def __init__(self, provider=None, store=None, runner_factory=None):
        self.provider = provider
        self.store = store or RedisBackfillStore(redis_client=getattr(getattr(provider, "redis_provider", None), "redis", None))
        self.runner_factory = runner_factory or (lambda: BackfillRunner(store=self.store))

    def snapshot_metadata(self, symbol: str, interval: str, payload_or_has_candles: Any) -> dict[str, Any]:
        interval = normalize_chart_interval(interval)
        source_interval = source_interval_for(interval)
        if isinstance(payload_or_has_candles, dict):
            returned_count = int(payload_or_has_candles.get("returnedCount") or len(payload_or_has_candles.get("candles") or []))
            requested_limit = int(payload_or_has_candles.get("requestedLimit") or returned_count)
            stored_count = int(payload_or_has_candles.get("storedCandleCount") or returned_count)
            available_from = payload_or_has_candles.get("availableFrom")
            available_to = payload_or_has_candles.get("availableTo")
            no_data_before = payload_or_has_candles.get("noDataBefore")
            requested_range = payload_or_has_candles.get("requestedRange") or {}
            invalid_row_count = int(payload_or_has_candles.get("invalidRowCount") or 0)
            candles = payload_or_has_candles.get("candles") or []
        else:
            returned_count = 1 if payload_or_has_candles else 0
            requested_limit = returned_count
            stored_count = returned_count
            available_from = None
            available_to = None
            no_data_before = None
            requested_range = {}
            invalid_row_count = 0
            candles = []

        latest = self._latest_status(symbol, source_interval)
        backfill_status = latest.get("status") if latest else "not_requested"
        no_data_boundary_reached = requested_range_reached_no_data_boundary(requested_range, no_data_before)

        renderability = renderability_payload(
            interval,
            source_interval,
            candles,
            returned_count,
            stored_count,
            requested_range,
            requested_limit,
        )
        renderable = renderability["renderable"] and invalid_row_count <= 0
        repair_status = "none" if no_data_boundary_reached else repair_status_for(
            backfill_status=backfill_status,
            renderability=renderability,
        )
        can_backfill = can_request_backfill(backfill_status) and not no_data_boundary_reached
        if returned_count > 0 and renderable:
            reason_code = renderability["renderabilityReasonCode"] or "requested_range_renderable"
            coverage = coverage_payload(
                state="complete" if repair_status == "none" else "partial",
                reason_code=reason_code,
                message=None,
                repair_status=repair_status,
                source_interval=source_interval,
                backfill_status=backfill_status,
                requested_limit=requested_limit,
                returned_count=returned_count,
                stored_count=stored_count,
                available_from=available_from,
                available_to=available_to,
                no_data_before=no_data_before,
                requested_range=requested_range,
                invalid_row_count=invalid_row_count,
                renderability=renderability,
            )
            return {
                "dataStatus": "ready",
                "backfillStatus": backfill_status,
                "repairStatus": repair_status,
                "canBackfill": can_backfill,
                "sourceInterval": source_interval,
                "message": None,
                "coverage": coverage,
            }

        if returned_count > 0:
            partial_message = latest.get("error") if latest and latest.get("status") in {"failed", "unavailable"} else None
            reason_code = renderability["renderabilityReasonCode"] or coverage_reason(backfill_status, "requested_range_incomplete")
            data_status = "partial"
            message = partial_message or partial_coverage_message(
                symbol=symbol,
                interval=interval,
                source_interval=source_interval,
                returned_count=returned_count,
                renderability=renderability,
                invalid_row_count=invalid_row_count,
            )
            coverage = coverage_payload(
                state="partial",
                reason_code=reason_code,
                message=message,
                repair_status=repair_status,
                source_interval=source_interval,
                backfill_status=backfill_status,
                requested_limit=requested_limit,
                returned_count=returned_count,
                stored_count=stored_count,
                available_from=available_from,
                available_to=available_to,
                no_data_before=no_data_before,
                requested_range=requested_range,
                invalid_row_count=invalid_row_count,
                renderability=renderability,
            )
            return {
                "dataStatus": data_status,
                "backfillStatus": backfill_status,
                "repairStatus": repair_status,
                "canBackfill": can_backfill,
                "sourceInterval": source_interval,
                "message": message,
                "coverage": coverage,
            }

        message = latest.get("error") if latest and latest.get("status") in {"failed", "unavailable"} else None
        if not message:
            if no_data_boundary_reached:
                message = f"No Alpaca {source_interval} candle data is available before {no_data_before} for {symbol}."
            elif backfill_status in ACTIVE_STATUSES:
                message = f"Preparing {source_interval} candle data for {symbol}."
            elif backfill_status == "succeeded":
                message = f"Backfill completed, but no stored {source_interval} candles were found for {symbol}."
            else:
                message = f"No stored {source_interval} candles were found for {symbol}."
        data_status = "empty" if can_backfill else "error" if backfill_status in {"failed", "unavailable"} else "empty"
        coverage = coverage_payload(
            state="unavailable" if backfill_status in {"failed", "unavailable"} else "empty",
            reason_code="no_data_boundary_reached" if no_data_boundary_reached else coverage_reason(backfill_status, "no_stored_candles"),
            message=message,
            repair_status=repair_status,
            source_interval=source_interval,
            backfill_status=backfill_status,
            requested_limit=requested_limit,
            returned_count=returned_count,
            stored_count=stored_count,
            available_from=available_from,
            available_to=available_to,
            no_data_before=no_data_before,
            requested_range=requested_range,
            invalid_row_count=invalid_row_count,
            renderability=renderability,
        )
        return {
            "dataStatus": data_status,
            "backfillStatus": backfill_status,
            "repairStatus": repair_status,
            "canBackfill": can_backfill,
            "sourceInterval": source_interval,
            "message": message,
            "coverage": coverage,
        }

    def request_backfill(
        self,
        symbol: str,
        interval: str,
        start: str | None = None,
        end: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        interval = normalize_chart_interval(interval)
        source_interval = source_interval_for(interval)
        try:
            validate_requested_range(start, end)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            record, deduplicated = self.store.create_request(symbol, source_interval, start=start, end=end, force=force)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Backfill status store failed: {exc}") from exc

        return summarize_status(record, deduplicated=deduplicated, requested_interval=interval, source_interval=source_interval)

    def get_status(self, symbol: str, interval: str, request_id: str | None = None) -> dict[str, Any]:
        interval = normalize_chart_interval(interval)
        source_interval = source_interval_for(interval)
        try:
            record = self.store.get_status(request_id) if request_id else self.store.latest_status(symbol, source_interval)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Backfill status store failed: {exc}") from exc
        if not record:
            return {
                "symbol": symbol,
                "interval": interval,
                "requestId": None,
                "status": "not_requested",
                "range": None,
                "sourceInterval": source_interval,
            }
        return summarize_status(record, requested_interval=interval, source_interval=source_interval)

    def queue_metrics(self) -> dict[str, Any]:
        try:
            return self.store.queue_metrics()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Backfill queue metrics failed: {exc}") from exc

    def _latest_status(self, symbol: str, interval: str) -> dict[str, Any] | None:
        interval = normalize_chart_interval(interval)
        try:
            return self.store.latest_status(symbol, interval)
        except Exception:
            logger.warning("Backfill status lookup failed for %s %s.", symbol, interval, exc_info=True)
            return None


def validate_requested_range(start: str | None, end: str | None) -> None:
    if not start or not end:
        raise ValueError("Range backfill requires explicit start and end timestamps.")
    start_dt = parse_backfill_time(start)
    end_dt = parse_backfill_time(end)
    if end_dt <= start_dt:
        raise ValueError("Range backfill end must be after start.")


def summarize_status(
    record: dict[str, Any],
    deduplicated: bool | None = None,
    requested_interval: str | None = None,
    source_interval: str | None = None,
) -> dict[str, Any]:
    source_interval = source_interval or record["interval"]
    payload = {
        "symbol": record["symbol"],
        "interval": requested_interval or record["interval"],
        "sourceInterval": source_interval,
        "requestId": record["requestId"],
        "status": record["status"],
        "range": record.get("range"),
        "requestedAt": record.get("requestedAt"),
        "updatedAt": record.get("updatedAt"),
        "startedAt": record.get("startedAt"),
        "finishedAt": record.get("finishedAt"),
        "error": record.get("error"),
        "result": record.get("result"),
    }
    if deduplicated is not None:
        payload["deduplicated"] = deduplicated
    return payload


def coverage_reason(backfill_status: str, default: str) -> str:
    if backfill_status in ACTIVE_STATUSES:
        return "backfill_active"
    if backfill_status == "failed":
        return "backfill_failed"
    if backfill_status == "unavailable":
        return "backfill_unavailable"
    if backfill_status == "succeeded":
        return "backfill_succeeded_range_still_missing"
    return default


def can_request_backfill(backfill_status: str) -> bool:
    return backfill_status not in ACTIVE_STATUSES


def requested_range_reached_no_data_boundary(
    requested_range: dict[str, Any] | None,
    no_data_before: str | None,
) -> bool:
    boundary = parse_time(no_data_before)
    if not boundary or not requested_range:
        return False
    before = parse_time(requested_range.get("before"))
    if before:
        return before <= boundary
    to_time = parse_time(requested_range.get("to"))
    return bool(to_time and to_time <= boundary)


def repair_status_for(
    *,
    backfill_status: str,
    renderability: dict[str, Any],
) -> str:
    if backfill_status in ACTIVE_STATUSES:
        return "gapfill_active"
    if backfill_status in {"failed", "unavailable"}:
        return "gapfill_failed"
    if not renderability.get("renderabilityReasonCode"):
        return "none"
    return "gapfill_required"


def coverage_payload(
    *,
    state: str,
    reason_code: str,
    message: str | None,
    repair_status: str,
    source_interval: str,
    backfill_status: str,
    requested_limit: int,
    returned_count: int,
    stored_count: int,
    available_from: str | None,
    available_to: str | None,
    no_data_before: str | None,
    requested_range: dict[str, Any],
    invalid_row_count: int,
    renderability: dict[str, Any],
) -> dict[str, Any]:
    return {
        "state": state,
        "reasonCode": reason_code,
        "message": message,
        "repairStatus": repair_status,
        "sourceInterval": source_interval,
        "backfillStatus": backfill_status,
        "requestedLimit": requested_limit,
        "returnedCount": returned_count,
        "storedCandleCount": stored_count,
        "availableFrom": available_from,
        "availableTo": available_to,
        "noDataBefore": no_data_before,
        "requestedRange": requested_range,
        "invalidRowCount": invalid_row_count,
        **renderability,
    }


def renderability_payload(
    interval: str,
    source_interval: str,
    candles: list[dict[str, Any]],
    returned_count: int,
    stored_count: int,
    requested_range: dict[str, Any] | None = None,
    requested_limit: int | None = None,
) -> dict[str, Any]:
    configured_minimum_returned_count = minimum_renderable_returned_bars(interval)
    minimum_returned_count = effective_minimum_returned_count(configured_minimum_returned_count, requested_limit)
    minimum_source_bars = minimum_renderable_source_bars(source_interval)
    span_seconds = returned_span_seconds(candles)
    max_span_seconds = max_renderable_span_seconds(interval, returned_count)
    sparse_window = sparse_returned_window(interval, candles, returned_count, span_seconds, max_span_seconds)
    expected_range_bars = expected_chart_bars_for_requested_range(interval, source_interval, requested_range)
    requested_range_complete = expected_range_bars is not None and expected_range_bars > 0 and returned_count >= expected_range_bars
    renderable = returned_count > 0 and not sparse_window
    reason_code = None
    if returned_count <= 0:
        reason_code = "no_stored_candles"
    elif stored_count < minimum_source_bars and not requested_range_complete:
        reason_code = "insufficient_source_bars"
    elif returned_count < minimum_returned_count and not requested_range_complete:
        reason_code = "insufficient_returned_bars"
    elif sparse_window:
        reason_code = "returned_window_sparse"
    return {
        "renderable": renderable,
        "minimumReturnedCount": minimum_returned_count,
        "minimumRenderableSourceBars": minimum_source_bars,
        "expectedRequestedRangeBars": expected_range_bars,
        "returnedSpanSeconds": span_seconds,
        "maxRenderableSpanSeconds": max_span_seconds,
        "renderabilityReasonCode": reason_code,
    }


def effective_minimum_returned_count(configured_minimum: int, requested_limit: int | None) -> int:
    if requested_limit is None:
        return configured_minimum
    try:
        limit = max(1, int(requested_limit))
    except (TypeError, ValueError):
        return configured_minimum
    return min(configured_minimum, limit)


def expected_chart_bars_for_requested_range(
    interval: str,
    source_interval: str,
    requested_range: dict[str, Any] | None,
) -> int | None:
    if not requested_range:
        return None
    start = requested_range.get("from")
    end = requested_range.get("to")
    if not start or not end:
        return None
    try:
        source_buckets = expected_bucket_starts(start, end, source_interval)
    except (TypeError, ValueError):
        return None
    if not source_buckets:
        return 0
    if interval == source_interval:
        return len(source_buckets)
    if interval in {"5m", "10m"}:
        bucket_minutes = 5 if interval == "5m" else 10
        return len({minute_bucket_start(bucket, bucket_minutes) for bucket in source_buckets})
    if interval == "1W":
        return len({week_bucket_start(bucket) for bucket in source_buckets})
    if interval == "1M":
        return len({month_bucket_start(bucket) for bucket in source_buckets})
    return None


def minute_bucket_start(value: datetime, bucket_minutes: int) -> datetime:
    value = value.astimezone(timezone.utc)
    return value.replace(minute=(value.minute // bucket_minutes) * bucket_minutes, second=0, microsecond=0)


def week_bucket_start(value: datetime) -> datetime:
    bucket_date = value.astimezone(timezone.utc).date() - timedelta(days=value.weekday())
    return datetime.combine(bucket_date, datetime.min.time(), timezone.utc)


def month_bucket_start(value: datetime) -> datetime:
    date_value = value.astimezone(timezone.utc).date()
    return datetime.combine(date_value.replace(day=1), datetime.min.time(), timezone.utc)


def partial_coverage_message(
    *,
    symbol: str,
    interval: str,
    source_interval: str,
    returned_count: int,
    renderability: dict[str, Any],
    invalid_row_count: int,
) -> str:
    _ = invalid_row_count
    reason = renderability.get("reasonCode") or renderability.get("renderabilityReasonCode")
    if reason == "insufficient_source_bars":
        return (
            f"Stored {source_interval} candle coverage is too small to draw a trustworthy {interval} chart for {symbol}. "
            "Historical backfill or local data repair is required."
        )
    if reason == "insufficient_returned_bars":
        return (
            f"Only {returned_count} {interval} candles are available for {symbol}. "
            "Additional historical candles will be requested to extend this range."
        )
    if reason == "returned_window_sparse":
        return (
            f"Available {interval} candles for {symbol} are too sparse across time to render as a continuous chart. "
            "Historical backfill or local data repair is required."
        )
    return (
        f"Loaded {returned_count} {interval} candles for {symbol}, but the requested range still needs gapfill."
    )


def returned_span_seconds(candles: list[dict[str, Any]]) -> int | None:
    timestamps = [parse_time(candle.get("timestamp")) for candle in candles if candle.get("timestamp")]
    timestamps = [timestamp for timestamp in timestamps if timestamp is not None]
    if len(timestamps) < 2:
        return None
    return int((max(timestamps) - min(timestamps)).total_seconds())


def sparse_returned_window(
    interval: str,
    candles: list[dict[str, Any]],
    returned_count: int,
    span_seconds: int | None,
    max_span_seconds: int | None,
) -> bool:
    if returned_count < 2 or span_seconds is None or max_span_seconds is None:
        return False
    if interval in {"1m", "5m", "10m"}:
        return has_intraday_sparse_gap(interval, candles)
    return span_seconds > max_span_seconds


def has_intraday_sparse_gap(interval: str, candles: list[dict[str, Any]]) -> bool:
    timestamps = sorted(
        timestamp for timestamp in (parse_time(candle.get("timestamp")) for candle in candles if candle.get("timestamp"))
        if timestamp is not None
    )
    if len(timestamps) < 2:
        return False
    allowed_gap = interval_seconds(interval) * 3
    calendar = TradingCalendar.from_environment()
    market_timezone = calendar.timezone
    for previous, current in zip(timestamps, timestamps[1:]):
        gap_seconds = (current - previous).total_seconds()
        if gap_seconds <= allowed_gap:
            continue
        if same_regular_market_session_gap(previous, current, calendar, market_timezone):
            return True
    return False


def same_regular_market_session_gap(
    left: datetime,
    right: datetime,
    calendar: TradingCalendar,
    market_timezone: ZoneInfo,
) -> bool:
    if not same_market_session_date(left, right, market_timezone):
        return False
    return in_regular_market_session(left, calendar) and in_regular_market_session(right, calendar)


def in_regular_market_session(value: datetime, calendar: TradingCalendar) -> bool:
    local = value.astimezone(calendar.timezone)
    session_date = local.date()
    if not calendar.is_session_date(session_date):
        return False
    return calendar.open_time <= local.time() < calendar.session_close_for(session_date)


def same_market_session_date(left: datetime, right: datetime, market_timezone: ZoneInfo) -> bool:
    return left.astimezone(market_timezone).date() == right.astimezone(market_timezone).date()


def max_renderable_span_seconds(interval: str, returned_count: int) -> int | None:
    if returned_count < 2:
        return None
    factor = 3 if interval in {"1m", "5m", "10m"} else 2
    return int(interval_seconds(interval) * max(1, returned_count - 1) * factor)


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def get_backfill_service(provider=None) -> BackfillService:
    return BackfillService(provider=provider)
