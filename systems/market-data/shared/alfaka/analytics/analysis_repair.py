from __future__ import annotations

import os
import threading
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from alfaka.backfill.runner import BackfillRunner, BackfillUnavailable
from alfaka.backfill.gapfill import TradingCalendar
from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider
from alfaka.storage.candle_validation import invalid_candle_numeric_reason

from .analysis_candles import (
    ANALYSIS_INTERVALS,
    INTRADAY_ANALYSIS_INTERVALS,
    _expected_intraday_keys,
    analysis_daily_window,
    canonicalize_candle_identity,
)
from .schema import LOOKBACK_BARS


MARKET_TIMEZONE = ZoneInfo("America/New_York")
RepairEventHandler = Callable[[str, dict[str, Any]], None]
CancelCheck = Callable[[], bool]


@dataclass(frozen=True)
class AnalysisRepairRange:
    kind: str
    start: str
    end: str
    missing_count: int
    missing_keys: tuple[str, ...] = ()
    interval: str = "1D"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "start": self.start,
            "end": self.end,
            "missingCount": self.missing_count,
            "interval": self.interval,
        }


@dataclass(frozen=True)
class AnalysisReadinessAudit:
    expected_bars: int
    actual_bars: int
    missing_bars: int
    available_from: str | None
    available_to: str | None
    ranges: tuple[AnalysisRepairRange, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "expectedBars": self.expected_bars,
            "actualBars": self.actual_bars,
            "missingBars": self.missing_bars,
            "availableFrom": self.available_from,
            "availableTo": self.available_to,
            "ranges": [item.to_dict() for item in self.ranges],
        }


