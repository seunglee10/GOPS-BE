from __future__ import annotations

import inspect
from collections import OrderedDict
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.market_data.backfill.service import get_backfill_service
from app.market_data.fill.service import get_on_demand_fill_service
from app.market_data.fundamentals.service import build_fundamentals_adapter
from app.market_data.heatmap.service import get_heatmap_service
from app.market_data.indices.service import get_indices_service
from app.market_data.realtime.subscription_cohorts import RealtimeSubscriptionCohortService
from app.services.alfaka_market_data import get_market_data_provider, normalize_market_symbol, requested_ma_from_csv, sp500_universe_symbols
from alfaka.serving.chart_derived_data import (
    ChartDerivedArtifactStore,
    ChartDerivedDataClient,
    DERIVED_KIND_INDICATORS,
    build_indicator_request,
    build_volume_profile_request,
    clickhouse_client_from_env,
    indicator_fetch_from_time,
    read_json_cache,
    redis_ttl_seconds,
    with_derived_metadata,
    write_json_cache,
)
from alfaka.serving.indicators import (
    compute_indicator_payload,
    indicator_required_lookback_bars,
    indicator_specs_from_csv,
)
from alfaka.serving.intervals import normalize_chart_interval, resolve_candle_limit
from alfaka.orderflow import (
    ORDER_FLOW_CLASSIFICATION_VERSION,
    ORDER_FLOW_SIDE_CLASSIFICATION,
    pinned_symbols_from_env,
    price_bin_size_from_env,
)
from alfaka.serving.volume_profile import (
    DEFAULT_VOLUME_PROFILE_TARGET_BINS,
    normalize_target_bins,
)
from alfaka.serving.news_hot_cache import company_daily_summary_coverage_valid
from alfaka.storage.news_daily_summary import attach_price_changes_to_daily_summaries, clickhouse_row_to_daily_summary


