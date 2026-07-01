from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "systems/market-data/shared"))

from alfaka.common.redis_keys import RedisKeyBuilder  # noqa: E402
from alfaka.streaming.processor import processor_runtime_config, write_processor_idle_health  # noqa: E402
from alfaka.streaming.transforms import CandleAggregator, LiveCandleBuilder, MovingAverageState, VolumeProfileBinBuilder, normalize_bar  # noqa: E402
from alfaka.serving.dto import websocket_event  # noqa: E402


def candle(timestamp: str, close: float = 1.0, correction_type: str = "NONE", volume: int = 10, vwap: float | None = None) -> dict:
    return {
        "eventType": "CANDLE",
        "symbol": "AAPL",
        "interval": "1m",
        "timestamp": timestamp,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": volume,
        "tradeCount": 1,
        "vwap": vwap,
        "isClosed": True,
        "correctionType": correction_type,
        "sourceEventId": f"source/{timestamp}/{correction_type}",
    }


def trade(timestamp: str, price: float = 100.0, size: int = 1) -> dict:
    return {
        "eventType": "TRADE",
        "symbol": "AAPL",
        "price": price,
        "size": size,
        "timestamp": timestamp,
        "feed": "sip",
        "feedProfile": "sip",
        "marketSession": "regular",
        "sourceEventId": f"trade/{timestamp}",
    }


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expires: dict[str, int] = {}

    def exists(self, key: str) -> bool:
        return key in self.values

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def expire(self, key: str, seconds: int) -> None:
        self.expires[key] = seconds


