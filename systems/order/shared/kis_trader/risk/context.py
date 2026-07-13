"""Input snapshots for pre-trade risk evaluation.

Data minimization on purpose: the risk engine only ever sees an internal
account identifier, symbols, quantities, and prices. Names, account numbers,
or any other personal fields must never enter this context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    quantity: Decimal
    market_value: Decimal
    sector: str | None = None


@dataclass(frozen=True)
class SymbolMetrics:
    """Pre-computed market metrics for the order symbol.

    All values are optional: rules that need a missing metric are skipped and
    reported in the verdict instead of guessing.
    """

    last_price: Decimal | None = None
    average_daily_volume: Decimal | None = None
    sector: str | None = None


@dataclass(frozen=True)
class RiskContext:
    account_equity: Decimal | None = None
    positions: tuple[PositionSnapshot, ...] = ()
    metrics: SymbolMetrics = field(default_factory=SymbolMetrics)
    daily_pnl: Decimal | None = None
    # 오늘 매수 누적 금액 (daily_buy_budget 룰 입력, 접수 기준 근사)
    daily_buy_notional: Decimal | None = None


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def risk_context_from_dict(payload: dict[str, Any]) -> RiskContext:
    """Build a RiskContext from a plain JSON-ish dict (provider convenience)."""
    positions = []
    for row in payload.get("positions", []):
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        quantity = decimal_or_none(row.get("quantity"))
        market_value = decimal_or_none(row.get("marketValue"))
        if not symbol or quantity is None or market_value is None:
            continue
        positions.append(
            PositionSnapshot(
                symbol=symbol,
                quantity=quantity,
                market_value=market_value,
                sector=(str(row["sector"]) if row.get("sector") else None),
            )
        )
    metrics_payload = payload.get("metrics") or {}
    metrics = SymbolMetrics(
        last_price=decimal_or_none(metrics_payload.get("lastPrice")),
        average_daily_volume=decimal_or_none(metrics_payload.get("averageDailyVolume")),
        sector=(str(metrics_payload["sector"]) if metrics_payload.get("sector") else None),
    )
    return RiskContext(
        account_equity=decimal_or_none(payload.get("accountEquity")),
        positions=tuple(positions),
        metrics=metrics,
        daily_pnl=decimal_or_none(payload.get("dailyPnl")),
        daily_buy_notional=decimal_or_none(payload.get("dailyBuyNotional")),
    )
