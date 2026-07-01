from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.market_data.backfill.service import get_backfill_service
from app.services.alfaka_market_data import get_market_data_provider, normalize_market_symbol, requested_ma_from_csv
from alfaka.serving.intervals import normalize_chart_interval, resolve_candle_limit


class MarketDataQueryService:
    def __init__(self, provider=None, backfill_service=None):
        self.provider = provider or get_market_data_provider()
        self.backfill_service = backfill_service or get_backfill_service(self.provider)

    def candle_snapshot(
        self,
        symbol: str,
        interval: str,
        ma: str,
        limit: int | None,
        before: str | None = None,
        from_time: str | None = None,
        to_time: str | None = None,
    ) -> dict[str, Any]:
        symbol = normalize_market_symbol(symbol)
        interval = normalize_chart_interval(interval)
        requested_ma = requested_ma_from_csv(ma)
        resolved_limit = resolve_candle_limit(interval, limit)
        try:
            payload = self.provider.candle_snapshot(symbol, interval, resolved_limit, before=before, from_time=from_time, to_time=to_time)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Market data provider failed: {exc}") from exc
        payload["indicators"] = {"ma": requested_ma, "volume": True}
        payload.update(self.backfill_service.snapshot_metadata(symbol, interval, payload))
        return payload

    def request_backfill(
        self,
        symbol: str,
        interval: str,
        start: str | None = None,
        end: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        symbol = normalize_market_symbol(symbol)
        interval = normalize_chart_interval(interval)
        try:
            self.provider.symbol_detail(symbol)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Symbol registry failed: {exc}") from exc
        return self.backfill_service.request_backfill(symbol, interval, start=start, end=end, force=force)

    def backfill_status(self, symbol: str, interval: str, request_id: str | None = None) -> dict[str, Any]:
        symbol = normalize_market_symbol(symbol)
        interval = normalize_chart_interval(interval)
        return self.backfill_service.get_status(symbol, interval, request_id=request_id)

    def backfill_queue_metrics(self) -> dict[str, Any]:
        return self.backfill_service.queue_metrics()

    def symbol_search(self, query: str, limit: int) -> dict[str, Any]:
        return {
            "source": "alpaca",
            "query": query,
            "symbols": self.provider.search_symbols(query, limit),
        }

    def symbol_detail(self, symbol: str) -> dict[str, Any]:
        try:
            return self.provider.symbol_detail(normalize_market_symbol(symbol))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def volume_profile_bins(self, symbol: str, from_time: str, to_time: str, price_bin_size: str) -> dict[str, Any]:
        symbol = normalize_market_symbol(symbol)
        return self.provider.volume_profile_bins(symbol, from_time, to_time, price_bin_size)

    def latest_status(self, symbol: str | None = None) -> dict[str, Any]:
        normalized = normalize_market_symbol(symbol) if symbol else None
        status = self.provider.latest_status(normalized)
        return status or {
            "symbol": normalized or "_MARKET",
            "statusType": "unknown",
            "status": "unknown",
            "source": "alpaca",
            "feed": "unknown",
        }

    def agent_chart_context(self, symbol: str, interval: str, from_time: str, to_time: str, include: str) -> dict[str, Any]:
        symbol = normalize_market_symbol(symbol)
        interval = normalize_chart_interval(interval)
        include_set = {item.strip() for item in include.split(",") if item.strip()}
        try:
            return self.provider.agent_chart_context(symbol, interval, from_time, to_time, include_set)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Market context provider failed: {exc}") from exc


def get_query_service() -> MarketDataQueryService:
    return MarketDataQueryService()
