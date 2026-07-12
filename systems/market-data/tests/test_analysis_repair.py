from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SHARED = ROOT / "systems" / "market-data" / "shared"
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

from alfaka.analytics.analysis_candles import _expected_intraday_keys, analysis_daily_window  # noqa: E402
from alfaka.analytics.analysis_repair import AnalysisCandleRepairService  # noqa: E402
from alfaka.backfill.gapfill import TradingCalendar  # noqa: E402
from alfaka.backfill.runner import BackfillDeadlineExceeded, BackfillUnavailable  # noqa: E402
from alfaka.common import s3_client as s3_client_module  # noqa: E402
from alfaka.common.trading_calendar import configured_closed_dates, is_us_equity_session_date  # noqa: E402


NOW = datetime(2026, 7, 11, tzinfo=timezone.utc)


def test_shared_calendar_is_year_aware_and_includes_exceptional_closures():
    assert is_us_equity_session_date(datetime(2021, 6, 18).date())
    assert not is_us_equity_session_date(datetime(2021, 12, 31).date())
    assert not is_us_equity_session_date(datetime(2022, 6, 20).date())
    assert not is_us_equity_session_date(datetime(2025, 1, 9).date())
    assert "2024-07-04" in configured_closed_dates(2024, 2024)
    assert "2024-07-05" not in configured_closed_dates(2024, 2024)


