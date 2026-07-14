from __future__ import annotations

import json
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo


MARKET_TIMEZONE = ZoneInfo("America/New_York")

SECTOR_MARKETS: tuple[tuple[str, str, str], ...] = (
    ("반도체", "SOXX", "미국 반도체"),
    ("기술", "XLK", "미국 기술주"),
    ("헬스케어", "XLV", "미국 헬스케어"),
    ("필수소비재", "XLP", "미국 필수소비재"),
    ("에너지", "XLE", "미국 에너지"),
    ("금융", "XLF", "미국 금융"),
    ("산업재", "XLI", "미국 산업재"),
    ("유틸리티", "XLU", "미국 유틸리티"),
    ("소재", "XLB", "미국 소재"),
    ("부동산", "XLRE", "미국 부동산"),
    ("커뮤니케이션", "XLC", "미국 커뮤니케이션"),
    ("임의소비재", "XLY", "미국 임의소비재"),
)


class CoachPointInTimeContextProvider(Protocol):
    """Loads external coach evidence once for one immutable input snapshot."""

    def load(
        self,
        *,
        fills: list[dict[str, Any]],
        requested_at: datetime,
        portfolio_before_by_fill_id: dict[str, dict[str, Any]] | None = None,
        portfolio_after_by_fill_id: dict[str, dict[str, Any]] | None = None,
        portfolio_current: dict[str, Any] | None = None,
        current_fill_ids: set[str] | None = None,
    ) -> dict[str, Any]: ...