@dataclass(frozen=True)
class AnalysisRepairResult:
    checked: bool
    attempted: bool
    repaired: bool
    unavailable: bool
    missing_before: int
    missing_after: int
    materialized_rows: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AnalysisCandleRepairService:
    def __init__(
        self,
        *,
        provider: Any | None = None,
        runner_factory: Callable[[], Any] | None = None,
        now_provider: Callable[[], datetime] | None = None,
        enabled: bool | None = None,
        alpaca_enabled: bool | None = None,
        max_ranges: int | None = None,
        concurrency: int | None = None,
    ):
        self.provider = provider or ClickHouseMarketDataProvider()
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.enabled = _env_bool("CHART_ASSET_REPAIR_ENABLED", True) if enabled is None else bool(enabled)
        self.alpaca_enabled = _env_bool("CHART_ASSET_REPAIR_ALPACA_ENABLED", True) if alpaca_enabled is None else bool(alpaca_enabled)
        self.max_ranges = max(1, int(max_ranges or os.getenv("CHART_ASSET_REPAIR_MAX_RANGES", "8")))
        self.runner_factory = runner_factory or (lambda: BackfillRunner(store=None))
        self._semaphore = threading.BoundedSemaphore(max(1, int(concurrency or os.getenv("CHART_ASSET_REPAIR_CONCURRENCY", "2"))))

    def ensure_ready(
        self,
        symbol: str,
        intervals: Iterable[str],
        *,
        request_id: str,
        on_event: RepairEventHandler | None = None,
        is_cancel_requested: CancelCheck | None = None,
    ) -> AnalysisRepairResult:
        if not self.enabled:
            return AnalysisRepairResult(False, False, False, False, 0, 0, 0, "repair_disabled")
        cancel = is_cancel_requested or (lambda: False)
        emit = on_event or (lambda _stage, _payload: None)
        before = self.audit(symbol, intervals)
        emit("audit", before.to_dict())
        if before.missing_bars == 0:
            return AnalysisRepairResult(True, False, False, False, 0, 0, 0, "coverage_complete")
        if cancel():
            return AnalysisRepairResult(True, False, False, True, before.missing_bars, before.missing_bars, 0, "canceled")

        alpaca_error = False
        with self._semaphore:
            runner = self.runner_factory()
            if self.alpaca_enabled and not cancel():
                _alpaca_materialized, alpaca_error, _alpaca_empty_head = self._repair_ranges(
                    runner,
                    symbol, before.ranges,
                    source_preference="alpaca-only",
                    request_id=request_id,
                    stage="alpaca",
                    emit=emit,
                    cancel=cancel,
                )

        after = self.audit(symbol, intervals)
        emit("final", after.to_dict())
        if cancel():
            reason = "canceled"
        elif after.missing_bars == 0:
            reason = "repaired"
        elif not self.alpaca_enabled:
            reason = "alpaca_disabled"
        elif _partial_history_is_usable(after):
            reason = "partial_listing_history"
        elif alpaca_error:
            reason = "alpaca_unavailable"
        else:
            reason = "candle_repair_incomplete"
        return AnalysisRepairResult(
            checked=True,
            attempted=True,
            repaired=after.missing_bars == 0,
            unavailable=after.missing_bars > 0 and not _partial_history_is_usable(after),
            missing_before=before.missing_bars,
            missing_after=after.missing_bars,
            # Stage runners may read shared objects or merged request ranges.
            # The durable status should report only missing canonical bars that
            # became available for this symbol during this request.
            materialized_rows=max(0, before.missing_bars - after.missing_bars),
            reason=reason,
        )

    def audit(self, symbol: str, intervals: Iterable[str]) -> AnalysisReadinessAudit:
        requested = tuple(dict.fromkeys(intervals))
        if not requested or set(requested).difference(ANALYSIS_INTERVALS):
            raise ValueError("Unsupported analysis intervals")
        now = self.now_provider()
        expected_count = actual_count = missing_count = 0
        present_keys: list[str] = []
        ranges: list[AnalysisRepairRange] = []
        daily_intervals = tuple(item for item in requested if item not in INTRADAY_ANALYSIS_INTERVALS)
        if daily_intervals:
            window = analysis_daily_window(daily_intervals, now=now)
            rows = self.provider.daily_candles(
                symbol,
                interval="1D",
                limit=max(1, len(window.expected_keys) + 16),
                from_time=window.start,
                before=window.end,
            )
            actual = _valid_daily_keys(rows)
            expected = list(window.expected_keys)
            present = [key for key in expected if key in actual]
            expected_count += len(expected)
            actual_count += len(present)
            missing_count += len(expected) - len(present)
            present_keys.extend(present)
            ranges.extend(_bounded_missing_ranges(expected, actual, self.max_ranges))
        trading_calendar = TradingCalendar.from_environment()
        for interval in requested:
            if interval not in INTRADAY_ANALYSIS_INTERVALS:
                continue
            expected = _expected_intraday_keys(now, LOOKBACK_BARS[interval], interval, trading_calendar)
            rows = self.provider.stored_interval_candles(
                symbol, interval, limit=LOOKBACK_BARS[interval] + 16,
            )
            actual = _valid_interval_keys(rows, interval)
            present = [key for key in expected if key in actual]
            expected_count += len(expected)
            actual_count += len(present)
            missing_count += len(expected) - len(present)
            present_keys.extend(present)
            ranges.extend(_bounded_intraday_ranges(expected, actual, interval, self.max_ranges))
        present_keys.sort()
        return AnalysisReadinessAudit(
            expected_bars=expected_count,
            actual_bars=actual_count,
            missing_bars=missing_count,
            available_from=present_keys[0] if present_keys else None,
            available_to=present_keys[-1] if present_keys else None,
            ranges=tuple(ranges),
        )

    @staticmethod
    def _repair_ranges(
        runner: Any,
        symbol: str,
        ranges: Iterable[AnalysisRepairRange],
        *,
        source_preference: str,
        request_id: str,
        stage: str,
        emit: RepairEventHandler,
        cancel: CancelCheck,
    ) -> tuple[int, bool, bool]:
        materialized = 0
        unavailable = False
        empty_head = False
        for index, repair_range in enumerate(ranges):
            if cancel():
                break
            emit(stage, {"status": "started", "range": repair_range.to_dict()})
            try:
                outcome = runner.run({
                    "schemaVersion": 1,
                    "requestId": f"{request_id}-{stage}-{index}",
                    "symbol": symbol,
                    "interval": repair_range.interval,
                    "range": {"start": repair_range.start, "end": repair_range.end},
                    "jobType": "gapfill",
                    "sourcePreference": source_preference,
                    "mode": "inline",
                    "force": False,
                    "analysisMissingCandleKeys": list(repair_range.missing_keys),
                })
                result = outcome.get("result") if isinstance(outcome, dict) and isinstance(outcome.get("result"), dict) else outcome
                rows = int((result or {}).get("materializedRowCount") or 0)
                materialized += rows
                emit(stage, {"status": "completed", "materializedRows": rows, "range": repair_range.to_dict()})
            except BackfillUnavailable as exc:
                unavailable = True
                message = str(exc)
                if stage == "alpaca" and repair_range.kind == "missing_head" and "returned no bars" in message.lower():
                    empty_head = True
                emit(stage, {"status": "unavailable", "reason": _compact_reason(exc), "range": repair_range.to_dict()})
            except Exception as exc:
                unavailable = True
                emit(stage, {"status": "failed", "reason": _compact_reason(exc), "range": repair_range.to_dict()})
        return materialized, unavailable, empty_head


