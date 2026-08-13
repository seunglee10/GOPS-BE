import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from market_data.common.redis_keys import RedisKeyBuilder
from market_data.streaming.processor import (
    ProcessorState,
    process_trade_live_path,
    processor_runtime_config,
    processor_state_sizes,
    publish_corrected_intraday_aggregates,
)
from market_data.streaming.transforms import (
    DEFAULT_CLOSED_KEY_CAP,
    CalendarCandleAggregator,
    CandleAggregator,
    LiveCandleBuilder,
    MovingAverageState,
    SourceEventDeduper,
    TickWindowCandleBuilder,
    to_iso,
)


class ProcessorStateBoundsTest(unittest.TestCase):
    def test_100000_minute_fixture_keeps_hot_path_state_bounded(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        live = LiveCandleBuilder()
        moving_average = MovingAverageState()
        aggregator = CandleAggregator()
        deduper = SourceEventDeduper()

        for index in range(100_000):
            timestamp = to_iso(start + timedelta(minutes=index))
            symbol = f"S{index % 4}"
            trade = _trade(symbol=symbol, timestamp=timestamp, trade_id=index)
            live.update(trade)
            moving_average.attach_ma(_candle(symbol=symbol, timestamp=timestamp, close=float(index)))
            aggregator.update(_candle(timestamp=timestamp, close=float(index)), 5)
            deduper.is_duplicate(f"event-{index}")

        self.assertEqual(len(live.candles), 8)
        self.assertEqual(sum(len(rows) for rows in moving_average.closes.values()), 240)
        self.assertEqual(len(aggregator.windows), 0)
        self.assertLessEqual(len(aggregator.closed_keys), DEFAULT_CLOSED_KEY_CAP)
        self.assertEqual(len(deduper.seen), 10_000)
        self.assertEqual(len(deduper.order), 10_000)

    def test_tick_and_calendar_closed_markers_evict_at_cap(self):
        tick = TickWindowCandleBuilder(grace_seconds=0, max_closed_keys=3)
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for index in range(5):
            tick.update(_trade(timestamp=to_iso(start + timedelta(minutes=index)), trade_id=index))
            tick.flush_ready(start + timedelta(minutes=index + 1))

        calendar = CalendarCandleAggregator("1D", "1W", max_closed_keys=3)
        for index in range(5):
            candle = _candle(timestamp=to_iso(start + timedelta(weeks=index)), interval="1D")
            calendar.update(candle)
            calendar.flush_ready(start + timedelta(weeks=index + 1))

        self.assertEqual(len(tick.closed_keys), 3)
        self.assertEqual(tick.closed_keys.evictions, 2)
        self.assertEqual(len(calendar.closed_keys), 3)
        self.assertEqual(calendar.closed_keys.evictions, 2)

    def test_late_open_window_update_and_closed_window_recompute_match_contract(self):
        aggregator = CandleAggregator()
        start = datetime(2026, 7, 9, 13, 30, tzinfo=timezone.utc)
        completed = None
        source = []
        for index in range(5):
            candle = _candle(
                timestamp=to_iso(start + timedelta(minutes=index)),
                close=100.5 + index,
                volume=10 + index,
            )
            source.append(candle)
            completed = aggregator.update(candle, 5)

        corrected = {
            **source[2],
            "high": 120.0,
            "close": 110.0,
            "volume": 25,
            "correctionType": "UPDATED",
            "sourceEventId": "corrected-2",
        }
        recomputed = aggregator.recompute(corrected, 5, source)

        self.assertIsNotNone(completed)
        self.assertEqual(aggregator.windows, {})
        self.assertEqual(recomputed["high"], 120.0)
        self.assertEqual(recomputed["close"], source[-1]["close"])
        self.assertEqual(recomputed["volume"], 10 + 11 + 25 + 13 + 14)
        self.assertEqual(recomputed["correctionType"], "UPDATED")
        self.assertEqual(recomputed["sourceEventId"], "corrected-2")
        self.assertEqual(aggregator.windows, {})
        self.assertEqual(aggregator.recomputes, 1)

    def test_correction_adapter_reads_canonical_window_once(self):
        state = ProcessorState()
        start = datetime(2026, 7, 9, 13, 30, tzinfo=timezone.utc)
        rows = [_candle(timestamp=to_iso(start + timedelta(minutes=index))) for index in range(240)]
        corrected = {**rows[121], "close": 777.0, "correctionType": "UPDATED"}
        calls = []
        state.canonical_candle_loader = lambda symbol, from_time, to_time: calls.append(
            (symbol, from_time, to_time)
        ) or rows

        with mock.patch("market_data.streaming.processor.publish_closed_candle") as publish:
            count = publish_corrected_intraday_aggregates(
                mock.Mock(), mock.Mock(), RedisKeyBuilder(), state, {}, corrected
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(count, 4)
        self.assertEqual(publish.call_count, 4)
        self.assertEqual(state.aggregator.windows, {})
        self.assertEqual(state.aggregator.recomputes, 4)

    def test_moving_average_retains_latest_60_after_out_of_order_correction(self):
        state = MovingAverageState()
        start = datetime(2026, 7, 9, 13, 30, tzinfo=timezone.utc)
        candles = []
        for index in range(61):
            candle = _candle(timestamp=to_iso(start + timedelta(minutes=index)), close=float(index + 1))
            candles.append(candle)
            state.attach_ma(candle)

        corrected = {**candles[1], "close": 200.0}
        state.attach_ma(corrected)

        closes = state.closes[("AAPL", "1m")]
        self.assertEqual(len(closes), 60)
        self.assertEqual(closes[corrected["timestamp"]], 200.0)
        self.assertAlmostEqual(corrected["ma"]["ma60"], (sum(range(3, 62)) + 200.0) / 60)

    def test_trade_path_has_no_legacy_tick_volume_profile_state_or_write(self):
        state = ProcessorState()
        redis = mock.Mock()
        redis.get.return_value = None

        with mock.patch("market_data.streaming.processor.publish_live_candle"), \
                mock.patch("market_data.streaming.processor.publish_derived_live_candles"), \
                mock.patch("market_data.streaming.processor.process_order_flow_live_path"):
            for index in range(100):
                process_trade_live_path(
                    _trade(trade_id=index),
                    mock.Mock(),
                    redis,
                    RedisKeyBuilder(),
                    state,
                    {},
                )

        self.assertFalse(hasattr(state, "profile_builder"))
        self.assertNotIn("legacy_tick_vp_write_enabled", processor_runtime_config({}))
        self.assertNotIn("legacyVolumeProfileBins", processor_state_sizes(state))


def _trade(symbol="AAPL", timestamp="2026-07-09T13:30:01.000Z", trade_id=1):
    return {
        "eventType": "TRADE",
        "symbol": symbol,
        "tradeId": trade_id,
        "price": 100.5,
        "size": 2,
        "timestamp": timestamp,
        "feed": "sip",
        "feedProfile": "sip",
        "marketSession": "regular",
        "sourceEventId": f"event-{trade_id}",
        "receivedAt": timestamp,
    }


def _candle(symbol="AAPL", timestamp="2026-07-09T13:30:00.000Z", close=100.5, volume=10, interval="1m"):
    return {
        "eventType": "CANDLE",
        "symbol": symbol,
        "interval": interval,
        "timestamp": timestamp,
        "open": close - 0.5,
        "high": close + 0.5,
        "low": close - 1.0,
        "close": close,
        "volume": volume,
        "tradeCount": 1,
        "vwap": close,
        "ma": {},
        "isClosed": True,
        "correctionType": "NONE",
        "source": "alpaca.bars",
        "feed": "sip",
        "feedProfile": "sip",
        "marketSession": "regular",
        "sourceEventId": f"candle-{timestamp}",
        "createdAt": timestamp,
    }


if __name__ == "__main__":
    unittest.main()