class StreamingTransformTests(unittest.TestCase):
    def test_processor_manual_commit_is_default(self) -> None:
        self.assertFalse(processor_runtime_config({})["enable_auto_commit"])

    def test_processor_idle_health_creates_missing_heartbeat(self) -> None:
        redis = FakeRedis()
        keys = RedisKeyBuilder()

        result = write_processor_idle_health(redis, keys)

        self.assertEqual(result, "created")
        payload = json.loads(redis.values[keys.component_health("market-processor")])
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["lastResult"], "idle")
        self.assertEqual(payload["heartbeatResult"], "idle")
        self.assertGreater(redis.expires[keys.component_health("market-processor")], 0)

    def test_processor_idle_health_refreshes_existing_heartbeat_timestamp(self) -> None:
        redis = FakeRedis()
        keys = RedisKeyBuilder()
        key = keys.component_health("market-processor")
        redis.values[key] = json.dumps({
            "component": "market-processor",
            "status": "ok",
            "updatedAt": "2026-06-30T13:30:00.000Z",
            "lastResult": "trades",
            "lastSymbol": "AAPL",
        })

        result = write_processor_idle_health(redis, keys)

        self.assertEqual(result, "refreshed")
        payload = json.loads(redis.values[key])
        self.assertEqual(payload["lastResult"], "trades")
        self.assertEqual(payload["lastEventAt"], "2026-06-30T13:30:00.000Z")
        self.assertEqual(payload["lastSymbol"], "AAPL")
        self.assertEqual(payload["heartbeatResult"], "idle")
        self.assertNotEqual(payload["updatedAt"], "2026-06-30T13:30:00.000Z")
        self.assertGreater(redis.expires[key], 0)

    def test_daily_bar_uses_canonical_utc_midnight_timestamp(self) -> None:
        normalized = normalize_bar({
            "channel": "dailyBars",
            "symbol": "AAPL",
            "feed": "sip",
            "feedProfile": "sip",
            "marketSession": "regular",
            "sourceEventId": "daily/AAPL/2026-06-29",
            "receivedAt": "2026-06-30T00:00:00.000Z",
            "raw": {"t": "2026-06-29T04:00:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 100},
        })

        self.assertEqual(normalized["interval"], "1D")
        self.assertEqual(normalized["timestamp"], "2026-06-29T00:00:00.000Z")

    def test_daily_websocket_event_uses_canonical_utc_midnight_timestamp(self) -> None:
        event = websocket_event("CANDLE_CLOSED", "AAPL", "1D", {
            "symbol": "AAPL",
            "interval": "1D",
            "timestamp": "2026-06-29T04:00:00.000Z",
            "open": 1,
            "high": 2,
            "low": 1,
            "close": 2,
            "volume": 100,
            "isClosed": True,
            "sourceEventId": "daily/AAPL/2026-06-29",
        })

        self.assertEqual(event["data"]["timestamp"], "2026-06-29T00:00:00.000Z")
        self.assertIn(":2026-06-29T00:00:00.000Z:", event["cursor"])

    def test_aggregator_emits_sparse_bucket_at_bucket_end(self) -> None:
        aggregator = CandleAggregator()

        self.assertIsNone(aggregator.update(candle("2026-06-29T13:30:00.000Z", 1), 5))
        aggregated = aggregator.update(candle("2026-06-29T13:34:00.000Z", 2), 5)

        self.assertIsNotNone(aggregated)
        self.assertEqual(aggregated["interval"], "5m")
        self.assertEqual(aggregated["timestamp"], "2026-06-29T13:30:00.000Z")
        self.assertEqual(aggregated["close"], 2)

    def test_aggregator_uses_volume_weighted_vwap(self) -> None:
        aggregator = CandleAggregator()

        self.assertIsNone(aggregator.update(candle("2026-06-29T13:30:00.000Z", 10, volume=2, vwap=10), 5))
        aggregated = aggregator.update(candle("2026-06-29T13:34:00.000Z", 20, volume=3, vwap=20), 5)

        self.assertAlmostEqual(aggregated["vwap"], 16.0)

    def test_live_builder_accumulates_trade_count_volume_and_weighted_vwap(self) -> None:
        live_builder = LiveCandleBuilder()

        live_builder.update(trade("2026-06-29T13:30:10.000Z", 100, size=2))
        live_candle = live_builder.update(trade("2026-06-29T13:30:20.000Z", 110, size=3))

        self.assertEqual(live_candle["volume"], 5)
        self.assertEqual(live_candle["tradeCount"], 2)
        self.assertEqual(live_candle["high"], 110.0)
        self.assertEqual(live_candle["low"], 100.0)
        self.assertAlmostEqual(live_candle["vwap"], 106.0)

    def test_aggregator_preserves_updated_correction_type(self) -> None:
        aggregator = CandleAggregator()
        for minute in range(4):
            aggregator.update(candle(f"2026-06-29T13:3{minute}:00.000Z", 1), 5)

        aggregated = aggregator.update(candle("2026-06-29T13:34:00.000Z", 3, "UPDATED"), 5)

        self.assertEqual(aggregated["correctionType"], "UPDATED")

    def test_realtime_state_builders_prune(self) -> None:
        live_builder = LiveCandleBuilder(max_candles=1)
        live_builder.update(trade("2026-06-29T13:30:00.000Z"))
        live_builder.update(trade("2026-06-29T13:31:00.000Z"))
        self.assertEqual(len(live_builder.candles), 1)

        ma_state = MovingAverageState(max_closes_per_key=2)
        for minute in range(3):
            ma_state.attach_ma(candle(f"2026-06-29T13:3{minute}:00.000Z", minute + 1))
        self.assertEqual(len(ma_state.closes[("AAPL", "1m")]), 2)

        profile_builder = VolumeProfileBinBuilder(max_bins=1)
        profile_builder.update(trade("2026-06-29T13:30:00.000Z", 100))
        latest = profile_builder.update(trade("2026-06-29T13:31:00.000Z", 101))
        self.assertEqual(len(profile_builder.bins), 1)
        self.assertEqual(latest["feedProfile"], "sip")
        self.assertEqual(latest["marketSession"], "regular")


if __name__ == "__main__":
    unittest.main()
