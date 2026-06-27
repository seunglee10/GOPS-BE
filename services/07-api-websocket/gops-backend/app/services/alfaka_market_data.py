# 역할: GOPS backend가 alfaka Redis/ClickHouse provider를 읽게 연결합니다.
# 사용: 과거 캔들은 ClickHouse, 최신/실시간 캔들은 Redis에서 가져옵니다.
# 설정: ALPACA_SYMBOLS, REDIS_URL, CLICKHOUSE_* 값을 .env 또는 Docker env에 넣습니다.
from __future__ import annotations

import os
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import HTTPException


SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{0,9}(?:\.[A-Z])?$")
DEFAULT_SYMBOLS = ["AAPL", "TSLA", "NVDA"]


def _add_alfaka_package_path() -> None:
    # 로컬 병합 위치에서는 프로젝트 루트/packages 안에 alfaka 패키지가 있습니다.
    # Docker에서는 ALFAKA_PACKAGES_PATH 또는 /app/packages를 먼저 확인합니다.
    candidates = [os.getenv("ALFAKA_PACKAGES_PATH"), "/app/packages"]
    current_file = Path(__file__).resolve()
    candidates.extend(str(parent / "packages") for parent in current_file.parents)

    for candidate in candidates:
        if not candidate:
            continue
        package_path = Path(candidate)
        if (package_path / "alfaka").exists() and str(package_path) not in sys.path:
            sys.path.insert(0, str(package_path))
            return


_add_alfaka_package_path()

from alfaka.alpaca.subscription import load_request_config  # noqa: E402
from alfaka.serving.intervals import candle_count_for_24h  # noqa: E402
from alfaka.serving.provider import MarketDataProvider  # noqa: E402


def configured_symbols() -> list[str]:
    # 사용자가 실제로 Alpaca에서 받을 종목은 ALPACA_SYMBOLS에 넣습니다.
    # 값이 없으면 config/market-data-request.json의 semiconductor-100 universe를 씁니다.
    config = load_request_config()
    raw_symbols = os.getenv("ALPACA_SYMBOLS", ",".join(config.get("defaultSymbols") or DEFAULT_SYMBOLS))
    symbols = [normalize_market_symbol(item) for item in raw_symbols.split(",") if item.strip()]
    return symbols or DEFAULT_SYMBOLS


def normalize_market_symbol(symbol: str) -> str:
    # 프론트에서 들어온 회사명/심볼은 여기서 Alpaca 심볼 형식으로 정리합니다.
    # 한글 회사명 매핑은 subscription.py 쪽 신청 로직에서 관리하고, API는 심볼을 받습니다.
    normalized = symbol.strip().upper()
    if not SYMBOL_PATTERN.match(normalized):
        raise HTTPException(status_code=400, detail=f"Invalid market symbol: {normalized}")
    return normalized


def requested_ma_from_csv(value: str) -> list[int]:
    # 프론트가 ma=5,20,60처럼 요청하면 허용된 이동평균선만 남깁니다.
    # 실제 ma 값은 Flink/local processor가 만든 캔들 payload의 ma5/ma20/ma60을 씁니다.
    requested = []
    for item in value.split(","):
        item = item.strip()
        if item.isdigit() and int(item) in {5, 20, 60}:
            requested.append(int(item))
    return requested or [5, 20, 60]


@lru_cache(maxsize=1)
def get_market_data_provider() -> MarketDataProvider:
    # GOPS API와 WebSocket이 공통으로 쓰는 provider입니다.
    # 내부에서 Redis 최근 캔들 + ClickHouse 과거 캔들을 합쳐 GOPS DTO로 반환합니다.
    return MarketDataProvider()


def symbol_summaries() -> list[dict[str, Any]]:
    # 프론트의 관심종목/티커 영역이 읽는 요약 데이터입니다.
    # Redis에 최신 가격이 없으면 ClickHouse serving projection의 최신 1m candle로 보완합니다.
    return [build_symbol_summary(symbol) for symbol in configured_symbols()]


def build_symbol_summary(symbol: str) -> dict[str, Any]:
    provider = get_market_data_provider()
    symbol = normalize_market_symbol(symbol)
    latest_price = _safe_latest_price(provider, symbol)
    candles = _safe_recent_candles(provider, symbol, 30)
    if not candles:
        candles = _safe_clickhouse_candles(provider, symbol, candle_count_for_24h("1m"))
    last_candle = candles[-1] if candles else {}
    first_candle = candles[0] if candles else {}
    last_price = _read_float(latest_price.get("price")) or _read_float(last_candle.get("close"))
    first_price = _read_float(first_candle.get("open")) or last_price

    change_percent = None
    if last_price is not None and first_price not in (None, 0):
        change_percent = round(((last_price - first_price) / first_price) * 100, 2)

    return {
        "symbol": symbol,
        "name": symbol,
        "market": "US",
        "lastPrice": last_price,
        "changePercent": change_percent,
        "volume": _read_float(last_candle.get("volume")),
    }


def _safe_latest_price(provider: MarketDataProvider, symbol: str) -> dict[str, Any]:
    try:
        return provider.redis_provider.latest_price(symbol) or {}
    except Exception:
        return {}


def _safe_recent_candles(provider: MarketDataProvider, symbol: str, limit: int) -> list[dict[str, Any]]:
    try:
        return provider.redis_provider.recent_candles(symbol, "1m", limit)
    except Exception:
        return []


def _safe_clickhouse_candles(provider: MarketDataProvider, symbol: str, limit: int) -> list[dict[str, Any]]:
    try:
        return provider.clickhouse_provider.candles(symbol, "1m", limit)
    except Exception:
        return []


def _read_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