def _valid_daily_keys(rows: Iterable[dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    for row in rows:
        if row.get("isClosed", row.get("is_closed", True)) is False:
            continue
        normalized = canonicalize_candle_identity(row, "1D")
        if normalized is None:
            continue
        if invalid_candle_numeric_reason(normalized, require=True):
            continue
        result.add(str(normalized["candleKey"]))
    return result


def _valid_interval_keys(rows: Iterable[dict[str, Any]], interval: str) -> set[str]:
    result: set[str] = set()
    for row in rows:
        if row.get("isClosed", row.get("is_closed", True)) is False:
            continue
        normalized = canonicalize_candle_identity(row, interval)
        if normalized is None or invalid_candle_numeric_reason(normalized, require=True):
            continue
        result.add(str(normalized["candleKey"]))
    return result


def _bounded_missing_ranges(expected: list[str], actual: set[str], max_ranges: int) -> list[AnalysisRepairRange]:
    groups: list[list[int]] = []
    for index, key in enumerate(expected):
        if key in actual:
            continue
        if groups and groups[-1][1] == index - 1:
            groups[-1][1] = index
        else:
            groups.append([index, index])
    while len(groups) > max_ranges:
        merge_at = min(range(len(groups) - 1), key=lambda index: groups[index + 1][0] - groups[index][1])
        groups[merge_at:merge_at + 2] = [[groups[merge_at][0], groups[merge_at + 1][1]]]
    result: list[AnalysisRepairRange] = []
    for start_index, end_index in groups:
        kind = "missing_head" if start_index == 0 else "stale_tail" if end_index == len(expected) - 1 else "interior_gap"
        result.append(AnalysisRepairRange(
            kind=kind,
            start=_market_midnight(expected[start_index]),
            end=_market_midnight((date.fromisoformat(expected[end_index]) + timedelta(days=1)).isoformat()),
            missing_count=sum(1 for key in expected[start_index:end_index + 1] if key not in actual),
            missing_keys=tuple(key for key in expected[start_index:end_index + 1] if key not in actual),
            interval="1D",
        ))
    return result


def _bounded_intraday_ranges(
    expected: list[str], actual: set[str], interval: str, max_ranges: int,
) -> list[AnalysisRepairRange]:
    groups: list[list[int]] = []
    for index, key in enumerate(expected):
        if key in actual:
            continue
        if groups and groups[-1][1] == index - 1:
            groups[-1][1] = index
        else:
            groups.append([index, index])
    while len(groups) > max_ranges:
        merge_at = min(range(len(groups) - 1), key=lambda index: groups[index + 1][0] - groups[index][1])
        groups[merge_at:merge_at + 2] = [[groups[merge_at][0], groups[merge_at + 1][1]]]
    step_minutes = {"1m": 1, "5m": 5, "10m": 10, "1h": 60, "4h": 240}[interval]
    result = []
    for start_index, end_index in groups:
        missing_keys = tuple(key for key in expected[start_index:end_index + 1] if key not in actual)
        start = datetime.fromisoformat(expected[start_index].replace("Z", "+00:00"))
        end = datetime.fromisoformat(expected[end_index].replace("Z", "+00:00")) + timedelta(minutes=step_minutes)
        kind = "missing_head" if start_index == 0 else "stale_tail" if end_index == len(expected) - 1 else "interior_gap"
        result.append(AnalysisRepairRange(
            kind=kind,
            start=start.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            end=end.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            missing_count=len(missing_keys),
            missing_keys=missing_keys,
            interval=interval,
        ))
    return result


def _market_midnight(value: str) -> str:
    parsed = date.fromisoformat(value)
    return datetime.combine(parsed, time.min, MARKET_TIMEZONE).astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _compact_reason(exc: Exception) -> str:
    message = " ".join(str(exc).split())[:160]
    return f"{exc.__class__.__name__}: {message}" if message else exc.__class__.__name__


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _partial_history_is_usable(audit: AnalysisReadinessAudit) -> bool:
    return audit.actual_bars >= 120 and bool(audit.ranges) and all(item.kind == "missing_head" for item in audit.ranges)
