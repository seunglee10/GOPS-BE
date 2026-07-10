from __future__ import annotations

from typing import Any

from .schema import DISPLAY_BARS, LOOKBACK_BARS, assemble_feature_pack, normalize_candles


KERNEL_VERSION = "kernel-v1"


def compute_feature_pack(candles: list[dict[str, Any]], interval: str) -> dict[str, Any]:
    return assemble_feature_pack(candles, interval)


__all__ = [
    "DISPLAY_BARS",
    "KERNEL_VERSION",
    "LOOKBACK_BARS",
    "compute_feature_pack",
    "normalize_candles",
]