class StoreCoachPointInTimeContextProvider:
    """Point-in-time facade over the stores already available to the worker.

    The provider deliberately does not call SEC, Yahoo, Alpaca, or any other
    external API. Every returned item carries the timestamp that made it
    eligible for the snapshot cutoff. GraphDB is the sole exception: the
    existing graph is current-only, so it is explicitly excluded from
    historical similarity and never receives an invented source timestamp.
    """

    def __init__(
        self,
        *,
        clickhouse_provider: Any | None = None,
        redis_market_provider: Any | None = None,
        ontology_provider: Any | None = None,
        heatmap_seed_path: str | Path | None = None,
        now_provider=None,
    ) -> None:
        # Kept as a no-op keyword for callers created before the coach snapshot
        # became archive-first.  A post-market review must never read a mutable
        # Redis quote as analytical evidence.
        _ = redis_market_provider
        self._clickhouse = clickhouse_provider
        self._ontology = ontology_provider
        self._heatmap_seed_path = Path(heatmap_seed_path).expanduser() if heatmap_seed_path else None
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def load(
        self,
        *,
        fills: list[dict[str, Any]],
        requested_at: datetime,
        portfolio_before_by_fill_id: dict[str, dict[str, Any]] | None = None,
        portfolio_after_by_fill_id: dict[str, dict[str, Any]] | None = None,
        portfolio_current: dict[str, Any] | None = None,
        current_fill_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        cutoff = _utc(requested_at) or datetime.now(timezone.utc)
        self._clickhouse_failed = False
        normalized_fills = _normalize_fills(fills, cutoff)
        before = portfolio_before_by_fill_id or {}
        after = portfolio_after_by_fill_id or {}
        all_fill_ids = {row["fillId"] for row in normalized_fills}
        current_ids = {str(value) for value in current_fill_ids} if current_fill_ids is not None else all_fill_ids
        symbols = sorted({row["symbol"] for row in normalized_fills})
        current_symbols = sorted({row["symbol"] for row in normalized_fills if row["fillId"] in current_ids})
        missing: list[dict[str, Any]] = []

        quotes = self._current_quotes(current_symbols, cutoff, missing)
        metadata_rows = self._metadata_rows(symbols, cutoff, missing)
        heatmap = self._heatmap_metadata(cutoff, missing)

        fill_enrichment: dict[str, dict[str, Any]] = {}
        metadata_by_symbol: dict[str, dict[str, Any]] = {}
        metadata_rank_by_symbol: dict[str, tuple[int, datetime, datetime]] = {}
        for fill in normalized_fills:
            fill_id = fill["fillId"]
            symbol = fill["symbol"]
            fill_cutoff = fill["decisionCutoff"]
            portfolio_metadata = _portfolio_metadata(
                symbol,
                fill_cutoff,
                before.get(fill_id),
                after.get(fill_id),
            )
            stored_metadata = _metadata_at(metadata_rows.get(symbol, []), fill_cutoff)
            seed_metadata = _metadata_at(heatmap.get(symbol, []), fill_cutoff)
            company_name = (
                portfolio_metadata.get("companyName")
                or stored_metadata.get("companyName")
                or seed_metadata.get("companyName")
                or symbol
            )
            sector_metadata = portfolio_metadata if portfolio_metadata.get("sector") else seed_metadata
            item: dict[str, Any] = {
                "companyName": company_name,
                "sector": sector_metadata.get("sector"),
                "industry": sector_metadata.get("industry"),
                "metadataSource": sector_metadata.get("source") or stored_metadata.get("source"),
                "metadataSourceAsOf": sector_metadata.get("sourceAsOf") or stored_metadata.get("sourceAsOf"),
            }
            if fill_id in current_ids:
                quote = quotes.get(symbol)
                quote_at = _utc(quote.get("sourceAsOf")) if quote else None
                quote_is_post_entry = quote_at is not None and quote_at >= fill["entryCutoff"]
                item["currentPrice"] = quote.get("price") if quote and quote_is_post_entry else None
                item["currentPriceSource"] = quote.get("source") if quote and quote_is_post_entry else None
                item["currentPriceAsOf"] = quote.get("sourceAsOf") if quote and quote_is_post_entry else None
            fill_enrichment[fill_id] = item
            metadata_at = _utc(item.get("metadataSourceAsOf")) or datetime.min.replace(tzinfo=timezone.utc)
            candidate_rank = (1 if fill_id in current_ids else 0, metadata_at, fill_cutoff)
            if candidate_rank > metadata_rank_by_symbol.get(
                symbol,
                (-1, datetime.min.replace(tzinfo=timezone.utc), datetime.min.replace(tzinfo=timezone.utc)),
            ):
                metadata_rank_by_symbol[symbol] = candidate_rank
                metadata_by_symbol[symbol] = {
                    "symbol": symbol,
                    "companyName": company_name,
                    "sector": item.get("sector"),
                    "industry": item.get("industry"),
                    "source": item.get("metadataSource"),
                    "sourceAsOf": item.get("metadataSourceAsOf"),
                }

        if current_ids and any(
            not fill_enrichment.get(fill_id, {}).get("currentPrice")
            for fill_id in current_ids
        ):
            missing.append(_missing("market", "current_quote_missing", "일부 종목의 체결 이후·요청 cutoff 이전 현재가를 찾지 못했습니다."))
        if symbols and any(not fill_enrichment[row["fillId"]].get("sector") for row in normalized_fills):
            missing.append(_missing("metadata", "sector_metadata_missing", "일부 거래 시점의 섹터 분류를 확인할 수 없습니다."))

        news_context, news_as_of = self._news_context(normalized_fills, symbols, cutoff, missing)
        fundamentals_context, fundamentals_as_of = self._fundamentals_context(normalized_fills, symbols, cutoff, missing)
        earnings_context, earnings_as_of = self._earnings_context(current_symbols, cutoff, missing)
        ontology_context, ontology_as_of = self._ontology_context(current_symbols, cutoff, missing)
        portfolio_diversification = self._portfolio_diversification_context(
            portfolio_current if isinstance(portfolio_current, dict) else {},
            metadata_by_symbol,
            cutoff,
        )
        market_as_of = _max_iso([row.get("sourceAsOf") for row in quotes.values()])

        return {
            "fillEnrichmentById": fill_enrichment,
            "marketContext": {
                "quotesBySymbol": quotes,
                "metadataBySymbol": metadata_by_symbol,
                "portfolioDiversification": portfolio_diversification,
            },
            "newsContext": news_context,
            "fundamentalsContext": fundamentals_context,
            "earningsContext": earnings_context,
            "ontologyContext": ontology_context,
            "sourceAsOf": {
                "market": market_as_of,
                "news": news_as_of,
                "fundamentals": fundamentals_as_of,
                "earnings": earnings_as_of,
                "ontology": ontology_as_of,
            },
            "missingData": _dedupe_missing(missing),
        }

    def _current_quotes(
        self,
        symbols: list[str],
        cutoff: datetime,
        missing: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        if not symbols:
            return {}
        result: dict[str, dict[str, Any]] = {}
        rows = self._query(
            f"""
            SELECT
              symbol,
              argMax(price, tuple(event_time, inserted_at, ifNull(source_event_id, ''))) AS price,
              argMax(event_time, tuple(event_time, inserted_at, ifNull(source_event_id, ''))) AS sourceAsOf
            FROM {self._table('trade_ticks')}
            WHERE symbol IN {{symbols:Array(String)}}
              AND event_time <= parseDateTime64BestEffort({{requestedAt:String}})
              AND coalesce(received_at, event_time) <= parseDateTime64BestEffort({{requestedAt:String}})
              AND inserted_at <= parseDateTime64BestEffort({{requestedAt:String}})
            GROUP BY symbol
            FORMAT JSONEachRow
            """,
            {"symbols": symbols, "requestedAt": _iso(cutoff)},
            source="market",
            code="clickhouse_quote_unavailable",
            message="ClickHouse 체결 현재가 조회 실패",
            missing=missing,
        )
        for row in rows:
            quote = _stored_quote(row, cutoff, "clickhouse.trade_ticks")
            if quote:
                result[quote["symbol"]] = quote

        remaining = [symbol for symbol in symbols if symbol not in result]
        if not remaining:
            return result
        rows = self._query(
            f"""
            SELECT
              symbol,
              argMax(close, tuple(event_time, inserted_at, ifNull(source_event_id, ''))) AS price,
              argMax(event_time, tuple(event_time, inserted_at, ifNull(source_event_id, ''))) AS sourceAsOf
            FROM {self._table('chart_candles')}
            WHERE symbol IN {{symbols:Array(String)}}
              AND interval = '1m'
              AND is_closed = 1
              AND event_time <= parseDateTime64BestEffort({{requestedAt:String}})
              AND inserted_at <= parseDateTime64BestEffort({{requestedAt:String}})
            GROUP BY symbol
            FORMAT JSONEachRow
            """,
            {"symbols": remaining, "requestedAt": _iso(cutoff)},
            source="market",
            code="clickhouse_candle_quote_unavailable",
            message="ClickHouse 1분봉 현재가 조회 실패",
            missing=missing,
        )
        for row in rows:
            quote = _stored_quote(row, cutoff, "clickhouse.chart_candles.1m")
            if quote:
                result[quote["symbol"]] = quote
        return result

    def _metadata_rows(
        self,
        symbols: list[str],
        cutoff: datetime,
        missing: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        if not symbols:
            return {}
        params = {"symbols": symbols, "requestedAt": _iso(cutoff)}
        symbol_rows = self._query(
            f"""
            SELECT symbol, name AS companyName, exchange, source, updated_at AS sourceAsOf
            FROM {self._table('symbols')}
            WHERE symbol IN {{symbols:Array(String)}}
              AND updated_at <= parseDateTime64BestEffort({{requestedAt:String}})
              AND inserted_at <= parseDateTime64BestEffort({{requestedAt:String}})
            ORDER BY symbol, updated_at DESC
            LIMIT 10 BY symbol
            FORMAT JSONEachRow
            """,
            params,
            source="metadata",
            code="symbol_metadata_unavailable",
            message="종목 메타데이터 조회 실패",
            missing=missing,
        )
        sec_rows = self._query(
            f"""
            SELECT symbol, company_name AS companyName, exchange, 'sec_company_tickers' AS source,
                   updated_at AS sourceAsOf
            FROM {self._table('sec_company_tickers')}
            WHERE symbol IN {{symbols:Array(String)}}
              AND updated_at <= parseDateTime64BestEffort({{requestedAt:String}})
              AND inserted_at <= parseDateTime64BestEffort({{requestedAt:String}})
            ORDER BY symbol, updated_at DESC
            LIMIT 10 BY symbol
            FORMAT JSONEachRow
            """,
            params,
            source="metadata",
            code="sec_company_metadata_unavailable",
            message="SEC 회사명 메타데이터 조회 실패",
            missing=missing,
        )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in [*symbol_rows, *sec_rows]:
            symbol = _symbol(row.get("symbol"))
            timestamp = _utc(row.get("sourceAsOf"))
            if not symbol or timestamp is None or timestamp > cutoff:
                continue
            grouped.setdefault(symbol, []).append({
                "companyName": row.get("companyName"),
                "exchange": row.get("exchange"),
                "source": row.get("source") or "clickhouse.symbols",
                "sourceAsOf": _iso(timestamp),
            })
        return grouped

    def _heatmap_metadata(
        self,
        cutoff: datetime,
        missing: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        path = self._resolve_heatmap_path()
        if path is None:
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            missing.append(_unavailable("metadata", "heatmap_seed_invalid", "heatmap 메타데이터 로드 실패", exc))
            return {}
        source_at = _utc(payload.get("sourceRetrievedAt")) if isinstance(payload, dict) else None
        if source_at is None or source_at > cutoff:
            return {}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in payload.get("items", []) if isinstance(payload, dict) else []:
            if not isinstance(row, dict) or not (symbol := _symbol(row.get("symbol"))):
                continue
            grouped[symbol] = [{
                "companyName": row.get("companyName"),
                "sector": row.get("sector"),
                "industry": row.get("industry"),
                "source": "sp500-heatmap-seed",
                "sourceAsOf": _iso(source_at),
            }]
        return grouped

    def _portfolio_diversification_context(
        self,
        portfolio: dict[str, Any],
        metadata_by_symbol: dict[str, dict[str, Any]],
        cutoff: datetime,
    ) -> dict[str, Any]:
        """Calculate current diversification evidence from stored daily candles only."""
        positions_raw = portfolio.get("positions")
        positions = list(positions_raw.values()) if isinstance(positions_raw, dict) else positions_raw if isinstance(positions_raw, list) else []
        account = portfolio.get("account") if isinstance(portfolio.get("account"), dict) else {}
        valued: list[tuple[dict[str, Any], float, str]] = []
        for position in positions:
            if not isinstance(position, dict):
                continue
            symbol = _symbol(position.get("symbol"))
            value = next((_number(position.get(key)) for key in ("marketValueForeign", "marketValueKrw", "marketValue", "value", "costBasisValue") if _number(position.get(key)) is not None), None)
            sector = str(position.get("sector") or metadata_by_symbol.get(symbol, {}).get("sector") or "").strip()
            if symbol and value is not None and value >= 0 and sector:
                valued.append((position, value, sector))
        equity = next((_number(account.get(key)) for key in ("totalValueForeign", "totalValueKrw", "equity", "totalEquity", "netAsset") if _number(account.get(key)) is not None), None)
        if equity is None and valued:
            equity = sum(value for _, value, _ in valued)
        if not valued or equity is None or equity <= 0:
            return {"sourceAsOf": None, "holdingSensitivities": [], "candidates": []}

        sector_values: dict[str, float] = {}
        held_symbols: list[str] = []
        for position, value, sector in valued:
            sector_values[sector] = sector_values.get(sector, 0.0) + value
            held_symbols.append(_symbol(position.get("symbol")))
        concentrated_sector, concentrated_value = max(sector_values.items(), key=lambda item: (item[1], item[0]))
        if concentrated_value / equity * 100 < 35:
            return {"sourceAsOf": None, "holdingSensitivities": [], "candidates": []}
        concentrated_market = _sector_market(concentrated_sector)
        if concentrated_market is None:
            return {"sourceAsOf": None, "holdingSensitivities": [], "candidates": []}

        requested_symbols = sorted({*held_symbols, "SPY", concentrated_market[1], *(etf for _, etf, _ in SECTOR_MARKETS)})
        closes = self._portfolio_daily_closes(requested_symbols, cutoff)
        market_returns = _daily_returns(closes.get("SPY", []))
        concentrated_returns = _daily_returns(closes.get(concentrated_market[1], []))
        if len(market_returns) < 10 or len(concentrated_returns) < 10:
            return {"sourceAsOf": _latest_close_time(closes), "holdingSensitivities": [], "candidates": []}

        holding_sensitivities: list[dict[str, Any]] = []
        for position, _, sector in valued:
            symbol = _symbol(position.get("symbol"))
            holding_returns = _daily_returns(closes.get(symbol, []))
            sector_market = _sector_market(sector)
            sector_returns = _daily_returns(closes.get(sector_market[1], [])) if sector_market else {}
            holding_sensitivities.append({
                "symbol": symbol,
                "marketCorrelation": _correlation(holding_returns, market_returns),
                "sectorCorrelation": _correlation(holding_returns, sector_returns),
            })

        candidates: list[dict[str, Any]] = []
        concentrated_key = _sector_key(concentrated_sector)
        relative_base = _period_return(closes.get("SPY", []), 20)
        for sector, etf, market in SECTOR_MARKETS:
            if _sector_key(sector) == concentrated_key or any(_sector_key(held) == _sector_key(sector) for held in sector_values):
                continue
            candidate_closes = closes.get(etf, [])
            correlation = _correlation(_daily_returns(candidate_closes), concentrated_returns)
            relative = _period_return(candidate_closes, 20)
            if correlation is None or relative is None or relative_base is None or correlation > 0.65:
                continue
            relative_strength = round(relative - relative_base, 4)
            market_correlation = _correlation(_daily_returns(candidate_closes), market_returns)
            role = "defensive" if market_correlation is not None and market_correlation < 0.65 else "relative_strength" if relative_strength > 0 else "diversification"
            candidates.append({
                "id": f"{etf.lower()}-diversification", "market": market, "sector": sector, "etfSymbol": etf,
                "correlationToConcentratedSector": correlation, "relativeStrengthPercent": relative_strength,
                "role": role, "reason": "저장된 일봉의 집중 섹터 상관도와 시장 대비 상대 강도를 기준으로 계산했습니다.",
                "sourceAsOf": _latest_close_time({etf: candidate_closes, concentrated_market[1]: closes.get(concentrated_market[1], [])}),
            })
        candidates.sort(key=lambda item: (float(item["correlationToConcentratedSector"]), -float(item["relativeStrengthPercent"]), str(item["market"])))
        return {"sourceAsOf": _latest_close_time(closes), "holdingSensitivities": holding_sensitivities, "candidates": candidates[:3]}

    def _portfolio_daily_closes(self, symbols: list[str], cutoff: datetime) -> dict[str, list[tuple[datetime, float]]]:
        if not symbols:
            return {}
        rows = self._query(
            f"""
            SELECT symbol, event_time, argMax(close, tuple(inserted_at, ifNull(source_event_id, ''))) AS close
            FROM {self._table('chart_candles')}
            WHERE symbol IN {{symbols:Array(String)}}
              AND interval IN ('1D', '1d')
              AND is_closed = 1
              AND event_time >= parseDateTime64BestEffort({{fromAt:String}})
              AND event_time <= parseDateTime64BestEffort({{requestedAt:String}})
              AND inserted_at <= parseDateTime64BestEffort({{requestedAt:String}})
            GROUP BY symbol, event_time
            ORDER BY symbol, event_time ASC
            FORMAT JSONEachRow
            """,
            {"symbols": symbols, "fromAt": _iso(cutoff - timedelta(days=100)), "requestedAt": _iso(cutoff)},
            source="market", code="portfolio_diversification_market_unavailable",
            message="포트폴리오 시장 상관 데이터 조회 실패", missing=[],
        )
        result: dict[str, list[tuple[datetime, float]]] = {}
        for row in rows:
            symbol = _symbol(row.get("symbol")); event_at = _utc(row.get("event_time")); close = _number(row.get("close"))
            if symbol and event_at is not None and close is not None and close > 0 and event_at <= cutoff:
                result.setdefault(symbol, []).append((event_at, close))
        return result

    def _news_context(
        self,
        fills: list[dict[str, Any]],
        symbols: list[str],
        cutoff: datetime,
        missing: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], str | None]:
        if not fills:
            return {"byFillId": {}}, None
        oldest = min(row["decisionCutoff"] for row in fills)
        lookback_days = max(1, int(os.getenv("COACH_NEWS_LOOKBACK_DAYS", "30")))
        oldest_news = oldest.replace(microsecond=0) - timedelta(days=lookback_days)
        per_symbol_limit = min(500, max(50, len(fills) * 8))
        rows = self._query(
            f"""
            SELECT
              symbol, article_id AS articleId, headline, summary, url, source,
              published_at AS publishedAt, received_at AS receivedAt, inserted_at AS insertedAt,
              greatest(published_at, coalesce(received_at, published_at), inserted_at) AS availableAt
            FROM {self._table('news_articles')}
            WHERE symbol IN {{symbols:Array(String)}}
              AND published_at >= parseDateTime64BestEffort({{oldestAt:String}})
              AND published_at <= parseDateTime64BestEffort({{requestedAt:String}})
              AND greatest(published_at, coalesce(received_at, published_at), inserted_at)
                    <= parseDateTime64BestEffort({{requestedAt:String}})
            ORDER BY symbol, availableAt DESC
            LIMIT {{perSymbolLimit:UInt32}} BY symbol
            FORMAT JSONEachRow
            """,
            {
                "symbols": symbols,
                "oldestAt": _iso(oldest_news),
                "requestedAt": _iso(cutoff),
                "perSymbolLimit": per_symbol_limit,
            },
            source="news",
            code="news_provider_unavailable",
            message="뉴스 point-in-time 조회 실패",
            missing=missing,
        )
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            symbol = _symbol(row.get("symbol"))
            published = _utc(row.get("publishedAt"))
            available = _utc(row.get("availableAt"))
            if not symbol or published is None or available is None or available > cutoff:
                continue
            by_symbol.setdefault(symbol, []).append({
                "articleId": row.get("articleId"),
                "headline": row.get("headline"),
                "summary": row.get("summary"),
                "url": row.get("url"),
                "source": row.get("source") or "clickhouse.news_articles",
                "eventTime": _iso(published),
                "availableAt": _iso(available),
                "sourceAsOf": _iso(available),
            })
        by_fill: dict[str, Any] = {}
        accepted_times: list[str | None] = []
        max_items = max(1, int(os.getenv("COACH_NEWS_ITEMS_PER_FILL", "5")))
        for fill in fills:
            fill_news_start = fill["decisionCutoff"] - timedelta(days=lookback_days)
            eligible = [
                item for item in by_symbol.get(fill["symbol"], [])
                if (_utc(item.get("availableAt")) or datetime.max.replace(tzinfo=timezone.utc)) <= fill["decisionCutoff"]
                and (_utc(item.get("eventTime")) or datetime.min.replace(tzinfo=timezone.utc)) >= fill_news_start
            ]
            eligible.sort(key=lambda item: str(item.get("availableAt") or ""), reverse=True)
            items = eligible[:max_items]
            by_fill[fill["fillId"]] = {
                "symbol": fill["symbol"],
                "asOf": _iso(fill["decisionCutoff"]),
                "items": items,
            }
            accepted_times.extend(item.get("sourceAsOf") for item in items)
        if fills and not any(row["items"] for row in by_fill.values()):
            missing.append(_missing("news", "point_in_time_news_missing", "거래 시점 이전 뉴스 증거가 없습니다."))
        return {"byFillId": by_fill}, _max_iso(accepted_times)

    def _fundamentals_context(
        self,
        fills: list[dict[str, Any]],
        symbols: list[str],
        cutoff: datetime,
        missing: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], str | None]:
        if not fills:
            return {"byFillId": {}, "availabilityPrecision": "date"}, None
        params = {"symbols": symbols, "requestedAt": _iso(cutoff), "limit": 250}
        facts = self._query(
            f"""
            SELECT symbol, metric, value, unit, fiscal_year AS fiscalYear,
                   fiscal_period AS fiscalPeriod, period_end AS periodEnd,
                   filed_at AS filedAt, version_filed_at AS versionFiledAt,
                   inserted_at AS insertedAt, quality, 'fact' AS rowType
            FROM {self._table('sec_financial_facts')}
            WHERE symbol IN {{symbols:Array(String)}}
              AND filed_at <= toDate(parseDateTime64BestEffort({{requestedAt:String}}))
              AND version_filed_at <= toDate(parseDateTime64BestEffort({{requestedAt:String}}))
              AND inserted_at <= parseDateTime64BestEffort({{requestedAt:String}})
            ORDER BY symbol, period_end DESC, version_filed_at DESC, inserted_at DESC
            LIMIT {{limit:UInt32}} BY symbol
            FORMAT JSONEachRow
            """,
            params,
            source="fundamentals",
            code="sec_facts_unavailable",
            message="SEC 재무 fact 조회 실패",
            missing=missing,
        )
        derived = self._query(
            f"""
            SELECT symbol, metric, value, '' AS unit, fiscal_year AS fiscalYear,
                   fiscal_period AS fiscalPeriod, period_end AS periodEnd,
                   filed_at AS filedAt, version_filed_at AS versionFiledAt,
                   inserted_at AS insertedAt, computed_at AS computedAt, quality, 'derived' AS rowType
            FROM {self._table('sec_derived_metrics')}
            WHERE symbol IN {{symbols:Array(String)}}
              AND filed_at <= toDate(parseDateTime64BestEffort({{requestedAt:String}}))
              AND version_filed_at <= toDate(parseDateTime64BestEffort({{requestedAt:String}}))
              AND computed_at <= parseDateTime64BestEffort({{requestedAt:String}})
              AND inserted_at <= parseDateTime64BestEffort({{requestedAt:String}})
            ORDER BY symbol, period_end DESC, version_filed_at DESC, inserted_at DESC
            LIMIT {{limit:UInt32}} BY symbol
            FORMAT JSONEachRow
            """,
            params,
            source="fundamentals",
            code="sec_derived_unavailable",
            message="SEC 파생 재무지표 조회 실패",
            missing=missing,
        )
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for row in [*facts, *derived]:
            symbol = _symbol(row.get("symbol"))
            filed_at = _date(row.get("filedAt"))
            version_at = _date(row.get("versionFiledAt"))
            inserted_at = _utc(row.get("insertedAt"))
            computed_at = _utc(row.get("computedAt"))
            if not symbol or filed_at is None or version_at is None or inserted_at is None:
                continue
            if inserted_at > cutoff or (computed_at is not None and computed_at > cutoff):
                continue
            source_candidates = [inserted_at, _date_start(version_at)]
            if computed_at is not None:
                source_candidates.append(computed_at)
            source_at = max(source_candidates)
            by_symbol.setdefault(symbol, []).append({
                "metric": row.get("metric"),
                "value": _number(row.get("value")),
                "unit": row.get("unit") or None,
                "fiscalYear": row.get("fiscalYear"),
                "fiscalPeriod": row.get("fiscalPeriod"),
                "periodEnd": str(row.get("periodEnd")) if row.get("periodEnd") is not None else None,
                "filedAt": filed_at.isoformat(),
                "versionFiledAt": version_at.isoformat(),
                "insertedAt": _iso(inserted_at),
                "computedAt": _iso(computed_at),
                "quality": row.get("quality"),
                "rowType": row.get("rowType"),
                "availabilityPrecision": "date",
                "source": f"clickhouse.sec_{'derived_metrics' if row.get('rowType') == 'derived' else 'financial_facts'}",
                "sourceAsOf": _iso(source_at),
            })
        by_fill: dict[str, Any] = {}
        accepted_times: list[str | None] = []
        max_metrics = max(1, int(os.getenv("COACH_FUNDAMENTAL_METRICS_PER_FILL", "20")))
        for fill in fills:
            # SEC availability is only a Date in the current schema. Excluding
            # the fill date prevents an intraday trade from seeing a filing
            # whose exact acceptance time is unknown.
            eligible = [
                item for item in by_symbol.get(fill["symbol"], [])
                if (_date(item.get("filedAt")) or date.max) < _market_date(fill["decisionCutoff"])
                and (_date(item.get("versionFiledAt")) or date.max) < _market_date(fill["decisionCutoff"])
                and (_utc(item.get("insertedAt")) or datetime.max.replace(tzinfo=timezone.utc)) <= fill["decisionCutoff"]
                and (_utc(item.get("computedAt")) or datetime.min.replace(tzinfo=timezone.utc)) <= fill["decisionCutoff"]
            ]
            eligible.sort(
                key=lambda item: (str(item.get("periodEnd") or ""), str(item.get("versionFiledAt") or ""), str(item.get("insertedAt") or "")),
                reverse=True,
            )
            latest_by_metric: dict[str, dict[str, Any]] = {}
            for item in eligible:
                metric = str(item.get("metric") or "")
                if metric and metric not in latest_by_metric:
                    latest_by_metric[metric] = item
            items = list(latest_by_metric.values())[:max_metrics]
            by_fill[fill["fillId"]] = {
                "symbol": fill["symbol"],
                "asOf": _iso(fill["decisionCutoff"]),
                "availabilityPrecision": "date",
                "items": items,
            }
            accepted_times.extend(item.get("sourceAsOf") for item in items)
        if fills and not any(row["items"] for row in by_fill.values()):
            missing.append(_missing("fundamentals", "point_in_time_fundamentals_missing", "거래 시점 이전 SEC 재무 증거가 없습니다."))
        return {"byFillId": by_fill, "availabilityPrecision": "date"}, _max_iso(accepted_times)

    def _earnings_context(
        self,
        symbols: list[str],
        cutoff: datetime,
        missing: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], str | None]:
        if not symbols:
            return {}, None
        rows = self._query(
            f"""
            SELECT symbol, metric, average, low, high, analyst_count AS analystCount,
                   collected_at AS collectedAt, inserted_at AS insertedAt, raw
            FROM {self._table('yahoo_earnings_estimates')}
            WHERE symbol IN {{symbols:Array(String)}}
              AND JSONExtractString(raw, 'sourceFrame') = 'earnings_dates'
              AND collected_at <= parseDateTime64BestEffort({{requestedAt:String}})
              AND inserted_at <= parseDateTime64BestEffort({{requestedAt:String}})
            ORDER BY symbol, collected_at DESC, inserted_at DESC
            LIMIT 40 BY symbol
            FORMAT JSONEachRow
            """,
            {"symbols": symbols, "requestedAt": _iso(cutoff)},
            source="earnings",
            code="yahoo_earnings_unavailable",
            message="Yahoo 실적 일정 저장 데이터 조회 실패",
            missing=missing,
        )
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            symbol = _symbol(row.get("symbol"))
            collected = _utc(row.get("collectedAt"))
            inserted = _utc(row.get("insertedAt"))
            raw = _json_object(row.get("raw"))
            event_at = _utc(raw.get("date"))
            if not symbol or collected is None or inserted is None or event_at is None:
                continue
            if collected > cutoff or inserted > cutoff or _market_date(event_at) < _market_date(cutoff):
                continue
            available_at = max(collected, inserted)
            by_symbol.setdefault(symbol, []).append({
                "eventAt": event_at,
                "average": _number(row.get("average")),
                "low": _number(row.get("low")),
                "high": _number(row.get("high")),
                "analystCount": row.get("analystCount"),
                "source": "yahoo-finance.earnings_dates",
                "sourceAsOf": _iso(available_at),
            })
        result: dict[str, Any] = {}
        accepted_times: list[str | None] = []
        for symbol in symbols:
            candidates = sorted(by_symbol.get(symbol, []), key=lambda item: item["eventAt"])
            if not candidates:
                continue
            item = candidates[0]
            result[symbol] = {
                "earningsAt": _iso(item["eventAt"]),
                "earningsDaysRemaining": (_market_date(item["eventAt"]) - _market_date(cutoff)).days,
                "estimate": item.get("average"),
                "estimateLow": item.get("low"),
                "estimateHigh": item.get("high"),
                "analystCount": item.get("analystCount"),
                "source": item.get("source"),
                "sourceAsOf": item.get("sourceAsOf"),
                "historicalRevisionAvailable": False,
            }
            accepted_times.append(item.get("sourceAsOf"))
        if any(symbol not in result for symbol in symbols):
            missing.append(_missing("earnings", "earnings_schedule_missing", "일부 종목의 cutoff 이전 수집 실적 일정을 찾지 못했습니다."))
        # ReplacingMergeTree keeps the latest consensus per fiscal key. Never
        # claim that it can reproduce a historical Yahoo revision.
        return result, _max_iso(accepted_times)

    def _ontology_context(
        self,
        symbols: list[str],
        cutoff: datetime,
        missing: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], None]:
        context: dict[str, Any] = {
            "temporalScope": "current-only",
            "historicalSimilarityEligible": False,
            "sourceAsOf": None,
            "requestedCutoff": _iso(cutoff),
            "retrievedAt": _iso(self._now_provider()),
            "symbols": symbols,
            "items": [],
        }
        if not symbols:
            return context, None
        try:
            provider = self._ontology_provider()
            if provider is None:
                raise RuntimeError("ontology provider is not configured")
            from gops_agents.providers import ProviderRequest

            evidence = provider.fetch(ProviderRequest(symbol=symbols[0], symbols=tuple(symbols[1:]), intent="coach_snapshot"))
            context["items"] = [
                item.to_dict() if hasattr(item, "to_dict") else dict(item)
                for item in evidence or []
                if hasattr(item, "to_dict") or isinstance(item, dict)
            ]
        except Exception as exc:
            missing.append(_unavailable("ontology", "ontology_provider_unavailable", "GraphDB current ontology 조회 실패", exc))
        if not any(str(item.get("status") or "") == "available" for item in context["items"]):
            missing.append(_missing("ontology", "current_ontology_missing", "현재 GraphDB 온톨로지 증거가 없습니다."))
        return context, None

    def _query(
        self,
        query: str,
        params: dict[str, Any],
        *,
        source: str,
        code: str,
        message: str,
        missing: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if getattr(self, "_clickhouse_failed", False):
            return []
        try:
            provider = self._clickhouse_provider()
            rows = provider.query_json_each_row(query, params)
            return [dict(row) for row in rows or [] if isinstance(row, dict)]
        except Exception as exc:
            self._clickhouse_failed = True
            missing.append(_unavailable(source, code, message, exc))
            return []

    def _table(self, name: str) -> str:
        provider = self._clickhouse_provider()
        if hasattr(provider, "table"):
            return provider.table(name)
        return f"market_data.{name}"

    def _clickhouse_provider(self):
        if self._clickhouse is None:
            from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider

            self._clickhouse = ClickHouseMarketDataProvider()
        return self._clickhouse

    def _ontology_provider(self):
        if self._ontology is None:
            from gops_agents.providers import GraphDBOntologyProvider

            self._ontology = GraphDBOntologyProvider()
        return self._ontology

    def _resolve_heatmap_path(self) -> Path | None:
        candidates: list[Path] = []
        if self._heatmap_seed_path:
            candidates.append(self._heatmap_seed_path)
        configured = str(os.getenv("HEATMAP_UNIVERSE_REGISTRY_PATH") or "").strip()
        if configured:
            candidates.append(Path(configured).expanduser())
        repo_root = Path(__file__).resolve().parents[5]
        candidates.extend([
            repo_root / "systems/market-data/config/sp500-heatmap-seed.json",
            Path("/app/systems/market-data/config/sp500-heatmap-seed.json"),
        ])
        return next((path for path in candidates if path.is_file()), None)


def _normalize_fills(fills: list[dict[str, Any]], requested_at: datetime) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in fills or []:
        if not isinstance(row, dict):
            continue
        fill_id = str(row.get("fillId") or row.get("fill_id") or "").strip()
        symbol = _symbol(row.get("symbol"))
        filled_at = _utc(row.get("filledAt") or row.get("filled_at"))
        if not fill_id or not symbol or filled_at is None or filled_at > requested_at:
            continue
        decision_at = _utc(row.get("decisionAt") or row.get("decision_at")) or filled_at
        decision_at = min(decision_at, filled_at, requested_at)
        result.append({
            "fillId": fill_id,
            "symbol": symbol,
            "cutoff": decision_at,
            "decisionCutoff": decision_at,
            "entryCutoff": filled_at,
        })
    return result


def _stored_quote(row: dict[str, Any], cutoff: datetime, source: str) -> dict[str, Any] | None:
    symbol = _symbol(row.get("symbol"))
    price = _number(row.get("price"))
    source_at = _utc(row.get("sourceAsOf"))
    if not symbol or price is None or source_at is None or not _quote_is_fresh(source_at, cutoff):
        return None
    return {"symbol": symbol, "price": price, "source": source, "sourceAsOf": _iso(source_at)}


def _quote_is_fresh(source_at: datetime, cutoff: datetime) -> bool:
    """Reject future or clearly stale quotes without inventing a last price."""

    try:
        max_age_minutes = max(1, int(os.getenv("COACH_CURRENT_QUOTE_MAX_AGE_MINUTES", "5760")))
    except ValueError:
        max_age_minutes = 5760
    return source_at <= cutoff and cutoff - source_at <= timedelta(minutes=max_age_minutes)


def _portfolio_metadata(
    symbol: str,
    cutoff: datetime,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> dict[str, Any]:
    for snapshot in (before, after):
        if not isinstance(snapshot, dict):
            continue
        source_at = _utc(snapshot.get("sourceAsOf") or snapshot.get("asOf") or snapshot.get("updatedAt"))
        if source_at is None or source_at > cutoff:
            continue
        positions = snapshot.get("positions")
        if isinstance(positions, dict):
            positions = list(positions.values())
        for row in positions if isinstance(positions, list) else []:
            if not isinstance(row, dict) or _symbol(row.get("symbol") or row.get("ticker")) != symbol:
                continue
            return {
                "companyName": row.get("companyName") or row.get("company_name") or row.get("name"),
                "sector": row.get("sector"),
                "industry": row.get("industry"),
                "source": "portfolio_snapshot",
                "sourceAsOf": _iso(source_at),
            }
    return {}


def _metadata_at(rows: list[dict[str, Any]], cutoff: datetime) -> dict[str, Any]:
    eligible = [row for row in rows if (_utc(row.get("sourceAsOf")) or datetime.max.replace(tzinfo=timezone.utc)) <= cutoff]
    eligible.sort(key=lambda row: str(row.get("sourceAsOf") or ""), reverse=True)
    return eligible[0] if eligible else {}


def _symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            try:
                parsed = datetime.combine(date.fromisoformat(text[:10]), time.min)
            except (TypeError, ValueError):
                return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "")[:10])
    except (TypeError, ValueError):
        return None


