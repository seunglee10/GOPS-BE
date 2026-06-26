from typing import Any

from fastapi import APIRouter, Query

from backend.app.services.market_data import build_dummy_candles, build_symbol_summary, normalize_dummy_symbol, supported_dummy_symbols

router = APIRouter()


@router.get("/api/charts/candles")
def chart_candles(
    symbol: str = Query(default="AAPL", min_length=1, max_length=12),
    interval: str = Query(default="1m", pattern="^(1m|5m|10m)$"),
    ma: str = Query(default="5,20,60"),
    limit: int = Query(default=96, ge=30, le=240),
) -> dict[str, Any]:
    symbol = normalize_dummy_symbol(symbol)
    requested_ma = [
        int(item)
        for item in ma.split(",")
        if item.strip().isdigit() and int(item) in {5, 20, 60}
    ]

    return {
        "symbol": symbol,
        "interval": interval,
        "source": "dummy",
        "feed": "synthetic-demo",
        "isSynthetic": True,
        "notice": "Synthetic development candles. Do not present as real market data.",
        "indicators": {
            "ma": requested_ma or [5, 20, 60],
            "volume": True,
        },
        "candles": build_dummy_candles(symbol, interval, limit),
    }


@router.get("/api/charts/symbols")
def chart_symbols() -> dict[str, Any]:
    return {
        "source": "dummy",
        "feed": "synthetic-demo",
        "isSynthetic": True,
        "symbols": [build_symbol_summary(symbol) for symbol in supported_dummy_symbols()],
    }
