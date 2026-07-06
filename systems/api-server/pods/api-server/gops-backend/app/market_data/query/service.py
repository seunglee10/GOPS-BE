from __future__ import annotations

import inspect
from typing import Any

from fastapi import HTTPException

from app.market_data.backfill.service import get_backfill_service
from app.market_data.fill.service import get_on_demand_fill_service
from app.market_data.heatmap.service import get_heatmap_service
from app.market_data.indices.service import get_indices_service
from app.services.alfaka_market_data import get_market_data_provider, normalize_market_symbol, requested_ma_from_csv
from alfaka.serving.chart_derived_data import (
    ChartDerivedArtifactStore,
    ChartDerivedDataClient,
    build_footprint_request,
    build_indicator_request,
    build_volume_profile_request,
    clickhouse_client_from_env,
    indicator_fetch_from_time,
)
from alfaka.serving.indicators import (
    indicator_required_lookback_bars,
    indicator_specs_from_csv,
)
from alfaka.serving.intervals import normalize_chart_interval, resolve_candle_limit
from alfaka.serving.volume_profile import (
    DEFAULT_VOLUME_PROFILE_TARGET_BINS,
    normalize_target_bins,
)
from alfaka.serving.news_hot_cache import company_daily_summary_coverage_valid
from alfaka.storage.news_daily_summary import attach_price_changes_to_daily_summaries, clickhouse_row_to_daily_summary


