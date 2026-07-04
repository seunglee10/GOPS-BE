from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from alfaka.backfill.gapfill import TradingCalendar
from alfaka.backfill.runner import BackfillRunner
from alfaka.backfill.status import ACTIVE_STATUSES, RedisBackfillStore
from alfaka.serving.intervals import (
    backfill_target_bars,
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
            target_stored_count = int(payload_or_has_candles.get("targetStoredCount") or backfill_target_bars(source_interval))
            available_from = payload_or_has_candles.get("availableFrom")
            available_to = payload_or_has_candles.get("availableTo")
            target_range_from = payload_or_has_candles.get("targetRangeFrom")
            invalid_row_count = int(payload_or_has_candles.get("invalidRowCount") or 0)
            candles = payload_or_has_candles.get("candles") or []
            has_more_before = payload_or_has_candles.get("hasMoreBefore")
        else:
            returned_count = 1 if payload_or_has_candles else 0
            requested_limit = returned_count
            stored_count = returned_count
            target_stored_count = returned_count
            available_from = None
            available_to = None
            target_range_from = None
            invalid_row_count = 0
            candles = []
            has_more_before = None

        latest = self._latest_status(symbol, source_interval)
        backfill_status = latest.get("status") if latest else "not_requested"

        renderability = renderability_payload(interval, source_interval, candles, returned_count, stored_count)
        complete = complete_coverage(
            returned_count=returned_count,
            invalid_row_count=invalid_row_count,
            available_from=available_from,
            target_range_from=target_range_from,
            stored_count=stored_count,
            target_stored_count=target_stored_count,
            backfill_status=backfill_status,
            renderability=renderability,
        )
        can_backfill = can_request_backfill(backfill_status, complete)
        target_boundary = target_boundary_reached(
            has_more_before=has_more_before,
            returned_count=returned_count,
            renderability=renderability,
        )
        if complete:
            repair_status = "none"
            coverage = coverage_payload(
                state="complete",
                reason_code="coverage_complete",
                message=None,
                repair_status=repair_status,
                source_interval=source_interval,
                backfill_status=backfill_status,
                requested_limit=requested_limit,
                returned_count=returned_count,
                stored_count=stored_count,
                target_stored_count=target_stored_count,
                target_range_from=target_range_from,
                available_from=available_from,
                available_to=available_to,
                invalid_row_count=invalid_row_count,
                renderability=renderability,
            )
            return {
                "dataStatus": "ready",
                "backfillStatus": backfill_status,
                "repairStatus": repair_status,
                "canBackfill": False,
                "sourceInterval": source_interval,
                "message": None,
                "coverage": coverage,
            }

        if returned_count > 0:
            partial_message = latest.get("error") if latest and latest.get("status") in {"failed", "unavailable"} else None
            reason_code = coverage_reason(backfill_status, "stored_range_incomplete")
            if not renderability["renderable"]:
                reason_code = renderability["renderabilityReasonCode"] or "not_renderable"
            if target_boundary:
                reason_code = "target_boundary"
            repair_status = repair_status_for(
                complete=complete,
                backfill_status=backfill_status,
                requested_limit=requested_limit,
                returned_count=returned_count,
                renderability=renderability,
            )
            if target_boundary:
                repair_status = "none"
            data_status = "ready" if renderability["renderable"] and returned_count >= requested_limit else "partial"
            if target_boundary:
                message = partial_message or target_boundary_message(
                    symbol=symbol,
                    interval=interval,
                    source_interval=source_interval,
                )
            else:
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
                target_stored_count=target_stored_count,
                target_range_from=target_range_from,
                available_from=available_from,
                available_to=available_to,
                invalid_row_count=invalid_row_count,
                renderability=renderability,
            )
            return {
                "dataStatus": data_status,
                "backfillStatus": backfill_status,
                "repairStatus": repair_status,
                "canBackfill": False if target_boundary else can_backfill,
                "sourceInterval": source_interval,
                "message": message,
                "coverage": coverage,
            }

        message = latest.get("error") if latest and latest.get("status") in {"failed", "unavailable"} else None
        if not message:
            if backfill_status in ACTIVE_STATUSES:
                message = f"Preparing {source_interval} candle data for {symbol}."
            elif backfill_status == "succeeded":
                message = f"Backfill completed, but no stored {source_interval} candles were found for {symbol}."
            else:
                message = f"No stored {source_interval} candles were found for {symbol}."
        data_status = "empty" if can_backfill else "error" if backfill_status in {"failed", "unavailable"} else "empty"
        repair_status = repair_status_for(
            complete=complete,
            backfill_status=backfill_status,
            requested_limit=requested_limit,
            returned_count=returned_count,
            renderability=renderability,
        )
        coverage = coverage_payload(
            state="unavailable" if backfill_status in {"failed", "unavailable"} else "empty",
            reason_code=coverage_reason(backfill_status, "no_stored_candles"),
            message=message,
            repair_status=repair_status,
            source_interval=source_interval,
            backfill_status=backfill_status,
            requested_limit=requested_limit,
            returned_count=returned_count,
            stored_count=stored_count,
            target_stored_count=target_stored_count,
            target_range_from=target_range_from,
            available_from=available_from,
            available_to=available_to,
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
        mode: str = "default",
        force: bool = False,
    ) -> dict[str, Any]:
        interval = normalize_chart_interval(interval)
        source_interval = source_interval_for(interval)
        execution_mode = resolve_execution_mode(mode)
        try:
            record, deduplicated = self.store.create_request(symbol, source_interval, start=start, end=end, mode=execution_mode, force=force)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Backfill status store failed: {exc}") from exc

        if not deduplicated and execution_mode == "sync-dev":
            record = self.runner_factory().run(record)

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


