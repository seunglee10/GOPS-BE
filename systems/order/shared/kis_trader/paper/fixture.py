from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final


SEED_PROFILE: Final = "diversified-us-v2"
DISABLED_SEED_PROFILES: Final = frozenset({"", "none", "off", "disabled"})


@dataclass(frozen=True)
class DemoHolding:
    symbol: str
    name: str
    exchange: str
    sector: str
    industry: str
    quantity: Decimal
    average_price: Decimal
    fallback_price: Decimal
    pe_ratio: Decimal | None = None
    eps_ttm: Decimal | None = None
    low_52: Decimal | None = None
    high_52: Decimal | None = None
    dividend_yield: Decimal | None = None
    dividend_per_share: Decimal | None = None
    day_pnl_rate: Decimal | None = None


@dataclass(frozen=True)
class DemoFill:
    filled_at: datetime
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    equity: Decimal


DEMO_HOLDINGS: Final = (
    DemoHolding(
        "GOOGL", "Alphabet Inc.", "NASDAQ", "Communication Services",
        "Interactive Media & Services", Decimal("60"), Decimal("172.40"), Decimal("184.30"),
        Decimal("24.18"), Decimal("7.62"), Decimal("140.53"), Decimal("207.05"),
        Decimal("0.45"), Decimal("0.80"), Decimal("0.88"),
    ),
    DemoHolding(
        "MSFT", "Microsoft Corporation", "NASDAQ", "Information Technology",
        "Systems Software", Decimal("48"), Decimal("386.74125"), Decimal("423.18"),
        Decimal("36.92"), Decimal("11.46"), Decimal("344.79"), Decimal("468.35"),
        Decimal("0.72"), Decimal("3.32"), Decimal("-0.94"),
    ),
    DemoHolding(
        "JPM", "JPMorgan Chase & Co.", "NYSE", "Financials",
        "Diversified Banks", Decimal("54"), Decimal("199.20"), Decimal("216.44"),
        Decimal("12.11"), Decimal("17.87"), Decimal("190.90"), Decimal("247.30"),
        Decimal("2.25"), Decimal("4.60"), Decimal("0.75"),
    ),
    DemoHolding(
        "XOM", "Exxon Mobil Corporation", "NYSE", "Energy",
        "Integrated Oil & Gas", Decimal("90"), Decimal("109.50"), Decimal("113.22"),
        Decimal("13.62"), Decimal("8.31"), Decimal("97.80"), Decimal("126.34"),
        Decimal("3.38"), Decimal("3.96"), Decimal("0.33"),
    ),
    DemoHolding(
        "JNJ", "Johnson & Johnson", "NYSE", "Health Care",
        "Drug Manufacturers - General", Decimal("60"), Decimal("151.80"), Decimal("156.70"),
        Decimal("15.90"), Decimal("9.86"), Decimal("140.68"), Decimal("170.72"),
        Decimal("3.32"), Decimal("5.20"), Decimal("0.42"),
    ),
    DemoHolding(
        "COST", "Costco Wholesale Corporation", "NASDAQ", "Consumer Staples",
        "Discount Stores", Decimal("12"), Decimal("935.00"), Decimal("1005.00"),
        Decimal("58.74"), Decimal("17.11"), Decimal("793.00"), Decimal("1078.23"),
        Decimal("0.52"), Decimal("5.20"), Decimal("0.90"),
    ),
    DemoHolding(
        "HD", "The Home Depot, Inc.", "NYSE", "Consumer Discretionary",
        "Home Improvement Retail", Decimal("24"), Decimal("389.00"), Decimal("375.50"),
        Decimal("25.47"), Decimal("14.74"), Decimal("326.31"), Decimal("439.37"),
        Decimal("2.45"), Decimal("9.20"), Decimal("-0.79"),
    ),
    DemoHolding(
        "NVDA", "NVIDIA Corporation", "NASDAQ", "Information Technology",
        "Semiconductors", Decimal("20"), Decimal("175.00"), Decimal("181.50"),
        Decimal("52.40"), Decimal("3.46"), Decimal("86.62"), Decimal("195.95"),
        Decimal("0.02"), Decimal("0.04"), Decimal("1.24"),
    ),
    DemoHolding(
        "AMZN", "Amazon.com, Inc.", "NASDAQ", "Consumer Discretionary",
        "Broadline Retail", Decimal("15"), Decimal("228.00"), Decimal("225.00"),
        Decimal("35.60"), Decimal("6.32"), Decimal("151.61"), Decimal("242.52"),
        None, None, Decimal("-0.65"),
    ),
    DemoHolding(
        "WMT", "Walmart Inc.", "NASDAQ", "Consumer Staples",
        "Consumer Staples Merchandise Retail", Decimal("50"), Decimal("102.00"), Decimal("104.50"),
        Decimal("40.80"), Decimal("2.56"), Decimal("78.98"), Decimal("106.95"),
        Decimal("0.90"), Decimal("0.94"), Decimal("0.58"),
    ),
)