def _date_start(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def _market_date(value: datetime) -> date:
    return value.astimezone(MARKET_TIMEZONE).date()


def _iso(value: Any) -> str | None:
    parsed = _utc(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed else None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sector_key(value: Any) -> str:
    return "".join(str(value or "").lower().replace("&", "and").split())


def _sector_market(value: Any) -> tuple[str, str, str] | None:
    key = _sector_key(value)
    aliases = {
        "technology": "기술", "informationtechnology": "기술", "semiconductor": "반도체",
        "healthcare": "헬스케어", "consumerstaples": "필수소비재", "energy": "에너지",
        "financials": "금융", "financial": "금융", "industrials": "산업재", "materials": "소재",
        "utilities": "유틸리티", "realestate": "부동산", "communicationservices": "커뮤니케이션",
        "consumerdiscretionary": "임의소비재",
    }
    normalized = aliases.get(key, str(value or ""))
    normalized_key = _sector_key(normalized)
    return next((item for item in SECTOR_MARKETS if _sector_key(item[0]) == normalized_key), None)


def _daily_returns(points: list[tuple[datetime, float]]) -> dict[str, float]:
    result: dict[str, float] = {}
    previous: float | None = None
    for timestamp, close in sorted(points, key=lambda item: item[0]):
        if previous is not None and previous > 0:
            result[timestamp.date().isoformat()] = (close / previous - 1.0) * 100
        previous = close
    return result


def _correlation(left: dict[str, float], right: dict[str, float]) -> float | None:
    keys = sorted(set(left) & set(right))[-60:]
    if len(keys) < 10:
        return None
    first = [left[key] for key in keys]
    second = [right[key] for key in keys]
    first_mean = sum(first) / len(first); second_mean = sum(second) / len(second)
    numerator = sum((a - first_mean) * (b - second_mean) for a, b in zip(first, second))
    first_variance = sum((a - first_mean) ** 2 for a in first); second_variance = sum((b - second_mean) ** 2 for b in second)
    if first_variance <= 0 or second_variance <= 0:
        return None
    return round(numerator / (first_variance * second_variance) ** 0.5, 4)


def _period_return(points: list[tuple[datetime, float]], days: int) -> float | None:
    ordered = sorted(points, key=lambda item: item[0])
    if len(ordered) <= days or ordered[-days - 1][1] <= 0:
        return None
    return round((ordered[-1][1] / ordered[-days - 1][1] - 1.0) * 100, 4)


def _latest_close_time(series: dict[str, list[tuple[datetime, float]]]) -> str | None:
    values = [timestamp for points in series.values() for timestamp, _ in points]
    return _iso(max(values)) if values else None


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _max_iso(values: list[Any]) -> str | None:
    parsed = [_utc(value) for value in values]
    return _iso(max(value for value in parsed if value is not None)) if any(value is not None for value in parsed) else None


def _missing(source: str, code: str, message: str) -> dict[str, str]:
    return {"source": source, "code": code, "message": message}


def _unavailable(source: str, code: str, message: str, exc: Exception) -> dict[str, str]:
    return _missing(source, code, f"{message}: {exc.__class__.__name__}")


def _dedupe_missing(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (str(item.get("source") or "unknown"), str(item.get("code") or "unknown"))
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(item))
    return result
