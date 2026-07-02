from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from alfaka.alpaca.subscription import configured_universe_symbols, load_request_config, validate_symbol


logger = logging.getLogger(__name__)


class SymbolRegistry:
    def __init__(self, clickhouse_provider=None, redis_provider=None):
        self.clickhouse_provider = clickhouse_provider
        self.redis_provider = redis_provider
        self.config = load_request_config()

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        normalized_query = query.strip().upper()
        results = self._universe_matches(normalized_query)
        universe_symbols = set(self._universe_symbols())
        if self.clickhouse_provider:
            try:
                results.extend([
                    item
                    for item in self.clickhouse_provider.search_symbols(normalized_query, max(limit, 40))
                    if isinstance(item, dict) and (not universe_symbols or item.get("symbol") in universe_symbols)
                ])
            except Exception:
                logger.warning("ClickHouse symbol search failed; falling back to configured symbols.", exc_info=True)
                results.extend([])

        if self._is_universe_symbol(normalized_query):
            results.append(self._metadata_for_symbol(normalized_query))

        deduped = {}
        for item in results:
            symbol = item.get("symbol")
            if symbol and symbol not in deduped:
                deduped[symbol] = item
        return list(deduped.values())[:limit]

    def detail(self, symbol: str) -> dict[str, Any]:
        normalized = symbol.strip().upper()
        validate_symbol(normalized, self.config)
        if self.clickhouse_provider:
            try:
                stored = self.clickhouse_provider.symbol(normalized)
                if stored:
                    return stored
            except Exception:
                logger.warning("ClickHouse symbol detail failed; trying Redis/config fallback.", exc_info=True)
                pass
        if self.redis_provider:
            try:
                cached = self.redis_provider.symbol_metadata(normalized)
                if cached:
                    return cached
            except Exception:
                logger.warning("Redis symbol detail failed; trying config fallback.", exc_info=True)
                pass
        if self._is_universe_symbol(normalized):
            return self._metadata_for_symbol(normalized)
        raise LookupError(f"Unknown market symbol: {normalized}")

    def _universe_matches(self, query: str) -> list[dict[str, Any]]:
        matches = []
        for index, symbol in enumerate(self._universe_symbols()):
            metadata = self._metadata_for_symbol(symbol)
            if not query:
                matches.append(metadata)
                continue
            haystack = f"{metadata['symbol']} {metadata['name']}".upper()
            if query in haystack:
                matches.append({**metadata, "_matchScore": self._match_score(metadata, query), "_universeOrder": index})
        if not query:
            return matches
        matches.sort(key=lambda item: (item.get("_matchScore", 99), item.get("_universeOrder", 999999), item["symbol"]))
        return [
            {key: value for key, value in item.items() if not key.startswith("_")}
            for item in matches
        ]

    def _match_score(self, metadata: dict[str, Any], query: str) -> int:
        symbol = metadata["symbol"].upper()
        name = metadata["name"].upper()
        if symbol == query:
            return 0
        if symbol.startswith(query):
            return 1
        if name.startswith(query):
            return 2
        if query in symbol:
            return 3
        if query in name:
            return 4
        return 5

    def _metadata_for_symbol(self, symbol: str) -> dict[str, Any]:
        configured_metadata = (self.config.get("symbolMetadata") or {}).get(symbol) or {}
        names = {
            value.upper(): key.title()
            for key, value in (self.config.get("companyToSymbol") or {}).items()
        }
        exchange = configured_metadata.get("exchange")
        market = configured_metadata.get("market") or exchange or "US"
        return {
            "symbol": symbol,
            "name": configured_metadata.get("name") or names.get(symbol, symbol),
            "exchange": exchange,
            "market": market,
            "assetClass": configured_metadata.get("assetClass") or "us_equity",
            "tradable": configured_metadata.get("tradable", True),
            "status": configured_metadata.get("status") or "unknown",
            "source": configured_metadata.get("source") or "alpaca",
            "updatedAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }

    def _is_valid_symbol(self, symbol: str) -> bool:
        try:
            validate_symbol(symbol, self.config)
            return True
        except ValueError:
            return False

    def _is_universe_symbol(self, symbol: str) -> bool:
        return symbol in self._universe_symbols()

    def _universe_symbols(self) -> list[str]:
        symbols = [
            symbol
            for symbol in configured_universe_symbols(self.config)
            if self._is_valid_symbol(symbol)
        ]
        if symbols:
            return symbols
        return [
            str(symbol).strip().upper()
            for symbol in self.config.get("defaultSymbols") or []
            if isinstance(symbol, str) and self._is_valid_symbol(str(symbol).strip().upper())
        ]
