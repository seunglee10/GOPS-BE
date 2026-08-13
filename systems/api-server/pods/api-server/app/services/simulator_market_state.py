from __future__ import annotations

import os
import time
from typing import Any

from market_data.tools.simulator_state_snapshot import capture_simulator_market_state, restore_simulator_market_state
from app.services.alfaka_market_data import get_market_data_provider


class SimulatorMarketStateManager:
    def __init__(self, redis_client: Any, *, drain_seconds: float | None = None) -> None:
        self.redis_client = redis_client
        self.drain_seconds = _drain_seconds(drain_seconds)

    def capture(self) -> int:
        return capture_simulator_market_state(self.redis_client)

    def restore(self) -> int:
        if self.drain_seconds > 0:
            time.sleep(self.drain_seconds)
        return restore_simulator_market_state(self.redis_client)


def simulator_market_state_manager_from_app(app: Any) -> SimulatorMarketStateManager:
    existing = getattr(app.state, "simulator_market_state_manager", None)
    if existing is not None:
        return existing
    provider = get_market_data_provider()
    redis_client = getattr(getattr(provider, "redis_provider", None), "redis", None)
    if redis_client is None:
        raise RuntimeError("Simulator market-state Redis is unavailable")
    manager = SimulatorMarketStateManager(redis_client)
    app.state.simulator_market_state_manager = manager
    return manager


def _drain_seconds(value: float | None) -> float:
    if value is None:
        try:
            value = float(os.getenv("SIMULATOR_STATE_DRAIN_SECONDS", "0.5"))
        except (TypeError, ValueError):
            value = 0.5
    return min(2.0, max(0.0, value))
