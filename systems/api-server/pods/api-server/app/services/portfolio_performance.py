from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence


PERFORMANCE_RANGE_LOOKBACKS: dict[str, timedelta | None] = {
    "1W": timedelta(days=7),
    "1M": timedelta(days=31),
    "3M": timedelta(days=93),
    "1Y": timedelta(days=366),
    "ALL": None,
}


def performance_start_at(range_value: str, now: datetime) -> datetime | None:
    normalized = normalize_performance_range(range_value)
    lookback = PERFORMANCE_RANGE_LOOKBACKS[normalized]
    return ensure_utc(now) - lookback if lookback is not None else None


def normalize_performance_range(value: str) -> str:
    normalized = str(value or "1M").strip().upper()
    if normalized not in PERFORMANCE_RANGE_LOOKBACKS:
        raise ValueError(f"Unsupported portfolio performance range: {value}")
    return normalized


def build_portfolio_performance(
    snapshots: Sequence[Mapping[str, Any]],
    benchmark: Mapping[str, Any] | None,
    *,
    range_value: str,
    net_invested_principal: float | None = None,
) -> dict[str, Any]:
    normalized_range = normalize_performance_range(range_value)
    observed = sorted(
        (point for point in (_snapshot_point(row) for row in snapshots) if point is not None),
        key=lambda point: point["time"],
    )
    portfolio_points = _normalize_reported_returns(observed)
    fallback_principal = finite_float(net_invested_principal)
    if fallback_principal is not None and fallback_principal >= 0:
        for point in portfolio_points:
            point.setdefault("netInvestedPrincipal", round(fallback_principal, 6))
    benchmark_points = _normalize_benchmark_points(
        benchmark.get("points") if isinstance(benchmark, Mapping) else None,
        portfolio_points[0]["time"] if portfolio_points else None,
    )
    warnings: list[str] = []
    if len(portfolio_points) < 2:
        warnings.append("성과 이력이 두 시점 이상 쌓여야 추이를 표시할 수 있습니다.")
    if portfolio_points and len(benchmark_points) < 2:
        warnings.append("선택 기간의 S&P 500 비교 데이터가 부족합니다.")
    return {
        "status": "ready" if len(portfolio_points) >= 2 else "insufficient_history",
        "range": normalized_range,
        "calculation": "reported_unrealized_return_change",
        "asOf": portfolio_points[-1]["time"] if portfolio_points else None,
        "portfolio": {
            "name": "내 포트폴리오",
            "source": "portfolio_snapshot_history",
            "points": portfolio_points,
        },
        "benchmark": {
            "symbol": str(benchmark.get("symbol") or "^GSPC") if isinstance(benchmark, Mapping) else "^GSPC",
            "name": str(benchmark.get("name") or "S&P 500") if isinstance(benchmark, Mapping) else "S&P 500",
            "method": str(benchmark.get("method") or "price_return") if isinstance(benchmark, Mapping) else "price_return",
            "source": benchmark.get("source") if isinstance(benchmark, Mapping) else None,
            "asOf": benchmark_points[-1]["time"] if benchmark_points else None,
            "points": benchmark_points,
        },
        "warnings": warnings,
    }


