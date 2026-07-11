from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IntervalQualityConfig:
    display_bars: int
    extra_anchor_bars: int
    pivot_confirm_bars: int
    pivot_separation: int
    min_touch_gap: int
    reaction_horizon: int
    level_half_life: int
    level_last_touch_max_age: int
    event_relevance_bars: int
    extreme_episode_gap: int
    volume_baseline_bars: int
    retest_follow_through: int
    forward_trend_horizon: int


QUALITY_CONFIG = {
    "1m": IntervalQualityConfig(120, 60, 3, 5, 5, 10, 24, 42, 30, 10, 20, 3, 12),
    "5m": IntervalQualityConfig(120, 60, 3, 5, 5, 10, 24, 42, 30, 10, 20, 3, 12),
    "10m": IntervalQualityConfig(120, 60, 3, 5, 5, 10, 24, 42, 30, 10, 20, 3, 12),
    "1h": IntervalQualityConfig(120, 60, 3, 5, 5, 10, 24, 42, 30, 10, 20, 3, 12),
    "4h": IntervalQualityConfig(120, 60, 2, 4, 4, 8, 20, 40, 24, 8, 20, 2, 10),
    "1D": IntervalQualityConfig(120, 60, 3, 5, 5, 10, 24, 42, 30, 10, 20, 3, 12),
    "1W": IntervalQualityConfig(104, 52, 2, 3, 3, 6, 16, 36, 16, 4, 13, 2, 8),
    "1M": IntervalQualityConfig(36, 18, 1, 2, 2, 3, 8, 14, 12, 2, 12, 1, 4),
}

KERNEL_VERSION = "kernel-v4"
QUALITY_POLICY_VERSION = "chart-quality-v4"