WATCHLIST_NEWS_MODES = {"watchlist", "hot", "recommended"}
HOT_NEWS_RANKING_KINDS = (
    ("gainers", "급등"),
    ("losers", "급락"),
    ("dollar-volume", "거래대금"),
)
MARKET_TIMEZONE = ZoneInfo("America/New_York")


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
        include_previous_close: bool = False,
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
        if include_previous_close:
            payload["previousClose"] = previous_close_from_provider(self.provider, symbol)
        if isinstance(payload.get("fill"), dict):
            coverage = metadata.get("coverage") or {}
            payload["fill"] = {
                **payload["fill"],
                "status": normalize_fill_status(payload["fill"], metadata),
                "missingRanges": coverage.get("missingRanges") or payload["fill"].get("missingRanges") or [],
                "gapRanges": coverage.get("gapRanges") or payload["fill"].get("gapRanges") or [],
                "renderable": bool(coverage.get("renderable") or payload["fill"].get("renderable")),
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

    def financial_series(self, symbol: str, years: int, period: str) -> dict[str, Any]:
        try:
            normalized = normalize_market_symbol(symbol)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        normalized_period = "annual" if period.lower() in {"annual", "year", "fy"} else "quarterly"
        adapter = build_fundamentals_adapter(self.provider)
        series = adapter.financial_series(normalized, years=years, period=normalized_period)
        return {
            "source": "sec",
            "symbol": normalized,
            "period": normalized_period,
            "years": years,
            "items": [point.to_public_dict() for point in series],
        }

    def earnings_series(self, symbol: str, years: int) -> dict[str, Any]:
        try:
            normalized = normalize_market_symbol(symbol)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        adapter = build_fundamentals_adapter(self.provider)
        series = adapter.earnings_series(normalized, years=years)
        return {
            "source": "sec-yahoo",
            "symbol": normalized,
            "years": years,
            "items": [point.to_public_dict() for point in series],
        }

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
        interval: str = "1m",
    ) -> dict[str, Any]:
        symbol = normalize_market_symbol(symbol)
        interval = normalize_chart_interval(interval)
        if price_min is not None and price_max is not None and price_max <= price_min:
            raise HTTPException(status_code=400, detail="priceMax must be greater than priceMin.")
        resolved_target_bins = normalize_target_bins(target_bins)
        request = build_volume_profile_request(
            symbol=symbol,
            interval=interval,
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
        request = build_indicator_request(
            symbol=symbol,
            interval=interval,
            from_time=from_time,
            to_time=to_time,
            specs=specs,
            limit=requested_limit,
        )
        cached_payload = cached_inline_indicator_payload(self.derived_client, request)
        if cached_payload is not None:
            return cached_payload
        lookback = indicator_required_lookback_bars(specs)
        fetch_limit = requested_limit + lookback
        if from_time and lookback > 0:
            warmup_payload = self.candle_snapshot(
                symbol,
                interval,
                "",
                lookback,
                before=from_time,
            )
            range_payload = self.candle_snapshot(
                symbol,
                interval,
                "",
                requested_limit,
                from_time=from_time,
                to_time=to_time,
            )
            candle_payload = merge_candle_payloads(warmup_payload, range_payload)
        else:
            fetch_from_time = indicator_fetch_from_time(interval, from_time, lookback)
            candle_payload = self.candle_snapshot(
                symbol,
                interval,
                "",
                fetch_limit,
                from_time=fetch_from_time,
                to_time=to_time,
            )
        payload = inline_indicator_payload(
            request,
            candle_payload,
            specs,
            from_time=from_time,
            to_time=to_time,
            requested_limit=requested_limit,
            lookback=lookback,
        )
        write_inline_indicator_cache(self.derived_client, request, payload)
        return payload

    def order_flow_symbols(self) -> dict[str, Any]:
        return {
            "symbols": sorted(pinned_symbols_from_env()),
            "priceBinSize": price_bin_size_from_env(),
            "sideClassification": ORDER_FLOW_SIDE_CLASSIFICATION,
            "classificationVersion": ORDER_FLOW_CLASSIFICATION_VERSION,
        }

    def order_flow_daily(self, symbol: str, from_date: str, to_date: str, limit_days: int = 60) -> dict[str, Any]:
        """Reads tiny immutable daily order-flow rows directly from ClickHouse; no cache in MVP."""
        symbol = normalize_market_symbol(symbol)
        supported = sorted(pinned_symbols_from_env())
        if symbol not in set(supported):
            return self._unsupported_order_flow_payload(symbol, days=True, supported=supported, from_date=from_date, to_date=to_date)
        start = parse_date_arg(from_date, "from")
        end = parse_date_arg(to_date, "to")
        if end < start:
            raise ValueError("to must be on or after from.")
        limit_days = max(1, min(int(limit_days), 250))
        clickhouse_provider = getattr(self.provider, "clickhouse_provider", None)
        method = getattr(clickhouse_provider, "order_flow_daily_profiles", None)
        if not callable(method):
            raise HTTPException(status_code=503, detail="Order-flow ClickHouse provider is unavailable.")
        row_limit = max(1000, limit_days * 5000)
        query_limit = row_limit + 1
        try:
            rows = list(method(symbol, start.isoformat(), end.isoformat(), limit=query_limit) or [])
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Market data provider failed: {exc}") from exc
        if len(rows) > query_limit:
            rows = rows[:query_limit]
        hit_limit = len(rows) > row_limit
        today = datetime.now(MARKET_TIMEZONE).date().isoformat()
        grouped: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
        for row in rows:
            session_date = str(row.get("sessionDate") or row.get("session_date") or "")
            if not session_date or session_date >= today:
                continue
            grouped.setdefault(session_date, []).append(row)
        if hit_limit and grouped:
            grouped.popitem(last=True)
        selected_dates = list(grouped)[:limit_days]
        days = [
            order_flow_day_payload(session_date, grouped[session_date])
            for session_date in sorted(selected_dates)
        ]
        return {
            "symbol": symbol,
            "priceBinSize": price_bin_size_from_env(),
            "sideClassification": ORDER_FLOW_SIDE_CLASSIFICATION,
            "classificationVersion": ORDER_FLOW_CLASSIFICATION_VERSION,
            "marketSession": "regular",
            "from": start.isoformat(),
            "to": end.isoformat(),
            "dataStatus": "ready" if days else "empty",
            "days": days,
        }

    def order_flow_intraday(self, symbol: str) -> dict[str, Any]:
        symbol = normalize_market_symbol(symbol)
        supported = sorted(pinned_symbols_from_env())
        if symbol not in set(supported):
            return self._unsupported_order_flow_payload(symbol, days=False, supported=supported)
        redis_provider = getattr(self.provider, "redis_provider", None)
        method = getattr(redis_provider, "order_flow_live_bins", None)
        if not callable(method):
            raise HTTPException(status_code=503, detail="Order-flow Redis provider is unavailable.")
        current_session_date = datetime.now(MARKET_TIMEZONE).date().isoformat()
        try:
            bins = [
                bin_payload for bin_payload in method(symbol) or []
                if str(bin_payload.get("sessionDate") or "") == current_session_date
            ]
            live_quote = redis_provider.live_quote(symbol) if callable(getattr(redis_provider, "live_quote", None)) else None
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Market data provider failed: {exc}") from exc
        grouped: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
        for bin_payload in bins:
            minute = str(bin_payload.get("eventMinute") or "")
            if minute:
                grouped.setdefault(minute, []).append(bin_payload)
        minutes = [
            {
                "eventMinute": minute,
                "bins": [order_flow_level_payload(item) for item in sorted(items, key=lambda row: float(row.get("priceBin") or 0))],
            }
            for minute, items in sorted(grouped.items())
        ]
        return {
            "symbol": symbol,
            "sessionDate": current_session_date,
            "priceBinSize": price_bin_size_from_env(),
            "sideClassification": ORDER_FLOW_SIDE_CLASSIFICATION,
            "classificationVersion": ORDER_FLOW_CLASSIFICATION_VERSION,
            "marketSession": "regular",
            "dataStatus": "ready" if minutes else "empty",
            "minutes": minutes,
            "liveQuote": live_quote,
        }

    def _unsupported_order_flow_payload(
        self,
        symbol: str,
        *,
        days: bool,
        supported: list[str],
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "symbol": symbol,
            "priceBinSize": price_bin_size_from_env(),
            "sideClassification": ORDER_FLOW_SIDE_CLASSIFICATION,
            "classificationVersion": ORDER_FLOW_CLASSIFICATION_VERSION,
            "marketSession": "regular",
            "dataStatus": "unsupported",
            "supportedSymbols": supported,
        }
        if days:
            payload.update({"from": from_date, "to": to_date, "days": []})
        else:
            payload.update({"sessionDate": datetime.now(MARKET_TIMEZONE).date().isoformat(), "minutes": [], "liveQuote": None})
        return payload

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

    def watchlist_news(
        self,
        user_sub: str,
        limit: int = 30,
        locale: str = "ko-KR",
        mode: str = "watchlist",
        recommendation_repository: Any | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 50))
        normalized_mode = normalize_watchlist_news_mode(mode)
        if normalized_mode == "hot":
            symbols, reasons_by_symbol = self._hot_news_symbols(limit)
            return self._symbol_group_news(
                symbols,
                limit,
                locale,
                display_mode="hotNews",
                empty_source="hot",
                empty_message="인기순 기준 종목을 찾지 못했습니다.",
                no_news_message="인기순 종목 관련 저장 뉴스가 없습니다.",
                reasons_by_symbol=reasons_by_symbol,
            )
        if normalized_mode == "recommended":
            symbols, reasons_by_symbol = self._recommended_news_symbols(user_sub, recommendation_repository)
            return self._symbol_group_news(
                symbols,
                limit,
                locale,
                display_mode="recommendedNews",
                empty_source="recommendations",
                empty_message="추천 기업이 생성되면 관련 뉴스가 표시됩니다.",
                no_news_message="추천 기업 관련 저장 뉴스가 없습니다.",
                reasons_by_symbol=reasons_by_symbol,
            )

        symbols = self._user_watchlist_symbols(user_sub)
        return self._symbol_group_news(
            symbols,
            limit,
            locale,
            display_mode="watchlistNews",
            empty_source="watchlist",
            empty_message="관심종목을 추가하면 관련 뉴스가 표시됩니다.",
            no_news_message="관심종목 관련 저장 뉴스가 없습니다.",
        )

    def _symbol_group_news(
        self,
        symbols: list[str],
        limit: int,
        locale: str,
        *,
        display_mode: str,
        empty_source: str,
        empty_message: str,
        no_news_message: str,
        reasons_by_symbol: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        if not symbols:
            return {
                "source": empty_source,
                "displayMode": display_mode,
                "symbols": [],
                "items": [],
                "message": empty_message,
            }

        rows, source = self._watchlist_news_rows(symbols, limit, locale)
        rows = dedupe_news_rows(rows, symbols)
        company_names = self._watchlist_company_names(symbols)
        items = []
        for row in rows[:limit]:
            matches = watchlist_news_matches(row, symbols, company_names, reasons_by_symbol)
            fallback_symbol = matches[0]["symbol"] if matches else symbols[0]
            item = normalize_news_item(row, fallback_symbol)
            item["matches"] = matches
            items.append(item)
        return {
            "source": source,
            "displayMode": display_mode,
            "symbols": symbols,
            "items": items,
            "message": "" if items else no_news_message,
        }

    def _hot_news_symbols(self, limit: int) -> tuple[list[str], dict[str, list[str]]]:
        universe = sp500_universe_symbols()
        if not universe:
            return [], {}
        symbols: list[str] = []
        reasons_by_symbol: dict[str, list[str]] = {}
        rank_limit = min(10, max(1, int(limit)))
        for kind, reason in HOT_NEWS_RANKING_KINDS:
            for row in self._ranked_symbol_rows(universe, kind=kind, limit=rank_limit):
                symbol = normalized_symbol_from_value(row.get("symbol"))
                if not symbol:
                    continue
                if symbol not in symbols:
                    symbols.append(symbol)
                reasons = reasons_by_symbol.setdefault(symbol, [])
                if reason not in reasons:
                    reasons.append(reason)
        return symbols[:30], reasons_by_symbol

    def _ranked_symbol_rows(self, universe: list[str], *, kind: str, limit: int) -> list[dict[str, Any]]:
        clickhouse_provider = getattr(self.provider, "clickhouse_provider", None)
        rank_symbols = getattr(clickhouse_provider, "rank_symbols", None)
        rows: list[dict[str, Any]] = []
        if callable(rank_symbols):
            try:
                rows = rank_symbols(universe, kind=kind, limit=limit)
            except Exception:
                rows = []
        if rows or kind != "dollar-volume":
            return [row for row in rows or [] if isinstance(row, dict)]

        hot_symbols = getattr(clickhouse_provider, "hot_symbols_by_dollar_volume", None)
        if not callable(hot_symbols):
            return []
        try:
            rows = hot_symbols(universe, limit=limit)
        except Exception:
            rows = []
        return [row for row in rows or [] if isinstance(row, dict)]

    def _recommended_news_symbols(self, user_sub: str, recommendation_repository: Any | None) -> tuple[list[str], dict[str, list[str]]]:
        if recommendation_repository is None:
            return [], {}
        latest_run = getattr(recommendation_repository, "latest_run", None)
        if not callable(latest_run):
            return [], {}
        try:
            run = latest_run(user_sub)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Recommendation read failed: {exc}") from exc
        items = run.get("items") if isinstance(run, dict) else []
        symbols: list[str] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            symbol = normalized_symbol_from_value(item.get("symbol"))
            if symbol and symbol not in symbols:
                symbols.append(symbol)
        return symbols, {symbol: ["추천"] for symbol in symbols}

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

    def _watchlist_news_rows(self, symbols: list[str], limit: int, locale: str) -> tuple[list[dict[str, Any]], str]:
        redis_provider = getattr(self.provider, "redis_provider", None)
        clickhouse_provider = getattr(self.provider, "clickhouse_provider", None)
        redis_rows = self._localized_news_rows_for_symbols_from_provider(redis_provider, symbols, limit, locale, use_days=False)
        if len(dedupe_news_rows(redis_rows, symbols)) >= limit:
            return redis_rows, "redis"

        clickhouse_rows = self._localized_news_rows_for_symbols_from_provider(clickhouse_provider, symbols, limit, locale, use_days=True)
        if clickhouse_rows:
            self._warm_localized_news_redis(redis_provider, clickhouse_rows, locale)
            return clickhouse_rows, "clickhouse"
        if redis_rows:
            return redis_rows, "redis"
        return [], "no-data"

    def _localized_news_rows_for_symbols_from_provider(
        self,
        provider,
        symbols: list[str],
        limit: int,
        locale: str,
        *,
        use_days: bool,
    ) -> list[dict[str, Any]]:
        if provider is None or not symbols:
            return []
        method = getattr(provider, "localized_news_articles_for_symbols", None)
        if callable(method):
            try:
                if use_days:
                    rows = method(symbols, limit=limit, days=30, locale=locale)
                else:
                    rows = method(symbols, limit=limit, locale=locale)
            except TypeError:
                rows = method(symbols, limit=limit)
            except Exception:
                rows = []
            return [row for row in rows or [] if isinstance(row, dict)]

        rows: list[dict[str, Any]] = []
        for symbol in symbols:
            rows.extend(self._localized_news_rows_from_provider(provider, symbol, limit, locale, use_days=use_days))
        return dedupe_news_rows(rows, symbols)[:limit]

    def _user_watchlist_symbols(self, user_sub: str) -> list[str]:
        redis_provider = getattr(self.provider, "redis_provider", None)
        redis_client = getattr(redis_provider, "redis", None)
        if redis_client is None:
            return []
        try:
            return RealtimeSubscriptionCohortService(redis_client, auto_reconcile=False).user_watchlist_symbols(user_sub)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Watch List read failed: {exc}") from exc

    def _watchlist_company_names(self, symbols: list[str]) -> dict[str, str]:
        names: dict[str, str] = {}
        method = getattr(self.provider, "symbol_detail", None)
        if not callable(method):
            return names
        for symbol in symbols:
            try:
                detail = method(symbol)
            except Exception:
                continue
            name = read_string(detail.get("name")) if isinstance(detail, dict) else None
            if name:
                names[symbol] = name
        return names

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
            provider_include = set(include_set)
            needs_volume_profile = "volumeProfile" in provider_include
            needs_order_flow_daily = "orderFlowDaily" in provider_include
            provider_include.discard("volumeProfile")
            provider_include.discard("orderFlowDaily")
            context = self.provider.agent_chart_context(symbol, interval, from_time, to_time, provider_include)
            if needs_volume_profile:
                context["volumeProfile"] = self.volume_profile_bins(symbol, from_time, to_time, "auto", interval=interval)
            if needs_order_flow_daily:
                context["orderFlowDaily"] = order_flow_daily_totals_only(self.order_flow_daily(
                    symbol,
                    _agent_context_date(from_time),
                    _agent_context_date(to_time),
                    limit_days=5,
                ))
            context["include"] = sorted(include_set)
            return context
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Market context provider failed: {exc}") from exc


def get_query_service() -> MarketDataQueryService:
    return MarketDataQueryService()


def _agent_context_date(value: str) -> str:
    return str(value)[:10]


def order_flow_daily_totals_only(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        **payload,
        "days": [
            {
                "sessionDate": day.get("sessionDate"),
                "totals": day.get("totals", {}),
            }
            for day in payload.get("days", [])
            if isinstance(day, dict)
        ],
    }


def build_chart_derived_client(provider: Any) -> ChartDerivedDataClient:
    clickhouse = getattr(provider, "clickhouse_provider", None)
    if clickhouse is None:
        clickhouse = clickhouse_client_from_env()
    return ChartDerivedDataClient(
        redis_client=redis_client_for_provider(provider),
        artifact_store=ChartDerivedArtifactStore(clickhouse),
    )


def parse_date_arg(value: str, name: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{name} must be YYYY-MM-DD.") from exc


def order_flow_day_payload(session_date: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    levels = [order_flow_level_payload(row) for row in rows]
    ask = sum(level["askVolume"] for level in levels)
    bid = sum(level["bidVolume"] for level in levels)
    unknown = sum(level["unknownVolume"] for level in levels)
    trade_count = sum(int(level.get("askTradeCount", 0) + level.get("bidTradeCount", 0) + level.get("unknownTradeCount", 0)) for level in levels)
    volume = ask + bid + unknown
    return {
        "sessionDate": session_date,
        "totals": {
            "askVolume": ask,
            "bidVolume": bid,
            "unknownVolume": unknown,
            "delta": ask - bid,
            "tradeCount": trade_count,
            "volume": volume,
        },
        "levels": levels,
    }


def order_flow_level_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "priceBin": float(row.get("priceBin", row.get("price_bin", 0)) or 0),
        "askVolume": float(row.get("askVolume", row.get("ask_volume", 0)) or 0),
        "bidVolume": float(row.get("bidVolume", row.get("bid_volume", 0)) or 0),
        "unknownVolume": float(row.get("unknownVolume", row.get("unknown_volume", 0)) or 0),
        "askTradeCount": int(row.get("askTradeCount", row.get("ask_trade_count", 0)) or 0),
        "bidTradeCount": int(row.get("bidTradeCount", row.get("bid_trade_count", 0)) or 0),
        "unknownTradeCount": int(row.get("unknownTradeCount", row.get("unknown_trade_count", 0)) or 0),
    }


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


def cached_inline_indicator_payload(derived_client: Any, request: dict[str, Any]) -> dict[str, Any] | None:
    redis_client = getattr(derived_client, "redis_client", None)
    cached = read_json_cache(redis_client, request["cacheKey"])
    if not cached:
        return None
    payload = dict(cached)
    payload["cache"] = {
        **(payload.get("cache") if isinstance(payload.get("cache"), dict) else {}),
        "hit": True,
        "ttlSeconds": redis_ttl_seconds(DERIVED_KIND_INDICATORS),
        "keyVersion": request["calculationVersion"],
    }
    return with_derived_metadata(
        payload,
        request,
        state="ready",
        source="redis",
        artifact_stored=bool(payload.get("derived", {}).get("artifactStored")),
    )


def write_inline_indicator_cache(derived_client: Any, request: dict[str, Any], payload: dict[str, Any]) -> None:
    if payload.get("dataStatus") != "ready" or not payload.get("returnedCandleCount"):
        return
    redis_client = getattr(derived_client, "redis_client", None)
    if redis_client is None:
        return
    cache_payload = dict(payload)
    cache_payload["cache"] = {
        **(cache_payload.get("cache") if isinstance(cache_payload.get("cache"), dict) else {}),
        "hit": False,
        "ttlSeconds": redis_ttl_seconds(DERIVED_KIND_INDICATORS),
        "keyVersion": request["calculationVersion"],
    }
    write_json_cache(redis_client, request["cacheKey"], cache_payload, redis_ttl_seconds(DERIVED_KIND_INDICATORS))


def inline_indicator_payload(
    request: dict[str, Any],
    candle_payload: dict[str, Any],
    specs,
    *,
    from_time: str | None,
    to_time: str | None,
    requested_limit: int,
    lookback: int,
) -> dict[str, Any]:
    candles = candle_payload.get("candles") or []
    computed = compute_indicator_payload(
        candles,
        specs,
        from_time=from_time,
        to_time=to_time,
    )
    payload = {
        "symbol": request["symbol"],
        "interval": request["interval"],
        "from": from_time,
        "to": to_time,
        "requestedLimit": requested_limit,
        "lookbackBars": lookback,
        "returnedCandleCount": len(candles),
        "source": candle_payload.get("source", "alpaca"),
        "feed": candle_payload.get("feed", "unknown"),
        "dataStatus": candle_payload.get("dataStatus", "ready" if candles else "empty"),
        "cache": {"hit": False, "ttlSeconds": redis_ttl_seconds(DERIVED_KIND_INDICATORS), "keyVersion": request["calculationVersion"]},
        **computed,
    }
    return with_derived_metadata(payload, request, state="ready", source="api-inline", artifact_stored=False)


def merge_candle_payloads(*payloads: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    by_timestamp: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        if not merged:
            merged = dict(payload)
        else:
            merged.update({key: value for key, value in payload.items() if key != "candles"})
        for candle in payload.get("candles") or []:
            if not isinstance(candle, dict):
                continue
            timestamp = str(candle.get("timestamp") or "")
            if timestamp:
                by_timestamp[timestamp] = candle
    merged["candles"] = [by_timestamp[key] for key in sorted(by_timestamp)]
    return merged


def previous_close_from_provider(provider: Any, symbol: str) -> float | None:
    try:
        payload = provider_candle_snapshot(provider, symbol, "1D", 5, ma_windows=())
    except Exception:
        return None
    candles = payload.get("candles") if isinstance(payload, dict) else None
    if not isinstance(candles, list):
        return None
    for candle in reversed(candles):
        if not isinstance(candle, dict):
            continue
        if candle.get("isClosed") is False:
            continue
        try:
            value = float(candle.get("close"))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def provider_accepts_ma_windows(provider: Any) -> bool:
    try:
        return "ma_windows" in inspect.signature(provider.candle_snapshot).parameters
    except (TypeError, ValueError):
        return False


def normalize_news_item(row: dict[str, Any], fallback_symbol: str) -> dict[str, Any]:
    title = read_string(row.get("localizedTitle")) or read_string(row.get("localizedHeadline")) or read_string(row.get("title")) or read_string(row.get("headline")) or "Untitled news"
    summary = read_string(row.get("localizedSummary")) or read_string(row.get("localized_summary")) or read_string(row.get("summary")) or ""
    symbols = read_string_list(row.get("symbols"))
    symbol = read_string(row.get("targetSymbol")) or read_string(row.get("target_symbol")) or read_string(row.get("symbol")) or fallback_symbol
    return {
        "articleId": read_string(row.get("articleId")) or read_string(row.get("article_id")),
        "symbol": symbol.upper(),
        "symbols": symbols or [symbol.upper()],
        "title": title,
        "summary": summary,
        "url": read_string(row.get("url")),
        "source": read_string(row.get("source")),
        "publishedAt": read_string(row.get("publishedAt")) or read_string(row.get("published_at")),
        "impactDirection": read_string(row.get("impactDirection")) or read_string(row.get("impact_direction")),
    }


def dedupe_news_rows(rows: list[dict[str, Any]], watchlist_symbols: list[str]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for row in sorted(rows or [], key=news_published_at, reverse=True):
        key = news_dedupe_key(row)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    watchlist = set(watchlist_symbols)
    return [row for row in deduped if watchlist_news_symbols(row) & watchlist]


def news_dedupe_key(row: dict[str, Any]) -> str:
    article_id = read_string(row.get("articleId")) or read_string(row.get("article_id"))
    if article_id:
        return f"id:{article_id}"
    url = read_string(row.get("url"))
    if url:
        return f"url:{url}"
    title = read_string(row.get("title")) or read_string(row.get("localizedTitle")) or read_string(row.get("localizedHeadline")) or read_string(row.get("headline")) or ""
    return f"text:{news_published_at(row)}:{title}"


def news_published_at(row: dict[str, Any]) -> str:
    return read_string(row.get("publishedAt")) or read_string(row.get("published_at")) or ""


def watchlist_news_symbols(row: dict[str, Any]) -> set[str]:
    values = set(read_string_list(row.get("symbols")))
    for key in ("targetSymbol", "target_symbol", "symbol"):
        value = read_string(row.get(key))
        if value:
            values.add(value.upper())
    return values


def watchlist_news_matches(
    row: dict[str, Any],
    watchlist_symbols: list[str],
    company_names: dict[str, str],
    reasons_by_symbol: dict[str, list[str]] | None = None,
) -> list[dict[str, str]]:
    row_symbols = watchlist_news_symbols(row)
    matches: list[dict[str, str]] = []
    for symbol in watchlist_symbols:
        if symbol not in row_symbols:
            continue
        reasons = (reasons_by_symbol or {}).get(symbol) or []
        match = {
            "symbol": symbol,
            **({"companyName": company_names[symbol]} if symbol in company_names else {}),
            **({"reason": "·".join(reasons)} if reasons else {}),
        }
        matches.append(match)
    return matches


def normalize_watchlist_news_mode(mode: str) -> str:
    normalized = str(mode or "watchlist").strip().lower().replace("_", "-")
    aliases = {
        "watchlist": "watchlist",
        "interest": "watchlist",
        "hot": "hot",
        "popular": "hot",
        "recommended": "recommended",
        "recommendation": "recommended",
    }
    if normalized not in aliases:
        raise HTTPException(status_code=400, detail=f"Unsupported watchlist news mode: {mode}")
    return aliases[normalized]


def normalized_symbol_from_value(value: Any) -> str | None:
    text = read_string(value)
    if not text:
        return None
    try:
        return normalize_market_symbol(text)
    except ValueError:
        return None


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