def _snapshot_point(row: Mapping[str, Any]) -> dict[str, Any] | None:
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        return None
    source_as_of = parse_datetime(row.get("source_as_of") or payload.get("asOf") or payload.get("sourceAsOf"))
    if source_as_of is None:
        return None
    account = payload.get("account")
    if not isinstance(account, Mapping):
        return None
    stock_value = first_finite(account, "stockValueForeign", "market_value")
    cash_value = first_finite(account, "cashForeign", "cash_foreign")
    portfolio_value = first_finite(account, "totalValueForeign", "total_value_foreign")
    if portfolio_value is None and stock_value is not None:
        portfolio_value = stock_value + (cash_value or 0)
    holdings_cost_basis = positions_cost_basis(payload.get("positions"))
    net_invested_principal = first_finite(
        account,
        "netInvestedPrincipal",
        "startingCashForeign",
        "starting_cash",
    )
    reported_rate = first_finite(account, "unrealizedPnlRate", "unrealized_pnl_rate")
    if reported_rate is None:
        pnl = first_finite(account, "unrealizedPnlForeign", "unrealized_pnl")
        cost_basis = stock_value - pnl if stock_value is not None and pnl is not None else None
        if holdings_cost_basis is None:
            holdings_cost_basis = cost_basis
        if pnl is not None and cost_basis not in (None, 0):
            reported_rate = pnl / cost_basis * 100
    elif holdings_cost_basis is None:
        pnl = first_finite(account, "unrealizedPnlForeign", "unrealized_pnl")
        if stock_value is not None and pnl is not None:
            holdings_cost_basis = stock_value - pnl
    if reported_rate is None or reported_rate <= -100:
        return None
    return {
        "time": source_as_of,
        "reportedReturnPercent": reported_rate,
        "portfolioValue": portfolio_value,
        "holdingsCostBasis": holdings_cost_basis,
        "netInvestedPrincipal": net_invested_principal,
    }


def _normalize_reported_returns(points: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not points:
        return []
    base_rate = finite_float(points[0].get("reportedReturnPercent"))
    base_factor = 1 + base_rate / 100 if base_rate is not None else None
    if base_factor is None or base_factor <= 0:
        return []
    normalized: list[dict[str, Any]] = []
    for point in points:
        rate = finite_float(point.get("reportedReturnPercent"))
        timestamp = point.get("time")
        if rate is None or not isinstance(timestamp, datetime):
            continue
        normalized_point = {
            "time": isoformat_z(timestamp),
            "returnPercent": round(((1 + rate / 100) / base_factor - 1) * 100, 6),
        }
        portfolio_value = finite_float(point.get("portfolioValue"))
        holdings_cost_basis = finite_float(point.get("holdingsCostBasis"))
        net_invested_principal = finite_float(point.get("netInvestedPrincipal"))
        if portfolio_value is not None:
            normalized_point["portfolioValue"] = round(portfolio_value, 6)
        if holdings_cost_basis is not None:
            normalized_point["holdingsCostBasis"] = round(holdings_cost_basis, 6)
        if net_invested_principal is not None:
            normalized_point["netInvestedPrincipal"] = round(net_invested_principal, 6)
        normalized.append(normalized_point)
    return normalized


def positions_cost_basis(raw_positions: Any) -> float | None:
    if not isinstance(raw_positions, Sequence) or isinstance(raw_positions, (str, bytes, bytearray)):
        return None
    values = [
        value
        for position in raw_positions
        if isinstance(position, Mapping)
        if (value := first_finite(position, "purchaseAmountForeign", "purchase_amount_foreign")) is not None
    ]
    return sum(values) if values else None


def _normalize_benchmark_points(raw_points: Any, start_at: str | None) -> list[dict[str, Any]]:
    if not isinstance(raw_points, Sequence) or isinstance(raw_points, (str, bytes, bytearray)):
        return []
    start = parse_datetime(start_at)
    parsed = []
    for raw in raw_points:
        if not isinstance(raw, Mapping):
            continue
        timestamp = parse_datetime(raw.get("time"))
        value = finite_float(raw.get("returnPercent"))
        if timestamp is None or value is None or (start is not None and timestamp < start):
            continue
        parsed.append({"time": timestamp, "returnPercent": value})
    parsed.sort(key=lambda point: point["time"])
    if not parsed:
        return []
    base = parsed[0]["returnPercent"]
    return [
        {
            "time": isoformat_z(point["time"]),
            "returnPercent": round(((1 + point["returnPercent"] / 100) / (1 + base / 100) - 1) * 100, 6),
        }
        for point in parsed
        if 1 + point["returnPercent"] / 100 > 0 and 1 + base / 100 > 0
    ]


def first_finite(value: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        parsed = finite_float(value.get(key))
        if parsed is not None:
            return parsed
    return None


def finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return ensure_utc(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ensure_utc(parsed)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def isoformat_z(value: datetime) -> str:
    return ensure_utc(value).isoformat().replace("+00:00", "Z")
