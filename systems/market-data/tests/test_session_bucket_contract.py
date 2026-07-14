from __future__ import annotations

import sys
from datetime import datetime, time, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SHARED = ROOT / "systems" / "market-data" / "shared"
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

from alfaka.backfill.gapfill import TradingCalendar  # noqa: E402
from alfaka.serving.session_buckets import (  # noqa: E402
    BUCKET_POLICY_EXTENDED_SESSION,
    BUCKET_POLICY_REGULAR_SESSION,
    aggregate_regular_session_candles,
    aggregate_visible_extended_session_candles,
    extended_session_bucket,
    regular_session_bucket,
)
from alfaka.streaming.transforms import ProvisionalCandleState  # noqa: E402


def test_hour_bucket_is_anchored_to_new_york_open_across_dst() -> None:
    summer = regular_session_bucket("2026-07-10T13:35:00.000Z", "1h")
    winter = regular_session_bucket("2026-01-05T14:35:00.000Z", "1h")

    assert summer is not None
    assert winter is not None
    assert summer.start.isoformat() == "2026-07-10T13:30:00+00:00"
    assert summer.end.isoformat() == "2026-07-10T14:30:00+00:00"
    assert winter.start.isoformat() == "2026-01-05T14:30:00+00:00"
    assert winter.end.isoformat() == "2026-01-05T15:30:00+00:00"


def test_four_hour_final_bucket_ends_at_regular_close_and_early_close() -> None:
    regular = regular_session_bucket("2026-07-10T18:00:00.000Z", "4h")
    early_calendar = TradingCalendar(early_closes={"2026-07-02": time(13, 0)})
    early = regular_session_bucket(
        "2026-07-02T16:45:00.000Z",
        "4h",
        calendar=early_calendar,
    )

    assert regular is not None
    assert regular.start.isoformat() == "2026-07-10T17:30:00+00:00"
    assert regular.end.isoformat() == "2026-07-10T20:00:00+00:00"
    assert early is not None
    assert early.start.isoformat() == "2026-07-02T13:30:00+00:00"
    assert early.end.isoformat() == "2026-07-02T17:00:00+00:00"


def test_aggregation_closes_by_session_time_without_fake_no_trade_minutes() -> None:
    rows = [
        candle("2026-07-10T13:30:00.000Z", open=100, high=101, low=99, close=100.5, volume=10),
        candle("2026-07-10T14:29:00.000Z", open=101, high=103, low=100, close=102, volume=20),
        candle("2026-07-10T14:30:00.000Z", open=102, high=104, low=101, close=103, volume=30),
    ]

    result = aggregate_regular_session_candles(
        rows,
        "1h",
        now=datetime(2026, 7, 10, 14, 31, tzinfo=timezone.utc),
    )

    assert len(result) == 1
    assert result[0]["timestamp"] == "2026-07-10T13:30:00.000Z"
    assert result[0]["open"] == 100
    assert result[0]["close"] == 102
    assert result[0]["volume"] == 30
    assert result[0]["bucketPolicy"] == BUCKET_POLICY_REGULAR_SESSION


def test_extended_hour_buckets_are_anchored_and_clamped_to_each_session() -> None:
    after_hour = extended_session_bucket("2026-07-08T22:15:00.000Z", "1h")
    after = extended_session_bucket("2026-07-08T22:15:00.000Z", "4h")
    overnight = extended_session_bucket("2026-07-09T05:15:00.000Z", "4h")
    pre = extended_session_bucket("2026-07-09T12:15:00.000Z", "4h")

    assert after_hour is not None
    assert after_hour.start.isoformat() == "2026-07-08T22:00:00+00:00"
    assert after_hour.end.isoformat() == "2026-07-08T23:00:00+00:00"
    assert after is not None
    assert after.session == "after"
    assert after.start.isoformat() == "2026-07-08T20:00:00+00:00"
    assert after.end.isoformat() == "2026-07-09T00:00:00+00:00"
    assert overnight is not None
    assert overnight.session == "overnight"
    assert overnight.start.isoformat() == "2026-07-09T04:00:00+00:00"
    assert overnight.end.isoformat() == "2026-07-09T08:00:00+00:00"
    assert pre is not None
    assert pre.session == "pre"
    assert pre.start.isoformat() == "2026-07-09T12:00:00+00:00"
    assert pre.end.isoformat() == "2026-07-09T13:30:00+00:00"


def test_extended_aggregation_keeps_only_visible_sessions_and_marks_live_bucket() -> None:
    rows = [
        candle("2026-07-07T21:00:00.000Z", marketSession="after", close=90),
        candle("2026-07-08T21:00:00.000Z", marketSession="after", open=100, close=101),
        candle("2026-07-08T23:59:00.000Z", marketSession="after", open=101, close=102),
        candle("2026-07-09T00:01:00.000Z", marketSession="overnight", open=102, close=103),
        candle("2026-07-09T05:15:00.000Z", marketSession="overnight", open=103, close=104),
    ]

    result = aggregate_visible_extended_session_candles(
        rows,
        "4h",
        now=datetime(2026, 7, 9, 5, 30, tzinfo=timezone.utc),
    )

    assert [item["timestamp"] for item in result] == [
        "2026-07-08T20:00:00.000Z",
        "2026-07-09T00:00:00.000Z",
        "2026-07-09T04:00:00.000Z",
    ]
    assert [item["marketSession"] for item in result] == ["after", "overnight", "overnight"]
    assert [item["isClosed"] for item in result] == [True, True, False]
    assert result[-1]["close"] == 104
    assert result[-1]["bucketPolicy"] == BUCKET_POLICY_EXTENDED_SESSION


def test_provisional_candle_uses_extended_session_bucket() -> None:
    state = ProvisionalCandleState()
    state.record_closed(candle("2026-07-09T04:01:00.000Z", marketSession="overnight", open=100, close=101))
    live = candle(
        "2026-07-09T05:15:00.000Z",
        marketSession="overnight",
        open=101,
        close=105,
        isClosed=False,
    )

    result = state.build_from_1m("AAPL", "4h", live_1m=live)

    assert result is not None
    assert result["timestamp"] == "2026-07-09T04:00:00.000Z"
    assert result["open"] == 100
    assert result["close"] == 105
    assert result["marketSession"] == "overnight"
    assert result["bucketPolicy"] == BUCKET_POLICY_EXTENDED_SESSION


def candle(timestamp: str, **values) -> dict:
    row = {
        "symbol": "AAPL",
        "interval": "1m",
        "timestamp": timestamp,
        "open": 100,
        "high": 101,
        "low": 99,
        "close": 100.5,
        "volume": 10,
        "tradeCount": 1,
        "vwap": 100.25,
        "isClosed": True,
        "source": "alpaca.bars",
        "feed": "sip",
        "feedProfile": "sip",
        "marketSession": "regular",
        "priceAdjustment": "split",
        "canonicalVersion": "v2",
    }
    row.update(values)
    return row
