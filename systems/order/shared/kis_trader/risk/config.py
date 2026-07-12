"""Declarative risk rule configuration.

Precedence: code defaults < YAML file (RISK_RULES_PATH or explicit path) <
runtime overrides dict. Every threshold lives here so rules stay pure
functions and product/compliance can tune limits without code changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class RiskConfig:
    # single_name_limit
    single_name_max_weight: Decimal = Decimal("0.20")
    # sector_limit
    sector_max_weight: Decimal = Decimal("0.40")
    # fat_finger
    max_order_notional: Decimal = Decimal("50000")
    max_adv_participation: Decimal = Decimal("0.05")
    price_band_pct: Decimal = Decimal("0.05")
    # daily_loss_cooldown
    daily_loss_limit_pct: Decimal = Decimal("0.03")
    # daily_buy_budget — 사용자 옵트인 자기구속 장치. None = 꺼짐(룰 침묵)
    daily_buy_budget: Decimal | None = None
    # symbol -> sector fallback map (metrics/position sector wins)
    sector_map: dict[str, str] = field(default_factory=dict)


_DECIMAL_FIELDS = {
    "single_name_max_weight",
    "sector_max_weight",
    "max_order_notional",
    "max_adv_participation",
    "price_band_pct",
    "daily_loss_limit_pct",
}

_OPTIONAL_DECIMAL_FIELDS = {
    "daily_buy_budget",
}


def load_risk_config(
    path: str | os.PathLike[str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> RiskConfig:
    values: dict[str, Any] = {}
    resolved_path = path or os.getenv("RISK_RULES_PATH")
    if resolved_path:
        values.update(_read_yaml_values(Path(resolved_path)))
    if overrides:
        values.update(overrides)
    return _config_from_values(values)


def _read_yaml_values(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"risk rules file not found: {path}")
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment guard
        raise RuntimeError("PyYAML is required to load risk rules from YAML") from exc
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"risk rules file must contain a mapping: {path}")
    return loaded


def _config_from_values(values: Mapping[str, Any]) -> RiskConfig:
    known = {item.name for item in fields(RiskConfig)}
    parsed: dict[str, Any] = {}
    for key, value in values.items():
        if key not in known:
            raise ValueError(f"unknown risk config key: {key}")
        if key in _DECIMAL_FIELDS:
            parsed[key] = Decimal(str(value))
        elif key in _OPTIONAL_DECIMAL_FIELDS:
            parsed[key] = None if value is None else Decimal(str(value))
        elif key == "sector_map":
            if not isinstance(value, Mapping):
                raise ValueError("sector_map must be a mapping of symbol to sector")
            parsed[key] = {str(symbol).upper(): str(sector) for symbol, sector in value.items()}
        else:  # pragma: no cover - future fields
            parsed[key] = value
    return RiskConfig(**parsed)
