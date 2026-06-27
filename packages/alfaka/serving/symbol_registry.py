from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from alfaka.alpaca.subscription import load_request_config, validate_symbol


logger = logging.getLogger(__name__)


class SymbolRegistry:
    def __init__(self, clickhouse_provider=None, redis_provider=None):
        self.clickhouse_provider = clickhouse_provider
        self.redis_provider = redis_provider
        self.config = load_request_config()

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        normalized_query = query.strip().upper()
        results = []
        if self.clickhouse_provider:
            try:
                results.extend(self.clickhouse_provider.search_symbols(normalized_query, limit))
            except Exception:
                logger.warning("ClickHouse symbol search failed; falling back to configured symbols.", exc_info=True)
                results.extend([])

        results.extend(self._configured_matches(normalized_query))
        if self._is_configured_symbol(normalized_query):
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
        if self._is_configured_symbol(normalized):
            return self._metadata_for_symbol(normalized)
        raise LookupError(f"Unknown market symbol: {normalized}")

    def _configured_matches(self, query: str) -> list[dict[str, Any]]:
        if not query:
            return []
        matches = []
        for symbol in self._configured_symbols():
            metadata = self._metadata_for_symbol(symbol)
            haystack = f"{metadata['symbol']} {metadata['name']}".upper()
            if query in haystack:
                matches.append(metadata)
        return matches

    def _metadata_for_symbol(self, symbol: str) -> dict[str, Any]:
        names = {
            value.upper(): key.title()
            for key, value in (self.config.get("companyToSymbol") or {}).items()
        }
        return {
            "symbol": symbol,
            "name": names.get(symbol, symbol),
            "exchange": None,
            "market": "US",
            "assetClass": "us_equity",
            "tradable": True,
            "status": "unknown",
            "source": "alpaca",
            "updatedAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }

    def _is_valid_symbol(self, symbol: str) -> bool:
        try:
            validate_symbol(symbol, self.config)
            return True
        except ValueError:
            return False

    def _is_configured_symbol(self, symbol: str) -> bool:
        return symbol in self._configured_symbols()

    def _configured_symbols(self) -> set[str]:
        raw_symbols = os.getenv("ALPACA_SYMBOLS")
        values = [*self.config.get("defaultSymbols", [])]
        if raw_symbols:
            values.extend(raw_symbols.split(","))
        return {
            symbol.strip().upper()
            for symbol in values
            if isinstance(symbol, str) and symbol.strip() and self._is_valid_symbol(symbol.strip().upper())
        }
