from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SHARED = ROOT / "systems" / "market-data" / "shared"
if str(SHARED) not in sys.path: sys.path.insert(0, str(SHARED))

from alfaka.analytics.analysis_candles import _expected_intraday_keys, analysis_daily_window  # noqa: E402
from alfaka.analytics.analysis_repair import (  # noqa: E402
    AlpacaClickHouseRepairRunner,
    AnalysisCandleRepairService,
)
from alfaka.backfill.gapfill import TradingCalendar  # noqa: E402
from alfaka.backfill.runner import BackfillUnavailable  # noqa: E402


NOW = datetime(2026, 7, 11, tzinfo=timezone.utc)


@pytest.mark.parametrize("interval,expected_bars", [("1D", 380), ("1W", 312)])
def test_analysis_daily_window_uses_geometry_target_horizon(interval, expected_bars):
    window = analysis_daily_window((interval,), now=NOW)
    if interval == "1D":
        assert len(window.expected_keys) == expected_bars
    else:
        weeks = (datetime.fromisoformat(window.end.replace("Z", "+00:00")) - datetime.fromisoformat(window.start.replace("Z", "+00:00"))).days // 7
        assert weeks == expected_bars


def test_daily_repair_requests_only_missing_range_from_alpaca_and_rechecks_clickhouse():
    expected = list(analysis_daily_window(("1D",), now=NOW).expected_keys)
    missing = expected[-3]
    provider = Provider(daily=[_daily(key) for key in expected if key != missing])
    runner = Runner(provider)
    service = _service(provider, runner)

    result = service.ensure_ready("NVDA", ("1D",), request_id="cab-test")

    assert result.reason == "repaired"
    assert result.materialized_rows == 1
    assert len(runner.calls) == 1
    assert runner.calls[0]["sourcePreference"] == "alpaca-only"
    assert runner.calls[0]["interval"] == "1D"
    assert runner.calls[0]["analysisMissingCandleKeys"] == [missing]


def test_intraday_repair_uses_selected_interval_and_exact_canonical_keys():
    expected = _expected_intraday_keys(NOW, 380, "5m", TradingCalendar.from_environment())
    missing = expected[-2]
    provider = Provider(intraday={"5m": [_intraday(key, "5m") for key in expected if key != missing]})
    runner = Runner(provider)
    service = _service(provider, runner)

    result = service.ensure_ready("NVDA", ("5m",), request_id="cab-test")

    assert result.reason == "repaired"
    assert runner.calls[0]["interval"] == "5m"
    assert runner.calls[0]["analysisMissingCandleKeys"] == [missing]


def test_weekly_repair_audits_and_materializes_underlying_daily_candles():
    window = analysis_daily_window(("1W",), now=NOW)
    expected = list(window.expected_keys)
    missing = expected[-1]
    provider = Provider(daily=[_daily(key) for key in expected[:-1]])
    runner = Runner(provider)
    service = _service(provider, runner)

    result = service.ensure_ready("NVDA", ("1W",), request_id="cab-test")

    assert result.reason == "repaired"
    assert runner.calls[0]["interval"] == "1D"
    assert runner.calls[0]["analysisMissingCandleKeys"] == [missing]


def test_new_listing_with_120_contiguous_bars_is_usable_partial_coverage():
    expected = list(analysis_daily_window(("1D",), now=NOW).expected_keys)
    provider = Provider(daily=[_daily(key) for key in expected[-120:]])
    runner = EmptyRunner()
    service = _service(provider, runner)

    result = service.ensure_ready("NEW", ("1D",), request_id="cab-test")

    assert result.reason == "partial_listing_history"
    assert not result.unavailable
    assert result.missing_after == 260
    assert all(call["sourcePreference"] == "alpaca-only" for call in runner.calls)


def test_interior_gap_remaining_after_alpaca_failure_is_unavailable():
    expected = list(analysis_daily_window(("1D",), now=NOW).expected_keys)
    provider = Provider(daily=[_daily(key) for index, key in enumerate(expected) if index != 200])
    service = _service(provider, EmptyRunner())

    result = service.ensure_ready("NVDA", ("1D",), request_id="cab-test")

    assert result.unavailable
    assert result.reason == "alpaca_unavailable"


def test_default_repair_runner_writes_alpaca_candles_directly_to_clickhouse():
    client = ClickHouseClient()
    runner = AlpacaClickHouseRepairRunner(
        clickhouse_client=client,
        fetcher=lambda *_args, **_kwargs: [{
            "t": "2026-07-10T13:35:00.000Z",
            "o": 100,
            "h": 102,
            "l": 99,
            "c": 101,
            "v": 1000,
            "n": 25,
            "vw": 100.5,
        }],
    )

    outcome = runner.run({
        "requestId": "cab-direct",
        "symbol": "NVDA",
        "interval": "5m",
        "range": {"start": "2026-07-10T13:35:00.000Z", "end": "2026-07-10T13:40:00.000Z"},
        "analysisMissingCandleKeys": ["2026-07-10T13:35:00.000Z"],
    })

    assert outcome["result"]["materializedRowCount"] == 1
    assert "processedObjects" not in outcome["result"]
    assert client.inserts[0][0] == "chart_candles"
    assert client.inserts[0][1][0]["interval"] == "5m"


class ClickHouseClient:
    def __init__(self): self.inserts = []
    def insert_json_each_row(self, table, rows): self.inserts.append((table, rows))


class Provider:
    def __init__(self, *, daily=None, intraday=None):
        self.daily = list(daily or [])
        self.intraday = {key: list(value) for key, value in (intraday or {}).items()}

    def daily_candles(self, *_args, **_kwargs): return list(self.daily)
    def stored_interval_candles(self, _symbol, interval, **_kwargs): return list(self.intraday.get(interval, []))


class Runner:
    def __init__(self, provider): self.provider = provider; self.calls = []
    def run(self, record):
        self.calls.append(record)
        interval = record["interval"]
        keys = record.get("analysisMissingCandleKeys") or []
        if interval == "1D": self.provider.daily.extend(_daily(key) for key in keys)
        else: self.provider.intraday.setdefault(interval, []).extend(_intraday(key, interval) for key in keys)
        return {"result": {"materializedRowCount": len(keys)}}


class EmptyRunner:
    def __init__(self): self.calls = []
    def run(self, record):
        self.calls.append(record)
        raise BackfillUnavailable("Alpaca returned no bars")


def _service(provider, runner):
    return AnalysisCandleRepairService(
        provider=provider, runner_factory=lambda: runner, now_provider=lambda: NOW,
        enabled=True, alpaca_enabled=True, max_ranges=8, concurrency=1,
    )


def _daily(key):
    return {"symbol": "NVDA", "timestamp": f"{key}T00:00:00.000Z", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000, "isClosed": True}


def _intraday(key, interval):
    return {"symbol": "NVDA", "timestamp": key, "interval": interval, "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000, "isClosed": True}
