from __future__ import annotations

import fnmatch
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SHARED = ROOT / "systems" / "market-data" / "shared"
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

from alfaka.tools.simulator_state_snapshot import capture_simulator_market_state, restore_simulator_market_state


class FakeRedis:
    def __init__(self, values: dict[str, object]) -> None:
        self.values = dict(values)

    def scan_iter(self, match: str):
        return iter(sorted(key for key in self.values if fnmatch.fnmatch(key, match)))

    def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if key in self.values:
                del self.values[key]
                deleted += 1
        return deleted

    def exists(self, key: str) -> int:
        return int(key in self.values)

    def copy(self, source: str, destination: str, replace: bool = False) -> bool:
        if source not in self.values:
            return False
        if destination in self.values and not replace:
            return False
        self.values[destination] = self.values[source]
        return True

    def get(self, key: str):
        return self.values.get(key)

    def set(self, key: str, value: object) -> bool:
        self.values[key] = value
        return True


def test_restore_replaces_simulation_rows_with_the_exact_pre_simulation_snapshot():
    prefix = "test:market"
    candle_key = f"{prefix}:cache:candles:AMD:1m"
    quote_key = f"{prefix}:live:quote:AMD"
    nvda_key = f"{prefix}:live:trade:NVDA"
    redis = FakeRedis({
        candle_key: {"kind": "zset", "rows": ["real-candle"]},
        quote_key: {"kind": "hash", "price": 160.0},
        nvda_key: {"kind": "hash", "price": 180.0},
    })

    captured = capture_simulator_market_state(
        redis,
        symbols=("AMD",),
        intervals=("1m",),
        prefix=prefix,
    )

    redis.values[candle_key] = {"kind": "zset", "rows": ["simulator-candle"]}
    redis.values[quote_key] = {"kind": "hash", "price": 565.0}
    redis.values[f"{prefix}:live:candle:AMD:1m"] = {"close": 525.5}
    redis.values[f"{prefix}:state:candle-window:AMD:1m:sim-run"] = {"close": 525.5}

    restored = restore_simulator_market_state(
        redis,
        symbols=("AMD",),
        intervals=("1m",),
        prefix=prefix,
    )

    assert captured == 2
    assert restored == 2
    assert redis.values[candle_key] == {"kind": "zset", "rows": ["real-candle"]}
    assert redis.values[quote_key] == {"kind": "hash", "price": 160.0}
    assert f"{prefix}:live:candle:AMD:1m" not in redis.values
    assert f"{prefix}:state:candle-window:AMD:1m:sim-run" not in redis.values
    assert redis.values[nvda_key] == {"kind": "hash", "price": 180.0}


def test_restore_without_a_snapshot_still_removes_simulator_rows():
    prefix = "test:market"
    simulator_key = f"{prefix}:cache:candles:AMD:1m"
    redis = FakeRedis({simulator_key: {"rows": ["simulator-candle"]}})

    restored = restore_simulator_market_state(
        redis,
        symbols=("AMD",),
        intervals=("1m",),
        prefix=prefix,
    )

    assert restored == 0
    assert simulator_key not in redis.values
