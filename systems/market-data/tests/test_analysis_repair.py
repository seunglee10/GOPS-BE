from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SHARED = ROOT / "systems" / "market-data" / "shared"
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

from alfaka.analytics.analysis_candles import analysis_daily_window  # noqa: E402
from alfaka.analytics.analysis_repair import AnalysisCandleRepairService  # noqa: E402
from alfaka.backfill.runner import BackfillUnavailable  # noqa: E402
from alfaka.common.trading_calendar import configured_closed_dates, is_us_equity_session_date  # noqa: E402


NOW = datetime(2026, 7, 11, tzinfo=timezone.utc)


def test_shared_calendar_is_year_aware_and_includes_exceptional_closures():
    assert is_us_equity_session_date(datetime(2021, 6, 18).date())
    assert not is_us_equity_session_date(datetime(2021, 12, 31).date())
    assert not is_us_equity_session_date(datetime(2022, 6, 20).date())
    assert not is_us_equity_session_date(datetime(2025, 1, 9).date())
    assert "2024-07-04" in configured_closed_dates(2024, 2024)
    assert "2024-07-05" not in configured_closed_dates(2024, 2024)


@pytest.mark.parametrize("interval,expected_bars", [("1D", 500), ("1W", 312), ("1M", 72)])
def test_analysis_daily_window_uses_completed_interval_horizon(interval, expected_bars):
    window = analysis_daily_window((interval,), now=NOW)
    if interval == "1D":
        assert len(window.expected_keys) == expected_bars
        assert window.expected_keys[-1] == "2026-07-10"
    elif interval == "1W":
        assert window.start.startswith("2020-07-13T")
        assert window.end.startswith("2026-07-06T")
    else:
        assert window.start.startswith("2020-07-01T")
        assert window.end.startswith("2026-07-01T")


def test_complete_readiness_does_not_construct_backfill_runner():
    provider = MutableProvider(full_rows())
    service = AnalysisCandleRepairService(
        provider=provider,
        runner_factory=lambda: (_ for _ in ()).throw(AssertionError("runner must not be constructed")),
        now_provider=lambda: NOW,
        enabled=True,
        alpaca_enabled=True,
    )
    result = service.ensure_ready("AAPL", ("1D",), request_id="cab-test")
    assert result.reason == "coverage_complete"
    assert not result.attempted


def test_s3_repair_completes_without_alpaca():
    rows = full_rows()
    missing = rows.pop()
    provider = MutableProvider(rows)
    runner = MutatingRunner(provider, {"s3-only": [missing]})
    service = repair_service(provider, runner, alpaca_enabled=True)
    result = service.ensure_ready("AAPL", ("1D",), request_id="cab-test")
    assert result.reason == "repaired"
    assert result.materialized_rows == 1
    assert [item["sourcePreference"] for item in runner.calls] == ["s3-only"]


def test_s3_partial_then_alpaca_repairs_only_remaining_range():
    rows = full_rows()
    first, second = rows[-2:]
    provider = MutableProvider(rows[:-2])
    runner = MutatingRunner(provider, {"s3-only": [first], "alpaca-only": [second]})
    service = repair_service(provider, runner, alpaca_enabled=True)
    result = service.ensure_ready("AAPL", ("1D",), request_id="cab-test")
    assert result.reason == "repaired"
    assert result.materialized_rows == 2
    assert [item["sourcePreference"] for item in runner.calls] == ["s3-only", "alpaca-only"]


def test_empty_alpaca_head_is_bounded_listing_history_result():
    rows = full_rows()[5:]
    provider = MutableProvider(rows)
    runner = MutatingRunner(provider, {})
    service = repair_service(provider, runner, alpaca_enabled=True)
    result = service.ensure_ready("AAPL", ("1D",), request_id="cab-test")
    assert result.reason == "insufficient_listing_history"
    assert result.missing_after == 5
    assert len(runner.calls) == 2


def test_missing_range_count_is_bounded():
    rows = full_rows()
    provider = MutableProvider([row for index, row in enumerate(rows) if index % 4])
    service = AnalysisCandleRepairService(
        provider=provider,
        runner_factory=lambda: MutatingRunner(provider, {}),
        now_provider=lambda: NOW,
        enabled=True,
        alpaca_enabled=False,
        max_ranges=3,
    )
    audit = service.audit("AAPL", ("1D",))
    assert len(audit.ranges) == 3
    assert sum(item.missing_count for item in audit.ranges) == audit.missing_bars


def test_alpaca_disabled_stops_after_s3_and_returns_compact_reason():
    provider = MutableProvider(full_rows()[1:])
    runner = MutatingRunner(provider, {})
    service = repair_service(provider, runner, alpaca_enabled=False)
    result = service.ensure_ready("AAPL", ("1D",), request_id="cab-test")
    assert result.reason == "alpaca_disabled"
    assert result.unavailable
    assert [item["sourcePreference"] for item in runner.calls] == ["s3-only"]


def test_cancel_after_audit_does_not_construct_runner():
    provider = MutableProvider(full_rows()[1:])
    service = AnalysisCandleRepairService(
        provider=provider,
        runner_factory=lambda: (_ for _ in ()).throw(AssertionError("runner must not be constructed")),
        now_provider=lambda: NOW,
        enabled=True,
        alpaca_enabled=True,
    )
    result = service.ensure_ready(
        "AAPL", ("1D",), request_id="cab-test", is_cancel_requested=lambda: True,
    )
    assert result.reason == "canceled"
    assert not result.attempted


class MutableProvider:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def daily_candles(self, _symbol, **kwargs):
        self.calls.append(kwargs)
        return list(self.rows)


class MutatingRunner:
    def __init__(self, provider, additions):
        self.provider = provider
        self.additions = {key: list(value) for key, value in additions.items()}
        self.calls = []

    def run(self, record):
        self.calls.append(record)
        source = record["sourcePreference"]
        additions = self.additions.pop(source, [])
        if not additions:
            message = "Historical provider returned no bars." if source == "alpaca-only" else "No S3 final candle objects are available."
            raise BackfillUnavailable(message)
        self.provider.rows.extend(additions)
        return {"status": "succeeded", "result": {"materializedRowCount": len(additions)}}


def repair_service(provider, runner, *, alpaca_enabled):
    return AnalysisCandleRepairService(
        provider=provider,
        runner_factory=lambda: runner,
        now_provider=lambda: NOW,
        enabled=True,
        alpaca_enabled=alpaca_enabled,
        max_ranges=8,
        concurrency=1,
    )


def full_rows():
    window = analysis_daily_window(("1D",), now=NOW)
    return [candle(key) for key in window.expected_keys]


def candle(key):
    return {
        "symbol": "AAPL",
        "timestamp": f"{key}T00:00:00.000Z",
        "open": 100,
        "high": 102,
        "low": 99,
        "close": 101,
        "volume": 1000,
        "isClosed": True,
    }