def _fill(timestamp: str, symbol: str, side: str, quantity: str, price: str, equity: str) -> DemoFill:
    return DemoFill(
        filled_at=datetime.fromisoformat(timestamp.replace("Z", "+00:00")),
        symbol=symbol,
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        equity=Decimal(equity),
    )


DEMO_FILLS: Final = (
    _fill("2026-01-08T20:30:00Z", "GOOGL", "buy", "36", "165.00", "100000.00"),
    _fill("2026-01-15T20:30:00Z", "MSFT", "buy", "30", "372.00", "100620.00"),
    _fill("2026-02-03T20:30:00Z", "JPM", "buy", "54", "199.20", "99850.00"),
    _fill("2026-02-20T20:30:00Z", "XOM", "buy", "75", "107.00", "101340.00"),
    _fill("2026-03-11T19:30:00Z", "JNJ", "buy", "60", "151.80", "100920.00"),
    _fill("2026-03-26T19:30:00Z", "COST", "buy", "12", "935.00", "102180.00"),
    _fill("2026-04-14T19:30:00Z", "HD", "buy", "30", "389.00", "101760.00"),
    _fill("2026-05-06T19:30:00Z", "GOOGL", "buy", "36", "179.80", "103420.00"),
    _fill("2026-05-28T19:30:00Z", "MSFT", "buy", "18", "411.31", "102950.00"),
    _fill("2026-06-12T19:30:00Z", "XOM", "buy", "30", "115.75", "104180.00"),
    _fill("2026-06-25T19:30:00Z", "GOOGL", "sell", "12", "190.00", "104520.00"),
    _fill("2026-07-02T19:30:00Z", "XOM", "sell", "15", "118.00", "104300.00"),
    _fill("2026-07-08T19:30:00Z", "HD", "sell", "6", "375.00", "104793.52"),
    _fill("2026-07-09T19:30:00Z", "NVDA", "buy", "20", "175.00", "104920.00"),
    _fill("2026-07-10T19:30:00Z", "AMZN", "buy", "20", "228.00", "104780.00"),
    _fill("2026-07-13T19:30:00Z", "AMZN", "sell", "5", "238.00", "105020.00"),
    _fill("2026-07-14T19:30:00Z", "WMT", "buy", "50", "102.00", "104870.00"),
)

DEMO_STARTING_CASH: Final = Decimal("100000.00")
DEMO_FINAL_CASH: Final = Decimal("9101.32")
DEMO_HOLDINGS_COST: Final = Decimal("91203.38")
DEMO_MARKET_VALUE: Final = Decimal("95952.20")
DEMO_UNREALIZED_PNL: Final = Decimal("4748.82")
DEMO_REALIZED_PNL: Final = Decimal("304.70")
DEMO_EQUITY: Final = Decimal("105053.52")

DEMO_DAILY_EQUITY: Final = (
    (datetime.fromisoformat("2026-07-15T20:00:00+00:00"), Decimal("104980.00")),
    (datetime.fromisoformat("2026-07-16T20:00:00+00:00"), Decimal("105110.00")),
    (datetime.fromisoformat("2026-07-17T20:00:00+00:00"), Decimal("104990.00")),
    (datetime.fromisoformat("2026-07-18T04:00:00+00:00"), DEMO_EQUITY),
)

HOLDING_BY_SYMBOL: Final = {holding.symbol: holding for holding in DEMO_HOLDINGS}


def configured_seed_profile() -> str | None:
    value = os.getenv("PAPER_ACCOUNT_SEED_PROFILE", SEED_PROFILE).strip().lower()
    return None if value in DISABLED_SEED_PROFILES else value


def fallback_price(symbol: str) -> Decimal | None:
    holding = HOLDING_BY_SYMBOL.get(symbol.strip().upper())
    return holding.fallback_price if holding else None


