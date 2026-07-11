"""Build a RiskContext from live app data for pre-trade risk checks.

Data sources, in order of preference:
- portfolio: simulator account (simulation mode) or the latest holdings
  snapshot remembered by GET /api/account/holdings, falling back to a direct
  KIS demo fetch.
- symbol metrics: daily candles from the market-data query service; ATR-14 is
  computed with the same alfaka implementation the chart uses, ADV over the
  last 20 sessions.

Everything degrades gracefully: any missing piece leaves the matching
RiskContext field as None so the risk engine skips (and reports) the rules
that need it. This module must never break order submission.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

from kis_trader.risk import PositionSnapshot, RiskContext, SymbolMetrics
from kis_trader.risk.context import decimal_or_none

ATR_PERIOD = 14
ADV_SESSIONS = 20


def risk_pretrade_enabled() -> bool:
    return os.getenv("RISK_PRETRADE_ENABLED", "true").strip().lower() not in {"0", "false", "off"}


def build_risk_context(app: Any, user_sub: str, symbol: str, stop_price: Any = None) -> RiskContext:
    equity, positions, daily_pnl = _portfolio_snapshot(app, user_sub)
    metrics = _symbol_metrics(symbol)
    return RiskContext(
        account_equity=equity,
        positions=tuple(positions),
        metrics=metrics,
        daily_pnl=daily_pnl,
        stop_price=decimal_or_none(stop_price),
    )


# --- portfolio --------------------------------------------------------------


def _portfolio_snapshot(app: Any, user_sub: str) -> tuple[Decimal | None, list[PositionSnapshot], Decimal | None]:
    payload = _holdings_payload(app, user_sub)
    if not isinstance(payload, dict):
        return None, [], None
    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    positions, all_positions_valued = _parse_positions(payload.get("positions"))
    equity = _first_decimal(
        account,
        ("totalValueForeign", "totalValue", "total_value_foreign", "totalValueKrw"),
    )
    if equity is None:
        # Derive equity from cash + holdings only when every position could be
        # valued. Guessing with partial data understates equity and produces
        # false blocks, so incomplete data means "unknown" (rules skip).
        if not all_positions_valued:
            return None, positions, _first_decimal(account, ("dailyPnl", "daily_pnl", "todayPnl"))
        cash = _first_decimal(account, ("cashForeign", "cash", "cash_foreign"))
        holdings_value = sum((position.market_value for position in positions), Decimal("0"))
        equity = cash + holdings_value if cash is not None else (holdings_value or None)
    daily_pnl = _first_decimal(account, ("dailyPnl", "daily_pnl", "todayPnl"))
    return equity, positions, daily_pnl


def _holdings_payload(app: Any, user_sub: str) -> dict[str, Any] | None:
    try:
        from app.routes.simulator import simulator_gateway_from_app, simulator_mode_active

        if simulator_mode_active(app):
            return simulator_gateway_from_app(app).account(user_sub)
    except Exception:
        pass
    snapshots = getattr(app.state, "portfolio_holdings_snapshots", None)
    if isinstance(snapshots, dict):
        snapshot = snapshots.get(user_sub)
        if isinstance(snapshot, dict):
            return snapshot
    # A live KIS fetch inside the order path adds seconds of latency (token +
    # holdings round trips), so it is opt-in. Normal app usage populates the
    # snapshot via GET /api/account/holdings before the user can order.
    if os.getenv("RISK_FETCH_HOLDINGS_FALLBACK", "false").strip().lower() in {"1", "true", "on"}:
        try:
            from app.routes.account import _kis_client_from_app

            return _kis_client_from_app(app).fetch_holdings(market="overseas", currency="USD", exchange="")
        except Exception:
            return None
    return None


def _parse_positions(rows: Any) -> tuple[list[PositionSnapshot], bool]:
    """Parse holdings rows; the bool reports whether every row could be valued."""
    if isinstance(rows, dict):
        rows = list(rows.values())
    if not isinstance(rows, list):
        return [], True
    positions: list[PositionSnapshot] = []
    all_valued = True
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        quantity = _first_decimal(row, ("quantity", "qty"))
        market_value = _first_decimal(
            row,
            ("marketValueForeign", "marketValue", "market_value", "marketValueKrw"),
        )
        if not symbol or quantity is None or quantity <= 0:
            continue
        if market_value is None:
            price = _first_decimal(row, ("currentPrice", "lastPrice", "price", "avgPrice"))
            market_value = quantity * price if price is not None else None
        if market_value is None:
            all_valued = False
            continue
        sector = str(row.get("sector") or "").strip() or None
        positions.append(PositionSnapshot(symbol=symbol, quantity=quantity, market_value=market_value, sector=sector))
    return positions, all_valued


# --- symbol metrics ----------------------------------------------------------


def _symbol_metrics(symbol: str) -> SymbolMetrics:
    candles = _daily_candles(symbol)
    if not candles:
        return SymbolMetrics(sector=_sector_for_symbol(symbol))
    closes = [_as_float(candle.get("close")) for candle in candles]
    highs = [_as_float(candle.get("high")) for candle in candles]
    lows = [_as_float(candle.get("low")) for candle in candles]
    volumes = [_as_float(candle.get("volume")) for candle in candles]

    last_price = next((value for value in reversed(closes) if value is not None), None)
    atr_value = _latest_atr(highs, lows, closes)
    adv_value = _average_volume(volumes)
    return SymbolMetrics(
        last_price=decimal_or_none(last_price),
        atr=decimal_or_none(atr_value),
        average_daily_volume=decimal_or_none(adv_value),
        sector=_sector_for_symbol(symbol),
    )


def _daily_candles(symbol: str) -> list[dict[str, Any]]:
    try:
        from app.market_data.query.service import get_query_service

        payload = get_query_service().candle_snapshot(symbol, "1d", "", ATR_PERIOD * 2 + 2)
    except Exception:
        return []
    candles = payload.get("candles") if isinstance(payload, dict) else None
    return [candle for candle in candles if isinstance(candle, dict)] if isinstance(candles, list) else []


def _latest_atr(
    highs: list[float | None],
    lows: list[float | None],
    closes: list[float | None],
) -> float | None:
    try:
        from alfaka.serving.indicators import atr

        series = atr(highs, lows, closes, ATR_PERIOD)
    except Exception:
        return None
    return next((value for value in reversed(series) if value is not None), None)


def _average_volume(volumes: list[float | None]) -> float | None:
    recent = [value for value in volumes[-ADV_SESSIONS:] if value is not None and value > 0]
    if not recent:
        return None
    return sum(recent) / len(recent)


def _sector_for_symbol(symbol: str) -> str | None:
    try:
        from app.market_data.heatmap.service import get_heatmap_service

        payload = get_heatmap_service().snapshot("sp500")
        items = payload.get("items") if isinstance(payload, dict) else []
        for item in items or []:
            if isinstance(item, dict) and str(item.get("symbol") or "").upper() == symbol.upper():
                sector = str(item.get("sector") or "").strip()
                return sector or None
    except Exception:
        return None
    return None


# --- helpers -----------------------------------------------------------------


def _first_decimal(row: dict[str, Any], keys: tuple[str, ...]) -> Decimal | None:
    for key in keys:
        value = decimal_or_none(row.get(key))
        if value is not None:
            return value
    return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