def resolve_execution_mode(mode: str | None) -> str:
    requested = (mode or "default").strip()
    allow_requested_mode = os.getenv("BACKFILL_ALLOW_REQUESTED_MODE", "false").lower() in {"1", "true", "yes"}
    allowed_modes = {"queue", "sync-dev"}
    if allow_requested_mode and requested in allowed_modes:
        return requested
    configured = os.getenv("BACKFILL_EXECUTION_MODE", "queue")
    return configured if configured in allowed_modes else "queue"


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
        return "backfill_succeeded_without_complete_coverage"
    return default


def can_request_backfill(backfill_status: str, complete: bool) -> bool:
    return not complete and backfill_status not in ACTIVE_STATUSES


def repair_status_for(
    *,
    complete: bool,
    backfill_status: str,
    requested_limit: int,
    returned_count: int,
    renderability: dict[str, Any],
) -> str:
    if complete:
        return "none"
    if backfill_status in ACTIVE_STATUSES:
        return "gapfill_active"
    if backfill_status in {"failed", "unavailable"}:
        return "gapfill_failed"
    if renderability.get("renderable") and returned_count >= requested_limit:
        return "history_preload_required"
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
    target_stored_count: int,
    target_range_from: str | None,
    available_from: str | None,
    available_to: str | None,
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
        "targetStoredCount": target_stored_count,
        "targetRangeFrom": target_range_from,
        "availableFrom": available_from,
        "availableTo": available_to,
        "invalidRowCount": invalid_row_count,
        **renderability,
    }


def complete_coverage(
    *,
    returned_count: int,
    invalid_row_count: int,
    available_from: str | None,
    target_range_from: str | None,
    stored_count: int,
    target_stored_count: int,
    backfill_status: str,
    renderability: dict[str, Any],
) -> bool:
    if returned_count <= 0 or invalid_row_count > 0:
        return False

    has_range_coverage = bool(available_from and target_range_from and str(available_from) <= str(target_range_from))
    has_count_coverage = stored_count >= target_stored_count
    if has_range_coverage and has_count_coverage:
        return bool(renderability.get("renderable"))

    # Historical stock bars skip weekends/holidays and may begin at the next
    # trading session after the requested calendar boundary. Treat a dense,
    # renderable completed backfill as complete when it is only slightly short
    # of the naive calendar target.
    if backfill_status != "succeeded" or not renderability.get("renderable"):
        return False
    if stored_count < int(target_stored_count * 0.98):
        return False
    if not within_start_tolerance(available_from, target_range_from):
        return False
    return True