class MarketDataQueryService:
    def __init__(self, provider=None, backfill_service=None, fill_service=None, derived_client=None):
        self.provider = provider or get_market_data_provider()
        self.backfill_service = backfill_service or get_backfill_service(self.provider)
        self.fill_service = fill_service or get_on_demand_fill_service(self.provider)
        self.derived_client = derived_client or build_chart_derived_client(self.provider)

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
            payload = provider_candle_snapshot(
                self.provider,
                symbol,
                interval,
                resolved_limit,
                before=before,
                from_time=from_time,
                to_time=to_time,
                ma_windows=tuple(requested_ma),
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Market data provider failed: {exc}") from exc
        payload = self.fill_service.fill_if_needed(
            symbol=symbol,
            interval=interval,
            limit=resolved_limit,
            before=before,
            from_time=from_time,
            to_time=to_time,
            payload=payload,
        )
        filter_candle_moving_average_fields(payload, requested_ma)
        payload["indicators"] = {"ma": requested_ma, "volume": True}
        metadata = self.backfill_service.snapshot_metadata(symbol, interval, payload)
        payload.update(metadata)
        if isinstance(payload.get("fill"), dict):
            coverage = metadata.get("coverage") or {}
            payload["fill"] = {
                **payload["fill"],
                "status": normalize_fill_status(payload["fill"], metadata),
                "missingRanges": coverage.get("missingRanges") or payload["fill"].get("missingRanges") or [],
                "gapRanges": coverage.get("gapRanges") or payload["fill"].get("gapRanges") or [],
                "renderable": bool(coverage.get("renderable")),
                "minimumReturnedCount": coverage.get("minimumReturnedCount") or payload["fill"].get("minimumReturnedCount"),
                "minimumRenderableSourceBars": coverage.get("minimumRenderableSourceBars") or payload["fill"].get("minimumRenderableSourceBars"),
            }
        return payload

    def request_backfill(
        self,
        symbol: str,
        interval: str,
        start: str | None = None,
        end: str | None = None,
        mode: str = "default",
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
        return self.backfill_service.request_backfill(symbol, interval, start=start, end=end, mode=mode, force=force)

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

    def symbol_page(self, query: str, page: int, page_size: int) -> dict[str, Any]:
        from app.services.alfaka_market_data import market_symbol_page

        return market_symbol_page(query, page, page_size, backfill_service=self.backfill_service)

    def heatmap(self, universe: str) -> dict[str, Any]:
        try:
            return get_heatmap_service(self.provider).snapshot(universe)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def indices(self, background_tasks=None) -> dict[str, Any]:
        try:
            return get_indices_service(self.provider).snapshot(background_tasks=background_tasks)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Market indices provider failed: {exc}") from exc

    def symbol_detail(self, symbol: str) -> dict[str, Any]:
        try:
            return self.provider.symbol_detail(normalize_market_symbol(symbol))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def volume_profile_bins(
        self,
        symbol: str,
        from_time: str,
        to_time: str,
        price_bin_size: str,
        target_bins: int = DEFAULT_VOLUME_PROFILE_TARGET_BINS,
        price_min: float | None = None,
        price_max: float | None = None,
    ) -> dict[str, Any]:
        symbol = normalize_market_symbol(symbol)
        if price_min is not None and price_max is not None and price_max <= price_min:
            raise HTTPException(status_code=400, detail="priceMax must be greater than priceMin.")
        resolved_target_bins = normalize_target_bins(target_bins)
        request = build_volume_profile_request(
            symbol=symbol,
            from_time=from_time,
            to_time=to_time,
            price_bin_size=price_bin_size,
            target_bins=resolved_target_bins,
            price_min=price_min,
            price_max=price_max,
        )
        return self.derived_client.resolve(request)

    def indicator_series(
        self,
        symbol: str,
        interval: str,
        from_time: str | None = None,
        to_time: str | None = None,
        layers: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        symbol = normalize_market_symbol(symbol)
        interval = normalize_chart_interval(interval)
        try:
            specs = indicator_specs_from_csv(layers)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        requested_limit = resolve_candle_limit(interval, limit)

        lookback = indicator_required_lookback_bars(specs)
        fetch_limit = requested_limit + lookback
        fetch_from_time = indicator_fetch_from_time(interval, from_time, lookback)
        self.candle_snapshot(
            symbol,
            interval,
            "",
            fetch_limit,
            from_time=fetch_from_time,
            to_time=to_time,
        )
        request = build_indicator_request(
            symbol=symbol,
            interval=interval,
            from_time=from_time,
            to_time=to_time,
            specs=specs,
            limit=requested_limit,
        )
        return self.derived_client.resolve(request)

    def footprint_series(
        self,
        symbol: str,
        from_time: str,
        to_time: str,
        limit: int = 20000,
    ) -> dict[str, Any]:
        symbol = normalize_market_symbol(symbol)
        resolved_limit = max(1, min(int(limit), 100000))
        request = build_footprint_request(symbol=symbol, from_time=from_time, to_time=to_time, limit=resolved_limit)
        return self.derived_client.resolve(request)

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

    def latest_news(self, symbol: str, limit: int = 10, locale: str = "ko-KR") -> dict[str, Any]:
        symbol = normalize_market_symbol(symbol)
        limit = max(1, min(int(limit), 30))
        rows, source = self._latest_news_rows(symbol, limit, locale)
        return {
            "symbol": symbol,
            "source": source,
            "items": [normalize_news_item(row, symbol) for row in rows],
        }

    def daily_news(self, symbol: str, limit: int = 5, locale: str = "ko-KR") -> dict[str, Any]:
        symbol = normalize_market_symbol(symbol)
        limit = max(1, min(int(limit), 30))
        rows = self._daily_news_rows(symbol, limit, locale)
        summaries = [clickhouse_row_to_daily_summary(row) for row in rows]
        summaries = attach_price_changes_to_daily_summaries(summaries, self._daily_news_price_candles(symbol, limit))
        return {
            "symbol": symbol,
            "displayMode": "dailySummary",
            "dailySummaries": summaries,
        }

    def _latest_news_rows(self, symbol: str, limit: int, locale: str) -> tuple[list[dict[str, Any]], str]:
        redis_provider = getattr(self.provider, "redis_provider", None)
        clickhouse_provider = getattr(self.provider, "clickhouse_provider", None)
        redis_rows = self._localized_news_rows_from_provider(redis_provider, symbol, limit, locale, use_days=False)
        if len(redis_rows) >= limit:
            return redis_rows[:limit], "redis"

        clickhouse_rows = self._localized_news_rows_from_provider(clickhouse_provider, symbol, limit, locale, use_days=True)
        if clickhouse_rows:
            self._warm_localized_news_redis(redis_provider, clickhouse_rows, locale)
            return clickhouse_rows[:limit], "clickhouse"
        if redis_rows:
            return redis_rows[:limit], "redis"
        return [], "no-data"

    def _localized_news_rows_from_provider(self, provider, symbol: str, limit: int, locale: str, *, use_days: bool) -> list[dict[str, Any]]:
        if provider is None:
            return []
        method = getattr(provider, "localized_news_articles_for_symbols", None)
        if not callable(method):
            method = getattr(provider, "localized_news_articles", None)
            if not callable(method):
                return []
            try:
                if use_days:
                    rows = method(symbol, limit=limit, days=30, locale=locale)
                else:
                    rows = method(symbol, limit=limit, locale=locale)
            except TypeError:
                rows = method(symbol, limit=limit)
            except Exception:
                rows = []
        else:
            try:
                if use_days:
                    rows = method([symbol], limit=limit, days=30, locale=locale)
                else:
                    rows = method([symbol], limit=limit, locale=locale)
            except TypeError:
                rows = method([symbol], limit=limit)
            except Exception:
                rows = []
        return [row for row in rows or [] if isinstance(row, dict)]

    def _warm_localized_news_redis(self, redis_provider, rows: list[dict[str, Any]], locale: str) -> None:
        method = getattr(redis_provider, "warm_localized_news_articles", None)
        if not callable(method):
            return
        try:
            method(rows, locale=locale)
        except Exception:
            return

    def _daily_news_rows(self, symbol: str, limit: int, locale: str) -> list[dict[str, Any]]:
        redis_provider = getattr(self.provider, "redis_provider", None)
        clickhouse_provider = getattr(self.provider, "clickhouse_provider", None)
        days = 30
        redis_rows = self._daily_news_rows_from_provider(redis_provider, symbol, limit, locale, use_days=False)
        if self._daily_news_redis_coverage_valid(redis_provider, symbol, days, limit, locale, redis_rows):
            return redis_rows[:limit]

        clickhouse_rows = self._daily_news_rows_from_provider(clickhouse_provider, symbol, limit, locale, use_days=True)
        if clickhouse_rows:
            self._warm_daily_news_redis(redis_provider, symbol, clickhouse_rows, days, limit, locale)
            return clickhouse_rows[:limit]
        if redis_rows:
            return redis_rows[:limit]
        return []

    def _daily_news_rows_from_provider(self, provider, symbol: str, limit: int, locale: str, *, use_days: bool) -> list[dict[str, Any]]:
        if provider is None:
            return []
        method = getattr(provider, "company_daily_news_summaries", None)
        if not callable(method):
            return []
        try:
            if use_days:
                rows = method(symbol, limit=limit, days=30, locale=locale)
            else:
                rows = method(symbol, limit=limit, locale=locale)
        except TypeError:
            rows = method(symbol, limit=limit)
        except Exception:
            rows = []
        return [row for row in rows or [] if isinstance(row, dict)]

    def _daily_news_redis_coverage_valid(self, redis_provider, symbol: str, days: int, limit: int, locale: str, rows: list[dict[str, Any]]) -> bool:
        method = getattr(redis_provider, "company_daily_news_coverage", None)
        if not callable(method):
            return False
        try:
            coverage = method(symbol, locale=locale)
        except Exception:
            return False
        return company_daily_summary_coverage_valid(coverage, symbol=symbol, days=days, limit=limit, locale=locale, rows=rows)

    def _warm_daily_news_redis(self, redis_provider, symbol: str, rows: list[dict[str, Any]], days: int, limit: int, locale: str) -> None:
        method = getattr(redis_provider, "warm_company_daily_news_summaries", None)
        if not callable(method):
            return
        try:
            method(symbol, rows, days=days, limit=limit, locale=locale)
        except Exception:
            return

    def _daily_news_price_candles(self, symbol: str, limit: int) -> list[dict[str, Any]]:
        clickhouse_provider = getattr(self.provider, "clickhouse_provider", None)
        method = getattr(clickhouse_provider, "candles", None)
        if not callable(method):
            method = getattr(self.provider, "candles", None)
        if not callable(method):
            return []
        try:
            rows = method(symbol, "1D", max(2, limit + 10))
        except Exception:
            rows = []
        return [row for row in rows or [] if isinstance(row, dict)]

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


def build_chart_derived_client(provider: Any) -> ChartDerivedDataClient:
    clickhouse = getattr(provider, "clickhouse_provider", None)
    if clickhouse is None:
        clickhouse = clickhouse_client_from_env()
    return ChartDerivedDataClient(
        redis_client=redis_client_for_provider(provider),
        artifact_store=ChartDerivedArtifactStore(clickhouse),
    )


def normalize_fill_status(fill: dict[str, Any], metadata: dict[str, Any]) -> str:
    status = fill.get("status")
    data_status = metadata.get("dataStatus")
    if status in {"timeout", "failed"}:
        return status
    if data_status == "ready":
        return "not_needed" if status == "not_needed" else "filled"
    if data_status == "partial":
        return "partial"
    if data_status == "empty":
        return "empty" if status in {"not_needed", "empty"} else status or "empty"
    return status or "partial"


def redis_client_for_provider(provider: Any):
    redis_provider = getattr(provider, "redis_provider", None)
    return getattr(redis_provider, "redis", None)


def filter_candle_moving_average_fields(payload: dict[str, Any], requested_ma: list[int]) -> None:
    allowed_keys = {f"ma{window}" for window in requested_ma}
    for candle in payload.get("candles") or []:
        if not isinstance(candle, dict):
            continue
        for key in ("ma5", "ma20", "ma60"):
            if key not in allowed_keys:
                candle.pop(key, None)
        nested = candle.get("ma")
        if isinstance(nested, dict):
            for key in list(nested.keys()):
                if key in {"ma5", "ma20", "ma60"} and key not in allowed_keys:
                    nested.pop(key, None)


def provider_candle_snapshot(
    provider: Any,
    symbol: str,
    interval: str,
    limit: int,
    *,
    before: str | None = None,
    from_time: str | None = None,
    to_time: str | None = None,
    ma_windows: tuple[int, ...] = (),
) -> dict[str, Any]:
    if provider_accepts_ma_windows(provider):
        return provider.candle_snapshot(
            symbol,
            interval,
            limit,
            before=before,
            from_time=from_time,
            to_time=to_time,
            ma_windows=ma_windows,
        )
    return provider.candle_snapshot(symbol, interval, limit, before=before, from_time=from_time, to_time=to_time)


def provider_accepts_ma_windows(provider: Any) -> bool:
    try:
        return "ma_windows" in inspect.signature(provider.candle_snapshot).parameters
    except (TypeError, ValueError):
        return False


def normalize_news_item(row: dict[str, Any], fallback_symbol: str) -> dict[str, Any]:
    title = read_string(row.get("title")) or read_string(row.get("localizedTitle")) or read_string(row.get("localizedHeadline")) or read_string(row.get("headline")) or "Untitled news"
    summary = read_string(row.get("summary")) or read_string(row.get("localizedSummary")) or read_string(row.get("localized_summary")) or ""
    symbols = read_string_list(row.get("symbols"))
    symbol = read_string(row.get("targetSymbol")) or read_string(row.get("target_symbol")) or read_string(row.get("symbol")) or fallback_symbol
    return {
        "symbol": symbol.upper(),
        "symbols": symbols or [symbol.upper()],
        "title": title,
        "summary": summary,
        "url": read_string(row.get("url")),
        "source": read_string(row.get("source")),
        "publishedAt": read_string(row.get("publishedAt")) or read_string(row.get("published_at")),
        "impactDirection": read_string(row.get("impactDirection")) or read_string(row.get("impact_direction")),
    }


def read_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def read_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip().upper()] if value.strip() else []
    if not isinstance(value, list):
        return []
    symbols = []
    for item in value:
        text = read_string(item)
        if text and text.upper() not in symbols:
            symbols.append(text.upper())
    return symbols
