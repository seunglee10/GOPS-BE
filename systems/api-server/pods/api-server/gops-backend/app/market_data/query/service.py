from __future__ import annotations

import os
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.market_data.backfill.service import get_backfill_service
from app.market_data.fill.service import get_on_demand_fill_service
from app.market_data.derived.service import DerivedCalculationService
from app.market_data.fundamentals.service import build_fundamentals_adapter
from app.market_data.heatmap.service import get_heatmap_service
from app.market_data.indices.related import build_related_indices_payload
from app.market_data.indices.service import get_indices_service
from app.market_data.query.canonical import CanonicalCandleQuery
from app.market_data.realtime.subscription_cohorts import RealtimeSubscriptionCohortService
from app.services.alfaka_market_data import get_market_data_provider, normalize_market_symbol, requested_ma_from_csv, sp500_universe_symbols
from alfaka.serving.chart_derived_data import (
    build_indicator_request,
    build_volume_profile_request,
    indicator_fetch_from_time,
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
    compute_volume_profile_payload,
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
    def __init__(self, provider=None, backfill_service=None, fill_service=None, redis_client=None, canonical_query=None, derived_service=None):
        self.provider = provider or get_market_data_provider()
        self.backfill_service = backfill_service or get_backfill_service(self.provider)
        self.fill_service = fill_service or get_on_demand_fill_service(self.provider)
        self.canonical_query = canonical_query or CanonicalCandleQuery(self.provider, self.fill_service)
        if redis_client is None:
            redis_client = redis_client_for_provider(self.provider)
        self.derived_service = derived_service or DerivedCalculationService(
            canonical_query=self.canonical_query,
            redis_client=redis_client,
        )

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
            payload = self.canonical_query.query(
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
        filter_candle_moving_average_fields(payload, requested_ma)
        payload["indicators"] = {"ma": requested_ma, "volume": True}
        metadata = self.backfill_service.snapshot_metadata(symbol, interval, payload)
        payload.update(metadata)
        if include_previous_close:
            payload["previousClose"] = previous_close_from_query(self.canonical_query, symbol)
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

    def chart_events(
        self,
        symbol: str,
        from_time: str,
        to_time: str,
        *,
        locale: str = "ko-KR",
        upcoming_days: int = 90,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        try:
            symbol = normalize_market_symbol(symbol)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        range_from = parse_chart_event_datetime(from_time, "from")
        range_to = parse_chart_event_datetime(to_time, "to")
        if range_to <= range_from:
            raise HTTPException(status_code=400, detail="to must be later than from")
        if not is_supported_news_locale(locale):
            raise HTTPException(status_code=400, detail=f"Invalid locale: {locale}")
        upcoming_days = max(1, min(int(upcoming_days), 365))
        reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        as_of = reference if now is not None else None
        clickhouse_provider = getattr(self.provider, "clickhouse_provider", None)

        news_rows = self._chart_news_rows(
            clickhouse_provider,
            symbol,
            range_from.astimezone(MARKET_TIMEZONE).date().isoformat(),
            range_to.astimezone(MARKET_TIMEZONE).date().isoformat(),
            locale,
            as_of,
        )
        news_days = chart_news_days_from_rows(symbol, news_rows)

        earnings_rows: list[dict[str, Any]] = []
        earnings_query_failed = False
        if symbol in set(sp500_universe_symbols(fallback_to_configured=False)):
            query_from = min(range_from, reference)
            query_to = max(range_to, reference + timedelta(days=upcoming_days))
            method = getattr(clickhouse_provider, "earnings_events", None)
            if callable(method):
                try:
                    earnings_rows = [
                        row for row in method(symbol, chart_event_iso(query_from), chart_event_iso(query_to)) or []
                        if isinstance(row, dict)
                    ]
                except Exception:
                    earnings_query_failed = True
            else:
                earnings_query_failed = True

        normalized_earnings = chart_earnings_from_rows(symbol, earnings_rows)
        if as_of is not None:
            normalized_earnings = [
                item for item in normalized_earnings
                if parse_chart_event_datetime(item["sourceAsOf"], "sourceAsOf") <= as_of
            ]
        visible_earnings = [
            item for item in normalized_earnings
            if range_from <= parse_chart_event_datetime(item["eventAt"], "eventAt") <= range_to
        ]
        upcoming = next((
            item for item in normalized_earnings
            if item["status"] == "scheduled"
            and reference <= parse_chart_event_datetime(item["eventAt"], "eventAt") <= reference + timedelta(days=upcoming_days)
        ), None)
        source_times = [
            parse_chart_event_datetime(item["sourceAsOf"], "sourceAsOf")
            for item in normalized_earnings
            if item.get("sourceAsOf")
        ]
        earnings_status = "empty"
        if earnings_query_failed:
            earnings_status = "stale"
        elif normalized_earnings:
            latest_source = max(source_times) if source_times else None
            earnings_status = "stale" if latest_source and reference - latest_source > timedelta(days=7) else "ready"

        return {
            "symbol": symbol,
            "from": chart_event_iso(range_from),
            "to": chart_event_iso(range_to),
            "status": {
                "earnings": earnings_status,
                "news": "ready" if news_days else "empty",
            },
            "earnings": visible_earnings,
            "newsDays": news_days,
            "upcomingEarnings": chart_upcoming_earnings(upcoming, reference) if upcoming else None,
        }

    def _chart_news_rows(
        self,
        clickhouse_provider: Any,
        symbol: str,
        from_date: str,
        to_date: str,
        locale: str,
        as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        method = getattr(clickhouse_provider, "company_daily_news_summaries_between", None)
        if not callable(method):
            return []
        try:
            query_options: dict[str, Any] = {"limit": 370, "locale": locale}
            if as_of is not None:
                query_options["as_of"] = chart_event_iso(as_of)
            rows = method(
                symbol,
                from_date,
                to_date,
                **query_options,
            )
        except Exception:
            return []
        normalized_rows = [row for row in rows or [] if isinstance(row, dict)]
        if as_of is None:
            return normalized_rows
        available_rows: list[dict[str, Any]] = []
        for row in normalized_rows:
            generated_at = read_string(row.get("generatedAt") or row.get("generated_at"))
            if not generated_at:
                continue
            try:
                if parse_chart_event_datetime(generated_at, "generatedAt") <= as_of:
                    available_rows.append(row)
            except HTTPException:
                continue
        return available_rows

    def indices(self, background_tasks=None) -> dict[str, Any]:
        try:
            return self.indices_snapshot(background_tasks=background_tasks)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Market indices provider failed: {exc}") from exc

    def indices_snapshot(self, background_tasks=None) -> dict[str, Any]:
        """Return the shared market-index cache payload without reshaping it."""
        return get_indices_service(self.provider).snapshot(background_tasks=background_tasks)

    def related_indices(self, symbol: str, background_tasks=None) -> dict[str, Any]:
        try:
            indices_payload = self.indices_snapshot(background_tasks=background_tasks)
            return build_related_indices_payload(
                symbol,
                indices_payload=indices_payload,
                provider=self.provider,
            )
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Related market indices failed: {exc}") from exc

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
        candle_count: int | None = None,
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
            candle_count=candle_count,
        )
        params = request.get("parameters") or {}

        def calculate():
            candle_payload = self.derived_service.query_candles(
                symbol,
                interval,
                resolve_candle_limit(interval, candle_count),
                from_time=from_time,
                to_time=to_time,
                ma_windows=(),
            )
            return compute_volume_profile_payload(
                candle_payload,
                symbol=symbol,
                interval=interval,
                from_time=from_time,
                to_time=to_time,
                target_bins=int(params.get("targetBins") or resolved_target_bins),
                price_min=params.get("priceMin"),
                price_max=params.get("priceMax"),
                binning_mode="exact",
                requested_candle_count=params.get("candleCount"),
            )

        return self.derived_service.resolve(request, calculate)

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
        lookback = indicator_required_lookback_bars(specs)
        fetch_limit = requested_limit + lookback

        def calculate():
            if from_time and lookback > 0:
                warmup_payload = self.derived_service.query_candles(
                    symbol,
                    interval,
                    lookback,
                    before=from_time,
                    ma_windows=(),
                )
                range_payload = self.derived_service.query_candles(
                    symbol,
                    interval,
                    requested_limit,
                    from_time=from_time,
                    to_time=to_time,
                    ma_windows=(),
                )
                candle_payload = merge_candle_payloads(warmup_payload, range_payload)
            else:
                fetch_from_time = indicator_fetch_from_time(interval, from_time, lookback)
                candle_payload = self.derived_service.query_candles(
                    symbol,
                    interval,
                    fetch_limit,
                    from_time=fetch_from_time,
                    to_time=to_time,
                    ma_windows=(),
                )
            return inline_indicator_payload(
                request,
                candle_payload,
                specs,
                from_time=from_time,
                to_time=to_time,
                requested_limit=requested_limit,
                lookback=lookback,
            )

        return self.derived_service.resolve(request, calculate)

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

    def latest_news(
        self,
        symbol: str,
        limit: int = 10,
        locale: str = "ko-KR",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        symbol = normalize_market_symbol(symbol)
        limit = max(1, min(int(limit), 30))
        rows, source = (
            self._latest_news_rows_as_of(symbol, limit, locale, now)
            if now is not None
            else self._latest_news_rows(symbol, limit, locale)
        )
        payload = {
            "symbol": symbol,
            "source": source,
            "items": [normalize_news_item(row, symbol) for row in rows],
        }
        if now is not None:
            payload["asOf"] = chart_event_iso(now)
        return payload

    def daily_news(
        self,
        symbol: str,
        limit: int = 5,
        locale: str = "ko-KR",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        symbol = normalize_market_symbol(symbol)
        limit = max(1, min(int(limit), 30))
        rows = (
            self._daily_news_rows_as_of(symbol, limit, locale, now)
            if now is not None
            else self._daily_news_rows(symbol, limit, locale)
        )
        summaries = [clickhouse_row_to_daily_summary(row) for row in rows]
        if now is None:
            summaries = attach_price_changes_to_daily_summaries(summaries, self._daily_news_price_candles(symbol, limit))
        payload = {
            "symbol": symbol,
            "displayMode": "dailySummary",
            "dailySummaries": summaries,
        }
        if now is not None:
            payload["asOf"] = chart_event_iso(now)
        return payload

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

    def _latest_news_rows_as_of(
        self,
        symbol: str,
        limit: int,
        locale: str,
        as_of: datetime,
    ) -> tuple[list[dict[str, Any]], str]:
        clickhouse_provider = getattr(self.provider, "clickhouse_provider", None)
        method = getattr(clickhouse_provider, "localized_news_articles_for_symbols_as_of", None)
        if not callable(method):
            return [], "no-data"
        try:
            rows = method(
                [symbol],
                as_of=chart_event_iso(as_of),
                limit=limit,
                days=30,
                locale=locale,
            )
        except Exception:
            rows = []
        normalized = [row for row in rows or [] if isinstance(row, dict)]
        return normalized[:limit], "clickhouse-simulation" if normalized else "no-data"

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

    def _daily_news_rows_as_of(
        self,
        symbol: str,
        limit: int,
        locale: str,
        as_of: datetime,
    ) -> list[dict[str, Any]]:
        clickhouse_provider = getattr(self.provider, "clickhouse_provider", None)
        method = getattr(clickhouse_provider, "company_daily_news_summaries_between", None)
        if not callable(method):
            return []
        reference = as_of.astimezone(timezone.utc)
        to_date = reference.date()
        from_date = to_date - timedelta(days=30)
        try:
            rows = method(
                symbol,
                from_date.isoformat(),
                to_date.isoformat(),
                limit=max(30, limit),
                locale=locale,
                as_of=chart_event_iso(reference),
            )
        except Exception:
            rows = []
        normalized = [row for row in rows or [] if isinstance(row, dict)]
        normalized.sort(key=lambda row: str(row.get("date") or ""), reverse=True)
        return normalized[:limit]

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
            needs_volume_profile = "volumeProfile" in include_set
            needs_order_flow_daily = "orderFlowDaily" in include_set
            visible = self.canonical_query.query(
                symbol,
                interval,
                500,
                from_time=from_time,
                to_time=to_time,
                ma_windows=(),
            )
            daily = self.canonical_query.query(symbol, "1D", 2, ma_windows=())
            daily_candles = daily.get("candles") or []
            context = {
                "symbol": symbol,
                "interval": interval,
                "visibleRange": {"from": from_time, "to": to_time},
                "candles": visible.get("candles") or [],
                "latestDailyCandle": daily_candles[-1] if daily_candles else None,
                "previousDailyCandle": daily_candles[-2] if len(daily_candles) > 1 else None,
                "marketStatus": self.provider.latest_status(symbol) if "status" in include_set else None,
                "volumeProfile": None,
                "comparisonCandidates": self.provider.search_symbols(symbol[:2], 5),
            }
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
        **computed,
    }
    return payload


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


def previous_close_from_query(canonical_query: CanonicalCandleQuery, symbol: str) -> float | None:
    try:
        payload = canonical_query.query(symbol, "1D", 5, ma_windows=())
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


def parse_chart_event_datetime(value: Any, field: str) -> datetime:
    text = read_string(value)
    if not text:
        raise HTTPException(status_code=400, detail=f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def chart_event_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def is_supported_news_locale(locale: str) -> bool:
    parts = str(locale or "").split("-")
    return len(parts) in {1, 2} and len(parts[0]) == 2 and parts[0].islower() and (
        len(parts) == 1 or (len(parts[1]) == 2 and parts[1].isupper())
    )


def chart_news_days_from_rows(symbol: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        summary = clickhouse_row_to_daily_summary(row)
        day = str(summary.get("date") or "")[:10]
        if not day:
            continue
        current = grouped.get(day)
        article_ids = {
            str(item) for item in summary.get("articleIds") or [] if str(item).strip()
        }
        sources = dedupe_chart_news_sources(summary.get("sources") or [])
        item = {
            "id": f"news:{symbol}:{day}",
            "type": "news",
            "date": day,
            "articleCount": max(int(summary.get("articleCount") or 0), len(article_ids), len(sources)),
            "summary": str(summary.get("summary") or ""),
            "keyPoints": [str(point) for point in summary.get("keyPoints") or [] if str(point).strip()][:6],
            "impactDirection": normalize_chart_news_direction(summary.get("impactDirection")),
            "sentiment": str(summary.get("sentiment") or "neutral"),
            "sources": sources,
            "_articleIds": article_ids,
            "_generatedAt": str(summary.get("generatedAt") or ""),
        }
        if current is None:
            grouped[day] = item
            continue
        merged_ids = set(current.get("_articleIds") or set()) | article_ids
        merged_sources = dedupe_chart_news_sources([*(current.get("sources") or []), *sources])
        if item["_generatedAt"] >= current.get("_generatedAt", ""):
            current.update({
                "summary": item["summary"],
                "keyPoints": item["keyPoints"],
                "impactDirection": item["impactDirection"],
                "sentiment": item["sentiment"],
                "_generatedAt": item["_generatedAt"],
            })
        current["_articleIds"] = merged_ids
        current["sources"] = merged_sources
        current["articleCount"] = max(current["articleCount"], item["articleCount"], len(merged_ids), len(merged_sources))
    return [
        {key: value for key, value in grouped[day].items() if not key.startswith("_")}
        for day in sorted(grouped)
    ]


def dedupe_chart_news_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            continue
        title = read_string(source.get("title"))
        url = read_string(source.get("url"))
        if not title or not url:
            continue
        article_id = read_string(source.get("articleId")) or read_string(source.get("article_id"))
        key = article_id or url
        if key in seen:
            continue
        seen.add(key)
        result.append({
            **({"articleId": article_id} if article_id else {}),
            "title": title,
            **({"name": name} if (name := read_string(source.get("name") or source.get("source"))) else {}),
            "url": url,
            **({"publishedAt": published_at} if (published_at := read_string(source.get("publishedAt") or source.get("published_at"))) else {}),
        })
        if len(result) >= 3:
            break
    return result


def normalize_chart_news_direction(value: Any) -> str:
    normalized = str(value or "neutral").strip().lower()
    return normalized if normalized in {"positive", "negative", "mixed", "neutral"} else "neutral"


def chart_earnings_from_rows(symbol: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        event_text = read_string(row.get("eventAt") or row.get("event_at"))
        if not event_text:
            continue
        try:
            event_at = chart_event_iso(parse_chart_event_datetime(event_text, "eventAt"))
        except HTTPException:
            continue
        actual = chart_float(row.get("actualValue") if "actualValue" in row else row.get("actual_value"))
        estimate = chart_float(row.get("estimate") if "estimate" in row else row.get("average"))
        surprise = actual - estimate if actual is not None and estimate is not None else None
        surprise_percent = chart_float(row.get("surprisePercent") if "surprisePercent" in row else row.get("surprise_percent"))
        if surprise_percent is None and surprise is not None and estimate not in {None, 0}:
            surprise_percent = surprise / abs(estimate) * 100
        status = str(row.get("eventStatus") or row.get("event_status") or "").strip().lower()
        status = "reported" if actual is not None or status == "reported" else "scheduled"
        session = str(row.get("eventSession") or row.get("event_session") or "unknown").strip().lower()
        if session not in {"pre", "after", "regular", "unknown"}:
            session = "unknown"
        source_as_of_text = read_string(row.get("sourceAsOf") or row.get("source_as_of") or row.get("collectedAt")) or event_at
        try:
            source_as_of = chart_event_iso(parse_chart_event_datetime(source_as_of_text, "sourceAsOf"))
        except HTTPException:
            source_as_of = event_at
        item = {
            "id": f"earnings:{symbol}:{event_at}",
            "type": "earnings",
            "eventAt": event_at,
            "status": status,
            "session": session,
            "eps": {
                "actual": actual,
                "estimate": estimate,
                "surprise": surprise,
                "surprisePercent": surprise_percent,
            },
            "source": str(row.get("source") or "yahoo-finance"),
            "sourceAsOf": source_as_of,
        }
        current = deduped.get(event_at)
        if current is None or item["sourceAsOf"] >= current["sourceAsOf"]:
            deduped[event_at] = item
    return [deduped[key] for key in sorted(deduped)]


def chart_upcoming_earnings(item: dict[str, Any], reference: datetime) -> dict[str, Any]:
    event_at = parse_chart_event_datetime(item["eventAt"], "eventAt")
    days_remaining = max(0, (event_at.astimezone(MARKET_TIMEZONE).date() - reference.astimezone(MARKET_TIMEZONE).date()).days)
    return {
        "eventAt": item["eventAt"],
        "session": item["session"],
        "estimate": item["eps"]["estimate"],
        "daysRemaining": days_remaining,
        "sourceAsOf": item["sourceAsOf"],
    }


def chart_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in {float("inf"), float("-inf")} else None
