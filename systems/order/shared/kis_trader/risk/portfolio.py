"""Portfolio aggregation helpers (weights, sector exposure).

Pure functions shared by the pre-trade engine now and the risk monitor later.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Mapping

from .context import PositionSnapshot


def position_market_value(positions: Iterable[PositionSnapshot], symbol: str) -> Decimal:
    normalized = symbol.upper()
    total = Decimal("0")
    for position in positions:
        if position.symbol.upper() == normalized:
            total += position.market_value
    return total


def portfolio_weights(
    positions: Iterable[PositionSnapshot],
    account_equity: Decimal | None,
) -> dict[str, Decimal]:
    if account_equity is None or account_equity <= 0:
        return {}
    weights: dict[str, Decimal] = {}
    for position in positions:
        symbol = position.symbol.upper()
        weights[symbol] = weights.get(symbol, Decimal("0")) + position.market_value / account_equity
    return weights


def sector_exposures(
    positions: Iterable[PositionSnapshot],
    account_equity: Decimal | None,
    sector_lookup: Mapping[str, str] | None = None,
) -> dict[str, Decimal]:
    if account_equity is None or account_equity <= 0:
        return {}
    lookup = {key.upper(): value for key, value in (sector_lookup or {}).items()}
    exposures: dict[str, Decimal] = {}
    for position in positions:
        sector = position.sector or lookup.get(position.symbol.upper())
        if not sector:
            continue
        exposures[sector] = exposures.get(sector, Decimal("0")) + position.market_value / account_equity
    return exposures