def seed_snapshots() -> list[dict[str, Any]]:
    cash = DEMO_STARTING_CASH
    positions: dict[str, dict[str, Decimal]] = {}
    snapshots: list[dict[str, Any]] = []
    for fill in DEMO_FILLS:
        position = positions.setdefault(
            fill.symbol,
            {"qty": Decimal("0"), "average_price": Decimal("0"), "realized_pnl": Decimal("0")},
        )
        if fill.side == "buy":
            old_qty = position["qty"]
            next_qty = old_qty + fill.quantity
            position["average_price"] = (
                old_qty * position["average_price"] + fill.quantity * fill.price
            ) / next_qty
            position["qty"] = next_qty
            cash -= fill.quantity * fill.price
        else:
            position["qty"] -= fill.quantity
            position["realized_pnl"] += (
                fill.price - position["average_price"]
            ) * fill.quantity
            cash += fill.quantity * fill.price
        rendered_positions = [
            {
                "symbol": symbol,
                "quantity": values["qty"],
                "averagePrice": values["average_price"],
                "purchaseAmountForeign": values["qty"] * values["average_price"],
                "realizedPnlForeign": values["realized_pnl"],
            }
            for symbol, values in sorted(positions.items())
            if values["qty"] > 0
        ]
        holdings_cost = sum(
            (item["purchaseAmountForeign"] for item in rendered_positions),
            Decimal("0"),
        )
        reported_pnl = fill.equity - cash - holdings_cost
        snapshots.append({
            "asOf": fill.filled_at.isoformat(),
            "source": "seeded-demo",
            "seedProfile": SEED_PROFILE,
            "valuationBasis": "fixture_mark_to_market",
            "account": {
                "cashForeign": cash,
                "stockValueForeign": fill.equity - cash,
                "totalValueForeign": fill.equity,
                "unrealizedPnlForeign": reported_pnl,
                "unrealizedPnlRate": (
                    reported_pnl / holdings_cost * Decimal("100")
                    if holdings_cost > 0 else Decimal("0")
                ),
            },
            "positions": rendered_positions,
        })
    return snapshots


def seed_snapshot_history() -> list[tuple[datetime, dict[str, Any]]]:
    after_snapshots = seed_snapshots()
    previous: dict[str, Any] = {
        "asOf": (DEMO_FILLS[0].filled_at - timedelta(microseconds=1)).isoformat(),
        "source": "seeded-demo",
        "seedProfile": SEED_PROFILE,
        "valuationBasis": "fixture_mark_to_market",
        "snapshotPhase": "before",
        "account": {
            "cashForeign": DEMO_STARTING_CASH,
            "stockValueForeign": Decimal("0"),
            "totalValueForeign": DEMO_STARTING_CASH,
            "unrealizedPnlForeign": Decimal("0"),
            "unrealizedPnlRate": Decimal("0"),
        },
        "positions": [],
    }
    history: list[tuple[datetime, dict[str, Any]]] = []
    for fill, after in zip(DEMO_FILLS, after_snapshots):
        before_at = fill.filled_at - timedelta(microseconds=1)
        before = deepcopy(previous)
        before["asOf"] = before_at.isoformat()
        before["snapshotPhase"] = "before"
        rendered_after = deepcopy(after)
        rendered_after["snapshotPhase"] = "after"
        history.extend(((before_at, before), (fill.filled_at, rendered_after)))
        previous = rendered_after
    for source_as_of, equity in DEMO_DAILY_EQUITY:
        market_value = equity - DEMO_FINAL_CASH
        price_scale = market_value / DEMO_MARKET_VALUE
        positions = [
            {
                "symbol": holding.symbol,
                "quantity": holding.quantity,
                "averagePrice": holding.average_price,
                "currentPrice": holding.fallback_price * price_scale,
                "marketValueForeign": holding.quantity * holding.fallback_price * price_scale,
                "purchaseAmountForeign": holding.quantity * holding.average_price,
                "realizedPnlForeign": Decimal("0"),
            }
            for holding in DEMO_HOLDINGS
        ]
        unrealized = market_value - DEMO_HOLDINGS_COST
        history.append((source_as_of, {
            "asOf": source_as_of.isoformat(),
            "source": "seeded-demo",
            "seedProfile": SEED_PROFILE,
            "valuationBasis": "fixture_mark_to_market",
            "snapshotPhase": "daily-close",
            "account": {
                "cashForeign": DEMO_FINAL_CASH,
                "stockValueForeign": market_value,
                "totalValueForeign": equity,
                "unrealizedPnlForeign": unrealized,
                "unrealizedPnlRate": unrealized / DEMO_HOLDINGS_COST * Decimal("100"),
            },
            "positions": positions,
        }))
    return history
