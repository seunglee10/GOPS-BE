from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from alfaka.alpaca.subscription import configured_universe_symbols, load_request_config, validate_symbol


logger = logging.getLogger(__name__)


class SymbolRegistry:
    def __init__(self, clickhouse_provider=None, redis_provider=None):
        """심볼 검색/상세 조회에 필요한 ClickHouse, Redis, 설정 파일을 준비합니다."""
        self.clickhouse_provider = clickhouse_provider
        self.redis_provider = redis_provider
        self.config = load_request_config()

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """설정 universe와 provider 저장 데이터를 합쳐 심볼 검색 결과를 반환합니다."""
        normalized_query = query.strip().upper()
        results = self._universe_matches(normalized_query)
        provider_filter_symbols = self._provider_filter_symbols()
        if self.clickhouse_provider:
            try:
                results.extend([
                    item
                    for item in self.clickhouse_provider.search_symbols(normalized_query, max(limit, 40))
                    if isinstance(item, dict) and (not provider_filter_symbols or item.get("symbol") in provider_filter_symbols)
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
        """단일 심볼의 메타데이터를 ClickHouse, Redis, 설정 순서로 조회합니다."""
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
        """설정에 들어 있는 universe/extra 심볼 중 검색어와 맞는 항목을 찾습니다."""
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
        """심볼/이름이 검색어와 얼마나 가깝게 맞는지 정렬 점수를 계산합니다."""
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
        """설정 파일의 symbolMetadata를 차트 API가 쓰는 메타데이터 형태로 바꿉니다."""
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
        """설정된 심볼 패턴을 통과하는지 bool로 확인합니다."""
        try:
            validate_symbol(symbol, self.config)
            return True
        except ValueError:
            return False

    def _is_universe_symbol(self, symbol: str) -> bool:
        """심볼이 현재 registry가 노출하는 universe에 포함되는지 확인합니다."""
        return symbol in self._universe_symbols()

    def _provider_filter_symbols(self) -> set[str]:
        """provider 검색 결과를 제한할 registry/universe 심볼 집합을 만듭니다."""
        return {
            symbol
            for symbol in self._configured_registry_universe_symbols()
            if self._is_valid_symbol(symbol)
        }

    def _universe_symbols(self) -> list[str]:
        """검색/상세 fallback에 쓸 설정 기반 universe와 extraSymbols를 합칩니다."""
        symbols = [
            symbol
            for symbol in self._configured_registry_universe_symbols()
            if self._is_valid_symbol(symbol)
        ]
        extra_symbols = [
            str(symbol).strip().upper()
            for symbol in self.config.get("extraSymbols") or []
            if isinstance(symbol, str) and self._is_valid_symbol(str(symbol).strip().upper())
        ]
        symbols.extend(symbol for symbol in extra_symbols if symbol not in symbols)
        if symbols:
            return symbols
        symbols = [
            str(symbol).strip().upper()
            for symbol in self.config.get("defaultSymbols") or []
            if isinstance(symbol, str) and self._is_valid_symbol(str(symbol).strip().upper())
        ]
        return [*symbols, *[symbol for symbol in extra_symbols if symbol not in symbols]]

    def _configured_registry_universe_symbols(self) -> list[str]:
        """collection source가 registry/universe일 때만 대형 universe 심볼을 읽습니다."""
        source = (os.getenv("ALPACA_COLLECTION_SYMBOL_SOURCE") or self.config.get("collectionSymbolSource") or "").strip().lower()
        if source not in {"universe", "registry"}:
            return []
        return configured_universe_symbols(self.config)