def renderability_payload(
    interval: str,
    source_interval: str,
    candles: list[dict[str, Any]],
    returned_count: int,
    stored_count: int,
) -> dict[str, Any]:
    minimum_returned_count = minimum_renderable_returned_bars(interval)
    minimum_source_bars = minimum_renderable_source_bars(source_interval)
    span_seconds = returned_span_seconds(candles)
    max_span_seconds = max_renderable_span_seconds(interval, returned_count)
    gap_ranges = gap_ranges_for_returned_window(interval, source_interval, candles)
    sparse_window = sparse_returned_window(interval, candles, returned_count, span_seconds, max_span_seconds)
    renderable = (
        returned_count >= minimum_returned_count and
        stored_count >= minimum_source_bars and
        not sparse_window
    )
    reason_code = None
    if returned_count <= 0:
        reason_code = "no_stored_candles"
    elif stored_count < minimum_source_bars:
        reason_code = "insufficient_source_bars"
    elif returned_count < minimum_returned_count:
        reason_code = "insufficient_returned_bars"
    elif sparse_window:
        reason_code = "returned_window_sparse"
    return {
        "renderable": renderable,
        "minimumReturnedCount": minimum_returned_count,
        "minimumRenderableSourceBars": minimum_source_bars,
        "returnedSpanSeconds": span_seconds,
        "maxRenderableSpanSeconds": max_span_seconds,
        "renderabilityReasonCode": reason_code,
        "gapRanges": gap_ranges,
    }


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
            "More historical candles are required before this interval can be rendered."
        )
    if reason == "returned_window_sparse":
        return (
            f"Available {interval} candles for {symbol} are too sparse across time to render as a continuous chart. "
            "Historical backfill or local data repair is required."
        )
    return (
        f"Loaded {returned_count} requested candles, but the stored {source_interval} target range is not complete yet. "
        "Historical backfill is required."
    )


def target_boundary_reached(*, has_more_before: Any, returned_count: int, renderability: dict[str, Any]) -> bool:
    return has_more_before is False and returned_count > 0 and not renderability.get("renderable")


def target_boundary_message(*, symbol: str, interval: str, source_interval: str) -> str:
    return (
        f"Reached the configured historical target boundary for {symbol} {interval}. "
        f"No earlier {source_interval} candles are in scope for backfill."
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
    return bool(intraday_gap_ranges(interval, candles))


def gap_ranges_for_returned_window(interval: str, source_interval: str, candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if interval in {"1m", "5m", "10m"}:
        return intraday_gap_ranges(interval, candles)
    if source_interval == "1D" and interval == "1D":
        return daily_gap_ranges(candles)
    return []


def intraday_gap_ranges(interval: str, candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    timestamps = sorted(
        timestamp for timestamp in (parse_time(candle.get("timestamp")) for candle in candles if candle.get("timestamp"))
        if timestamp is not None
    )
    if len(timestamps) < 2:
        return []
    allowed_gap = interval_seconds(interval) * 3
    bucket_delta = timedelta(seconds=interval_seconds(interval))
    calendar = TradingCalendar.from_environment()
    market_timezone = calendar.timezone
    ranges = []
    for previous, current in zip(timestamps, timestamps[1:]):
        gap_seconds = (current - previous).total_seconds()
        if gap_seconds <= allowed_gap:
            continue
        if same_regular_market_session_gap(previous, current, calendar, market_timezone):
            missing_start = previous + bucket_delta
            missing_count = max(1, int(gap_seconds // interval_seconds(interval)) - 1)
            ranges.append({
                "start": to_iso(missing_start),
                "end": to_iso(current),
                "missingCount": missing_count,
            })
    return ranges


def daily_gap_ranges(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    timestamps = sorted(
        timestamp for timestamp in (parse_time(candle.get("timestamp")) for candle in candles if candle.get("timestamp"))
        if timestamp is not None
    )
    if len(timestamps) < 2:
        return []
    calendar = TradingCalendar.from_environment()
    ranges = []
    for previous, current in zip(timestamps, timestamps[1:]):
        missing = missing_daily_sessions(previous, current, calendar)
        if not missing:
            continue
        ranges.append({
            "start": to_iso(missing[0]),
            "end": to_iso(missing[-1] + timedelta(days=1)),
            "missingCount": len(missing),
        })
    return ranges


def missing_daily_sessions(previous: datetime, current: datetime, calendar: TradingCalendar) -> list[datetime]:
    zone = calendar.timezone
    session_date = previous.astimezone(zone).date() + timedelta(days=1)
    end_date = current.astimezone(zone).date()
    missing = []
    while session_date < end_date:
        if calendar.is_session_date(session_date):
            missing.append(datetime.combine(session_date, datetime.min.time(), zone).astimezone(timezone.utc))
        session_date += timedelta(days=1)
    return missing


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


def within_start_tolerance(available_from: str | None, target_range_from: str | None) -> bool:
    available = parse_time(available_from)
    target = parse_time(target_range_from)
    if not available or not target:
        return False
    return (available - target).total_seconds() <= 7 * 24 * 60 * 60


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def get_backfill_service(provider=None) -> BackfillService:
    return BackfillService(provider=provider)
