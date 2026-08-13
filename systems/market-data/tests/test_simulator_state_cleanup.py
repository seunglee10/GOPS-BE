from __future__ import annotations

import fnmatch
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SHARED = ROOT / "systems" / "market-data" / "shared"
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

from market_data.tools.cleanup_simulator_state import cleanup_simulator_market_state


class FakeRedis:
    def __init__(self, keys: set[str]) -> None:
        self.keys = set(keys)
        self.scan_patterns: list[str] = []

    def scan_iter(self, match: str):
        self.scan_patterns.append(match)
        return iter(sorted(key for key in self.keys if fnmatch.fnmatch(key, match)))

    def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if key in self.keys:
                self.keys.remove(key)
                deleted += 1
        return deleted


def test_cleanup_removes_demo_symbol_market_state_without_touching_other_symbols():
    prefix = "test:market"
    redis = FakeRedis({
        f"{prefix}:live:trade:AMD",
        f"{prefix}:live:quote:AMD",
        f"{prefix}:live:candle:AMD:1m",
        f"{prefix}:latest:closed:candle:AMD:1m",
        f"{prefix}:latest:closed:watermark:AMD:1m",
        f"{prefix}:cache:candles:AMD:1m",
        f"{prefix}:state:candle-window:AMD:1m:2026-07-10T13:30:00Z",
        f"{prefix}:pending:replace:AMD:1m:2026-07-10T13:30:00Z",
        f"{prefix}:order-flow:AMD:minutes",
        f"{prefix}:order-flow:AMD:live-minute",
        f"{prefix}:live:trade:NVDA",
    })

    deleted = cleanup_simulator_market_state(
        redis,
        symbols=("AMD",),
        intervals=("1m",),
        prefix=prefix,
    )

    assert deleted == 10
    assert redis.keys == {f"{prefix}:live:trade:NVDA"}
    assert redis.scan_patterns == [f"{prefix}:*"]
