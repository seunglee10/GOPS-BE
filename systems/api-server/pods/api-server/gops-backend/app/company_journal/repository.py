from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timezone
from typing import Any

from alfaka.storage.clickhouse_loader import ClickHouseHttpClient

from .models import CONTRACT_VERSION, PROMPT_VERSION, GenerationRequest, NarrativeDraft


def utc_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class CompanyJournalRepository:
    def __init__(self, client: ClickHouseHttpClient | None = None) -> None:
        self.client = client or ClickHouseHttpClient(
            url=os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
            database=os.getenv("CLICKHOUSE_DATABASE", "market_data"),
            user=os.getenv("CLICKHOUSE_USER", "alfaka"),
            password=os.getenv("CLICKHOUSE_PASSWORD", "alfaka"),
        )

    @property
    def database(self) -> str:
        return self.client.database

    def latest_verified(self, symbol: str) -> dict[str, Any] | None:
        rows = self.client.query_json_each_row(
            f"""
            SELECT *
            FROM {self.database}.company_journal_reports_v1
            WHERE symbol = {{symbol:String}} AND validation_status = 'verified'
            ORDER BY generated_at DESC
            LIMIT 1
            FORMAT JSONEachRow
            """,
            {"symbol": symbol},
        )
        return rows[0] if rows else None

    def latest_verified_for_digest(self, symbol: str, input_digest: str) -> dict[str, Any] | None:
        rows = self.client.query_json_each_row(
            f"""
            SELECT *
            FROM {self.database}.company_journal_reports_v1
            WHERE symbol = {{symbol:String}}
              AND input_digest = {{input_digest:String}}
              AND validation_status = 'verified'
            ORDER BY generated_at DESC
            LIMIT 1
            FORMAT JSONEachRow
            """,
            {"symbol": symbol, "input_digest": input_digest},
        )
        return rows[0] if rows else None

    def load_source_bundle(self, symbol: str) -> dict[str, Any]:
        company = self._one(
            f"SELECT symbol, company_name, cik, exchange, updated_at FROM {self.database}.sec_company_tickers "
            "WHERE symbol = {symbol:String} ORDER BY updated_at DESC LIMIT 1 FORMAT JSONEachRow",
            {"symbol": symbol},
        ) or self._one(
            f"SELECT symbol, name AS company_name, exchange, updated_at FROM {self.database}.symbols "
            "WHERE symbol = {symbol:String} ORDER BY updated_at DESC LIMIT 1 FORMAT JSONEachRow",
            {"symbol": symbol},
        )
        prices = self._rows(
            f"""
            SELECT toDate(event_time) AS date,
                   argMax(close, tuple(inserted_at, ifNull(source_event_id, ''))) AS close
            FROM {self.database}.chart_candles
            WHERE symbol = {{symbol:String}} AND is_closed = 1
              AND lower(interval) IN ('1d', '1day', 'day')
            GROUP BY date
            ORDER BY date DESC LIMIT 520 FORMAT JSONEachRow
            """,
            {"symbol": symbol},
        )
        benchmark = self._rows(
            f"""
            SELECT toDate(event_time) AS date,
                   argMax(close, tuple(inserted_at, ifNull(source_event_id, ''))) AS close
            FROM {self.database}.chart_candles
            WHERE symbol = 'SPY' AND is_closed = 1
              AND lower(interval) IN ('1d', '1day', 'day')
            GROUP BY date
            ORDER BY date DESC LIMIT 520 FORMAT JSONEachRow
            """
        )
        news = self._rows(
            f"""
            SELECT date, summary, key_points, positive_points, concerns, article_ids, generated_at
            FROM {self.database}.news_company_daily_summaries
            WHERE symbol = {{symbol:String}}
              AND locale IN ('ko-KR', 'ko')
              AND status IN ('final', 'rolling', 'ready')
            ORDER BY date DESC, generated_at DESC LIMIT 5 FORMAT JSONEachRow
            """,
            {"symbol": symbol},
        )
        metrics = self._rows(
            f"""
            SELECT metric, value, fiscal_year, fiscal_period, period_end, accession, filed_at, quality
            FROM {self.database}.sec_derived_metrics
            WHERE symbol = {{symbol:String}}
            ORDER BY period_end DESC, filed_at DESC LIMIT 80 FORMAT JSONEachRow
            """,
            {"symbol": symbol},
        )
        filings = self._rows(
            f"""
            SELECT form, filed_at, accession
            FROM {self.database}.sec_filing_events
            WHERE symbol = {{symbol:String}}
            ORDER BY filed_at DESC LIMIT 8 FORMAT JSONEachRow
            """,
            {"symbol": symbol},
        )
        earnings_actuals = self._safe_rows(
            f"""
            SELECT metric, value, fiscal_year, fiscal_period, period_end, filed_at, accession
            FROM {self.database}.sec_financial_facts
            WHERE symbol = {{symbol:String}}
              AND metric IN ('eps', 'revenue')
              AND fiscal_period IN ('Q1', 'Q2', 'Q3', 'Q4')
              AND value IS NOT NULL
              AND period_end >= toDate('2021-01-01')
            ORDER BY metric ASC, period_end DESC, version_filed_at DESC
            LIMIT 1 BY metric, fiscal_year, fiscal_period
            FORMAT JSONEachRow
            """,
            {"symbol": symbol},
        )
        earnings_estimates = self._safe_rows(
            f"""
            SELECT metric, average, low, high, analyst_count, fiscal_year, fiscal_period,
                   period_end, source, collected_at
            FROM {self.database}.yahoo_earnings_estimates
            WHERE symbol = {{symbol:String}}
              AND metric IN ('eps', 'revenue')
              AND fiscal_period IN ('Q1', 'Q2', 'Q3', 'Q4')
              AND average IS NOT NULL
              AND period_end >= toDate('2021-01-01')
            ORDER BY metric ASC, period_end DESC, collected_at DESC
            LIMIT 1 BY metric, fiscal_year, fiscal_period
            FORMAT JSONEachRow
            """,
            {"symbol": symbol},
        )
        graph = self._safe_one(
            f"""
            SELECT relation_version, generated_at, payload
            FROM {self.database}.agent_graph_expansions
            WHERE symbol = {{symbol:String}}
            ORDER BY generated_at DESC LIMIT 1 FORMAT JSONEachRow
            """,
            {"symbol": symbol},
        )
        dates = [str(row.get("date")) for row in prices if row.get("date")]
        analysis_as_of = max(dates) if dates else date.today().isoformat()
        return {
            "symbol": symbol,
            "analysisAsOf": analysis_as_of,
            "company": company or {},
            "prices": list(reversed(prices)),
            "benchmarkPrices": list(reversed(benchmark)),
            "news": news,
            "financialMetrics": metrics,
            "earningsActuals": earnings_actuals,
            "earningsEstimates": earnings_estimates,
            "filings": filings,
            "graph": compact_graph_expansion(graph or {}),
        }

    def load_performance_series(self, symbols: list[str]) -> list[dict[str, Any]]:
        normalized = list(dict.fromkeys(value.strip().upper() for value in symbols if value.strip()))[:3]
        if not normalized:
            return []
        rows = self._safe_rows(
            f"""
            SELECT symbol, event_time, open, high, low, close, volume
            FROM
            (
              SELECT symbol,
                     event_time,
                     argMax(open, tuple(inserted_at, ifNull(source_event_id, ''))) AS open,
                     argMax(high, tuple(inserted_at, ifNull(source_event_id, ''))) AS high,
                     argMax(low, tuple(inserted_at, ifNull(source_event_id, ''))) AS low,
                     argMax(close, tuple(inserted_at, ifNull(source_event_id, ''))) AS close,
                     argMax(volume, tuple(inserted_at, ifNull(source_event_id, ''))) AS volume
              FROM {self.database}.chart_candles
              WHERE symbol IN {{symbols:Array(String)}}
                AND is_closed = 1
                AND lower(interval) IN ('1d', '1day', 'day')
              GROUP BY symbol, event_time
              ORDER BY event_time DESC
              LIMIT 520 BY symbol
            )
            ORDER BY symbol ASC, event_time ASC
            FORMAT JSONEachRow
            """,
            {"symbols": normalized},
        )
        grouped: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in normalized}
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            if symbol not in grouped:
                continue
            grouped[symbol].append({
                "timestamp": row.get("event_time"),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": row.get("volume"),
                "isClosed": True,
            })
        return [{"symbol": symbol, "candles": grouped[symbol]} for symbol in normalized if grouped[symbol]]

    @staticmethod
    def input_digest(bundle: dict[str, Any]) -> str:
        canonical = json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def request_id(symbol: str, analysis_as_of: str, input_digest: str) -> str:
        return hashlib.sha256(f"{symbol}|{analysis_as_of}|{input_digest}".encode("utf-8")).hexdigest()

    def enqueue(self, bundle: dict[str, Any], input_digest: str, source: str) -> GenerationRequest:
        symbol = str(bundle["symbol"])
        analysis_as_of = date.fromisoformat(str(bundle["analysisAsOf"]))
        request = GenerationRequest(
            request_id=self.request_id(symbol, analysis_as_of.isoformat(), input_digest),
            symbol=symbol,
            analysis_as_of=analysis_as_of,
            input_digest=input_digest,
            requested_source=source,
        )
        if not self.request_is_active(request.request_id):
            self.append_request_event(request, "pending")
        return request

    def request_is_active(self, request_id: str) -> bool:
        row = self._one(
            f"""
            SELECT argMax(status, occurred_at) AS status
            FROM {self.database}.company_journal_generation_events_v1
            WHERE request_id = {{request_id:String}}
            FORMAT JSONEachRow
            """,
            {"request_id": request_id},
        )
        return str((row or {}).get("status") or "") in {"pending", "processing", "completed"}

    def pending_requests(self, limit: int) -> list[GenerationRequest]:
        rows = self._rows(
            f"""
            SELECT request_id,
                   argMax(symbol, occurred_at) AS symbol,
                   argMax(analysis_as_of, occurred_at) AS analysis_as_of,
                   argMax(input_digest, occurred_at) AS input_digest,
                   argMax(requested_source, occurred_at) AS requested_source,
                   argMax(status, occurred_at) AS status
            FROM {self.database}.company_journal_generation_events_v1
            GROUP BY request_id
            HAVING status = 'pending'
            ORDER BY max(occurred_at) ASC
            LIMIT {{limit:UInt32}}
            FORMAT JSONEachRow
            """,
            {"limit": limit},
        )
        return [
            GenerationRequest(
                request_id=str(row["request_id"]),
                symbol=str(row["symbol"]),
                analysis_as_of=date.fromisoformat(str(row["analysis_as_of"])),
                input_digest=str(row["input_digest"]),
                requested_source=str(row.get("requested_source") or "unknown"),
            )
            for row in rows
        ]

    def append_request_event(self, request: GenerationRequest, status: str, error: str | None = None) -> None:
        self.client.insert_json_each_row("company_journal_generation_events_v1", [{
            "request_id": request.request_id,
            "symbol": request.symbol,
            "analysis_as_of": request.analysis_as_of.isoformat(),
            "input_digest": request.input_digest,
            "status": status,
            "requested_source": request.requested_source,
            "error": error,
            "occurred_at": utc_text(),
        }])

    def insert_verified_report(
        self,
        request: GenerationRequest,
        draft: NarrativeDraft,
        metrics: dict[str, Any],
        receipt: dict[str, Any],
        missing_data: list[str],
    ) -> None:
        self.client.insert_json_each_row("company_journal_reports_v1", [{
            "symbol": request.symbol,
            "analysis_as_of": request.analysis_as_of.isoformat(),
            "generated_at": utc_text(),
            "input_digest": request.input_digest,
            "contract_version": CONTRACT_VERSION,
            "headline": draft.headline,
            "keywords": draft.keywords,
            "recent_movement": draft.recent_movement,
            "financial_stability": draft.financial_stability,
            "watch_items": draft.watch_items,
            "tab_narratives_json": json.dumps(draft.tabs, ensure_ascii=False, separators=(",", ":")),
            "server_metrics_json": json.dumps(metrics, ensure_ascii=False, separators=(",", ":")),
            "news_ids": receipt["newsIds"],
            "sec_filing_ids": receipt["secFilingIds"],
            "price_as_of": receipt.get("priceAsOf"),
            "graph_relation_ids": receipt["graphRelationIds"],
            "missing_data": missing_data,
            "validation_status": "verified",
            "validation_errors": [],
            "model": draft.model,
            "prompt_version": PROMPT_VERSION,
            "source_receipt_json": json.dumps(receipt, ensure_ascii=False, separators=(",", ":")),
        }])

    def daily_candidates(self, limit: int) -> list[str]:
        rows = self._rows(
            f"""
            SELECT symbol, max(event_time) AS latest
            FROM {self.database}.chart_candles
            WHERE is_closed = 1 AND lower(interval) IN ('1d', '1day', 'day')
            GROUP BY symbol ORDER BY latest DESC, symbol ASC LIMIT {{limit:UInt32}}
            FORMAT JSONEachRow
            """,
            {"limit": limit},
        )
        return [str(row["symbol"]).upper() for row in rows if row.get("symbol")]

    def _one(self, query: str, parameters: dict[str, Any] | None = None) -> dict[str, Any] | None:
        rows = self._rows(query, parameters)
        return rows[0] if rows else None

    def _rows(self, query: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self.client.query_json_each_row(query, parameters)

    def _safe_rows(self, query: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        try:
            return self._rows(query, parameters)
        except Exception:
            return []

    def _safe_one(self, query: str, parameters: dict[str, Any] | None = None) -> dict[str, Any] | None:
        rows = self._safe_rows(query, parameters)
        return rows[0] if rows else None


def compact_graph_expansion(row: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(str(row.get("payload") or "{}"))
    except (TypeError, ValueError):
        payload = {}
    themes = payload.get("themes") or payload.get("relatedThemes") or []
    related = payload.get("related_symbols") or payload.get("relatedSymbols") or []
    return {
        "relation_version": row.get("relation_version"),
        "generated_at": row.get("generated_at"),
        "keywords": [str(value)[:120] for value in (payload.get("keywords") or [])[:12]],
        "themes": [
            {
                "name": str(value.get("name") or "")[:120],
                "category": str(value.get("category") or "")[:120],
                "score": value.get("score"),
                "reason": str(value.get("reason") or "")[:500],
            }
            for value in themes[:8] if isinstance(value, dict)
        ],
        "relatedSymbols": [
            {
                "symbol": str(value.get("symbol") or "")[:15],
                "relationType": str(value.get("relation_type") or value.get("relationType") or "")[:80],
                "score": value.get("score"),
                "reason": str(value.get("reason") or "")[:500],
            }
            for value in related[:8] if isinstance(value, dict)
        ],
    }