def test_analysis_s3_client_bounds_one_operation_within_stage_deadline(monkeypatch):
    captured = {}

    def fake_client(_service, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(s3_client_module.boto3, "client", fake_client)
    s3_client_module.create_s3_client(operation_timeout_seconds=45)

    config = captured["config"]
    assert config.connect_timeout == 3
    assert config.read_timeout == 10
    assert config.retries == {"total_max_attempts": 1, "mode": "standard"}


@pytest.mark.parametrize("interval,expected_bars", [("1D", 500), ("1W", 312), ("1M", 72)])
def test_analysis_daily_window_uses_completed_interval_horizon(interval, expected_bars):
    window = analysis_daily_window((interval,), now=NOW)
    if interval == "1D":
        assert len(window.expected_keys) == expected_bars
        assert window.expected_keys[-1] == "2026-07-10"
    elif interval == "1W":
        assert window.start.startswith("2020-07-20T")
        assert window.end.startswith("2026-07-13T")
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


def test_invalid_ohlcv_does_not_count_as_ready_daily_candle():
    rows = full_rows()
    invalid_key = rows[-1]["timestamp"][:10]
    rows[-1]["close"] = rows[-1]["high"] + 1
    service = AnalysisCandleRepairService(
        provider=MutableProvider(rows),
        runner_factory=lambda: MutatingRunner(MutableProvider(rows), {}),
        now_provider=lambda: NOW,
        enabled=True,
        alpaca_enabled=False,
    )

    audit = service.audit("AAPL", ("1D",))

    assert audit.missing_bars == 1
    assert audit.ranges[-1].missing_keys == (invalid_key,)


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


def test_status_materialized_rows_counts_repaired_missing_bars_not_shared_object_rows():
    rows = full_rows()
    missing = rows.pop()
    provider = MutableProvider(rows)
    runner = MutatingRunner(provider, {"s3-only": [missing]}, reported_rows=200)
    service = repair_service(provider, runner, alpaca_enabled=False)

    result = service.ensure_ready("AAPL", ("1D",), request_id="cab-test")

    assert result.reason == "repaired"
    assert result.materialized_rows == 1


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


def test_s3_repair_batches_all_missing_ranges_and_emits_ephemeral_metrics_once():
    rows = full_rows()
    missing = [rows[20], rows[80]]
    provider = MutableProvider([row for row in rows if row not in missing])
    metrics = {
        "listCalls": 1,
        "objectsListed": 2,
        "manifestObjectsRead": 2,
        "objectsSelected": 2,
        "objectGets": 2,
        "elapsedMs": 14,
    }
    runner = MutatingRunner(provider, {"s3-only": missing}, metrics=metrics)
    events = []
    service = repair_service(provider, runner, alpaca_enabled=True)

    result = service.ensure_ready(
        "AAPL",
        ("1D",),
        request_id="cab-test",
        on_event=lambda stage, payload: events.append((stage, payload)),
    )

    assert result.reason == "repaired"
    assert len(runner.calls) == 1
    assert len(runner.calls[0]["analysisRepairRanges"]) == 2
    completed = [payload for stage, payload in events if stage == "s3" and payload["status"] == "completed"]
    assert len(completed) == 1
    assert completed[0]["metrics"] == metrics
    assert completed[0]["materializedRows"] == 2
    assert "metrics" not in result.to_dict()
    assert "ranges" not in result.to_dict()


def test_s3_timeout_falls_through_to_alpaca_and_keeps_metrics_ephemeral():
    rows = full_rows()
    missing = rows[-1]
    provider = MutableProvider(rows[:-1])
    runner = TimeoutThenAlpacaRunner(provider, missing)
    events = []
    service = repair_service(provider, runner, alpaca_enabled=True)

    result = service.ensure_ready(
        "AAPL",
        ("1D",),
        request_id="cab-test",
        on_event=lambda stage, payload: events.append((stage, payload)),
    )

    assert result.reason == "repaired"
    assert result.materialized_rows == 1
    assert [item["sourcePreference"] for item in runner.calls] == ["s3-only", "alpaca-only"]
    timeout_event = next(
        payload
        for stage, payload in events
        if stage == "s3" and payload.get("reasonCode") == "s3_timeout"
    )
    assert timeout_event["metrics"] == {"listCalls": 1, "objectsListed": 3, "elapsedMs": 45_001}


def test_s3_timeout_is_compact_terminal_reason_when_local_alpaca_is_disabled():
    rows = full_rows()
    provider = MutableProvider(rows[:-1])
    runner = TimeoutThenAlpacaRunner(provider, rows[-1])
    service = repair_service(provider, runner, alpaca_enabled=False)

    result = service.ensure_ready("AAPL", ("1D",), request_id="cab-test")

    assert result.reason == "s3_timeout"
    assert result.unavailable
    assert [item["sourcePreference"] for item in runner.calls] == ["s3-only"]


def test_s3_stage_wall_clock_timeout_returns_without_waiting_for_blocked_runner():
    rows = full_rows()
    provider = MutableProvider(rows[:-1])
    runner = SlowCancelableRunner()
    service = AnalysisCandleRepairService(
        provider=provider,
        runner_factory=lambda: runner,
        now_provider=lambda: NOW,
        enabled=True,
        alpaca_enabled=False,
        max_ranges=8,
        concurrency=1,
        s3_timeout_seconds=0.02,
    )

    started = time.monotonic()
    result = service.ensure_ready("AAPL", ("1D",), request_id="cab-timeout")
    elapsed = time.monotonic() - started

    assert result.reason == "s3_timeout"
    assert elapsed < 0.15
    assert runner.cancel_seen.wait(0.3)
    assert not runner.commit_seen.is_set()


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
    assert {key for item in audit.ranges for key in item.missing_keys} == {
        row["timestamp"][:10] for index, row in enumerate(rows) if index % 4 == 0
    }


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


def test_intraday_gap_uses_direct_alpaca_repair_without_s3_lookup():
    expected = _expected_intraday_keys(NOW, 500, "5m", TradingCalendar.from_environment())
    rows = [intraday_row(timestamp, "5m") for timestamp in expected]
    missing = rows.pop(-2)
    provider = MixedIntervalProvider({"5m": rows})
    runner = IntradayMutatingRunner(provider, missing)
    service = AnalysisCandleRepairService(
        provider=provider,
        runner_factory=lambda: runner,
        now_provider=lambda: NOW,
        enabled=True,
        alpaca_enabled=True,
        max_ranges=8,
        concurrency=1,
    )

    result = service.ensure_ready("AAPL", ("5m",), request_id="cab-intraday")

    assert result.reason == "repaired"
    assert result.materialized_rows == 1
    assert [item["sourcePreference"] for item in runner.calls] == ["alpaca-only"]
    assert runner.calls[0]["interval"] == "5m"
    assert runner.calls[0]["range"]["start"] == missing["timestamp"]


class MutableProvider:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def daily_candles(self, _symbol, **kwargs):
        self.calls.append(kwargs)
        return list(self.rows)


class MixedIntervalProvider:
    def __init__(self, rows):
        self.rows = {key: list(value) for key, value in rows.items()}

    def stored_interval_candles(self, _symbol, interval, **_kwargs):
        return list(self.rows.get(interval, []))


class IntradayMutatingRunner:
    def __init__(self, provider, missing):
        self.provider = provider
        self.missing = missing
        self.calls = []

    def run(self, record):
        self.calls.append(record)
        assert record["sourcePreference"] == "alpaca-only"
        self.provider.rows[record["interval"]].append(self.missing)
        return {"status": "succeeded", "result": {"materializedRowCount": 1}}


class MutatingRunner:
    def __init__(self, provider, additions, *, metrics=None, reported_rows=None):
        self.provider = provider
        self.additions = {key: list(value) for key, value in additions.items()}
        self.metrics = dict(metrics or {})
        self.reported_rows = reported_rows
        self.calls = []

    def run(self, record):
        self.calls.append(record)
        source = record["sourcePreference"]
        additions = self.additions.pop(source, [])
        if not additions:
            message = "Historical provider returned no bars." if source == "alpaca-only" else "No S3 final candle objects are available."
            raise BackfillUnavailable(message)
        self.provider.rows.extend(additions)
        return {
            "status": "succeeded",
            "result": {
                "materializedRowCount": self.reported_rows if self.reported_rows is not None else len(additions),
                "lookupMetrics": self.metrics,
            },
        }


class TimeoutThenAlpacaRunner:
    def __init__(self, provider, missing):
        self.provider = provider
        self.missing = missing
        self.calls = []

    def run(self, record):
        self.calls.append(record)
        if record["sourcePreference"] == "s3-only":
            raise BackfillDeadlineExceeded(
                "S3 analysis repair exceeded its stage deadline.",
                metrics={"listCalls": 1, "objectsListed": 3, "elapsedMs": 45_001},
            )
        self.provider.rows.append(self.missing)
        return {"status": "succeeded", "result": {"materializedRowCount": 1}}


class SlowCancelableRunner:
    def __init__(self):
        from threading import Event

        self.cancel_seen = Event()
        self.commit_seen = Event()

    def prepare_analysis_s3_repair(self, record):
        metrics = record["_lookupMetrics"]
        metrics.update({"listCalls": 1, "objectsListed": 1})
        deadline = time.monotonic() + 0.25
        while time.monotonic() < deadline:
            if record["_cancelCheck"]():
                self.cancel_seen.set()
                raise BackfillUnavailable("Analysis repair was canceled.")
            time.sleep(0.005)
        raise AssertionError("timeout cancellation was not delivered")

    def commit_analysis_s3_repair(self, outcome):
        self.commit_seen.set()
        return outcome


def intraday_row(timestamp, interval):
    return {
        "timestamp": timestamp,
        "open": 100,
        "high": 102,
        "low": 99,
        "close": 101,
        "volume": 1_000,
        "isClosed": True,
        "canonicalVersion": "v2",
        "priceAdjustment": "split",
        "marketSession": "regular",
        "sourceClass": "clickhouse_direct",
        "interval": interval,
    }


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
