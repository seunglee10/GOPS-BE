from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.company_journal.models import GenerationRequest, NarrativeDraft
from app.company_journal.repository import CompanyJournalRepository, compact_graph_expansion
from app.company_journal.routes import get_company_journal_service, router
from app.company_journal.service import (
    CompanyJournalService,
    calculate_server_metrics,
    company_journal_history_years,
    compact_analyst_summary,
    source_receipt,
)
from app.services.simulation_guard import requires_point_in_time_data, supports_cutoff_safe_simulation_read


def source_bundle() -> dict:
    return {
        "symbol": "GOOGL", "analysisAsOf": "2026-07-15", "company": {"company_name": "Alphabet Inc."},
        "prices": [{"date": "2026-07-10", "close": 100}, {"date": "2026-07-13", "close": 101},
                   {"date": "2026-07-14", "close": 102}, {"date": "2026-07-15", "close": 104.2}],
        "benchmarkPrices": [{"date": "2026-07-10", "close": 100}, {"date": "2026-07-13", "close": 100.5},
                            {"date": "2026-07-14", "close": 101}, {"date": "2026-07-15", "close": 101.6}],
        "news": [{"date": "2026-07-15", "summary": "Gemini와 Cloud", "article_ids": ["news-1"]}],
        "financialMetrics": [{"metric": "liabilities_to_equity", "value": 0.42, "accession": "sec-1"},
                             {"metric": "current_ratio", "value": 1.8, "accession": "sec-1"},
                             {"metric": "free_cash_flow", "value": 123.0, "accession": "sec-1"}],
        "earningsActuals": [
            {"metric": "eps", "value": 2.31, "fiscal_year": 2026, "fiscal_period": "Q2", "accession": "sec-1"},
            {"metric": "revenue", "value": 96_400_000_000, "fiscal_year": 2026, "fiscal_period": "Q2", "accession": "sec-1"},
        ],
        "earningsEstimates": [
            {"metric": "eps", "average": 2.18, "fiscal_year": 2026, "fiscal_period": "Q2", "collected_at": "2026-07-14 21:15:00.000"},
            {"metric": "revenue", "average": 94_800_000_000, "fiscal_year": 2026, "fiscal_period": "Q2", "collected_at": "2026-07-14 21:15:00.000"},
        ],
        "filings": [{"accession": "sec-1", "form": "10-Q"}], "graph": {"relation_version": "graph-7"},
    }


class FakeRepository:
    def __init__(self) -> None:
        self.bundle = source_bundle()
        self.events: list[tuple[str, str, str | None]] = []
        self.inserted = None
        self.requests = [GenerationRequest("request-1", "GOOGL", date(2026, 7, 15), "digest-1", "post_market")]

    def load_source_bundle(self, symbol):
        assert symbol == "GOOGL"
        return self.bundle

    def input_digest(self, bundle): return "digest-1"
    def latest_verified(self, symbol): return None
    def latest_verified_for_digest(self, symbol, digest): return None
    def enqueue(self, bundle, digest, source): return self.requests[0]
    def daily_candidates(self, limit): return ["GOOGL"][:limit]
    def pending_requests(self, limit): return self.requests[:limit]

    def append_request_event(self, request, status, error=None):
        self.events.append((request.request_id, status, error))

    def insert_verified_report(self, request, draft, metrics, receipt, missing_data):
        self.inserted = {"request": request, "draft": draft, "metrics": metrics, "receipt": receipt, "missing": missing_data}


class FakeWriter:
    def generate(self, bundle, metrics, missing_data):
        return NarrativeDraft(
            headline="Gemini와 Cloud 성장을 실제 실적으로 확인할 시점입니다.", keywords=["Gemini", "Cloud 성장"],
            recent_movement="최근 3거래일 동안 시장보다 강한 흐름을 보였습니다.",
            financial_stability="확인된 부채 부담은 안정적이지만 현금흐름을 함께 봐야 합니다.",
            watch_items="다음 실적에서 Cloud 성장률과 잉여현금흐름을 확인해야 합니다.",
            tabs={"current": "현재 핵심입니다.", "growth": "성장 지표를 확인합니다.",
                  "profitability": "수익성 지표를 확인합니다.", "stability": "안정성 지표를 확인합니다.",
                  "earnings": "실제 실적과 예상치를 확인합니다.", "valuation": "가치평가를 확인합니다."},
            model="fake-model",
        )


def test_server_metrics_are_deterministic_and_separate_from_narrative():
    first, first_missing = calculate_server_metrics(source_bundle())
    second, second_missing = calculate_server_metrics(source_bundle())
    assert first == second and first_missing == second_missing
    assert first["stockReturnPercent"] == 4.2
    assert first["benchmarkReturnPercent"] == 1.6
    assert first["relativeReturnPercentagePoints"] == 2.6
    assert first["financial"]["liabilitiesToEquity"] == 0.42
    assert "analystOutlook" not in first
    assert "revenue_growth_yoy" in first_missing


def test_analyst_summary_projection_is_compact_and_passes_through_without_recomposition():
    assert compact_analyst_summary({
        "statement": "  JPMorgan 의견과 시장 평균을 조합한 문장입니다.  ",
        "tone": "positive",
        "source_as_of": "2026-07-15 12:00:00.000",
        "collected_at": "2026-07-15 21:15:00.000",
        "source": "yahoo-finance",
    }) == {
        "statement": "JPMorgan 의견과 시장 평균을 조합한 문장입니다.",
        "tone": "positive",
        "sourceAsOf": "2026-07-15 12:00:00.000",
        "collectedAt": "2026-07-15 21:15:00.000",
        "source": "yahoo-finance",
    }
    assert compact_analyst_summary({"statement": ""}) is None


def test_source_receipt_contains_ids_not_duplicated_source_documents():
    assert source_receipt(source_bundle()) == {
        "newsIds": ["news-1"], "secFilingIds": ["sec-1"], "priceAsOf": "2026-07-15",
        "graphRelationIds": ["graph-7"], "financialAccessions": ["sec-1"],
        "earningsPeriods": ["2026-Q2"], "earningsEstimateAsOf": "2026-07-14 21:15:00.000",
    }


def test_graph_expansion_is_bounded_before_openai_input():
    compact = compact_graph_expansion({
        "relation_version": "v2",
        "generated_at": "2026-07-15T00:00:00Z",
        "payload": '{"keywords":["AI"],"themes":[{"name":"Cloud","score":0.9}],"related_symbols":[{"symbol":"MSFT","relation_type":"peer","score":0.8}]}'
    })
    assert compact["keywords"] == ["AI"]
    assert compact["themes"][0]["name"] == "Cloud"
    assert compact["relatedSymbols"][0]["symbol"] == "MSFT"


def test_missing_optional_graph_projection_does_not_block_source_bundle():
    class ClickHouseClient:
        database = "market_data"

        def query_json_each_row(self, query, parameters=None):
            if "agent_graph_expansions" in query:
                raise RuntimeError("optional graph projection is not deployed")
            if "chart_candles" in query:
                return [{"date": "2026-07-15", "close": 104.2}]
            return []

    bundle = CompanyJournalRepository(client=ClickHouseClient()).load_source_bundle("NVDA")

    assert bundle["symbol"] == "NVDA"
    assert bundle["analysisAsOf"] == "2026-07-15"
    assert bundle["graph"]["keywords"] == []


def test_company_journal_source_bundle_reads_actual_history_from_2021_without_fabricating_yahoo_rows():
    class ClickHouseClient:
        database = "market_data"

        def __init__(self):
            self.queries = []

        def query_json_each_row(self, query, parameters=None):
            self.queries.append(query)
            if "chart_candles" in query:
                return [{"date": "2026-07-15", "close": 104.2}]
            return []

    client = ClickHouseClient()
    bundle = CompanyJournalRepository(client=client).load_source_bundle("NVDA")
    history_queries = [query for query in client.queries if "earnings_estimates" in query or "sec_financial_facts" in query]

    assert len(history_queries) == 2
    assert all("period_end >= toDate('2021-01-01')" in query for query in history_queries)
    assert bundle["earningsActuals"] == []
    assert bundle["earningsEstimates"] == []
    assert "analystSummary" not in bundle


def test_performance_series_deduplicates_candles_before_per_symbol_limit():
    class ClickHouseClient:
        database = "market_data"

        def __init__(self):
            self.query = ""

        def query_json_each_row(self, query, parameters=None):
            self.query = query
            return [{
                "symbol": "NVDA", "event_time": "2026-07-15T00:00:00Z",
                "open": 100, "high": 105, "low": 99, "close": 104.2, "volume": 10,
            }]

    client = ClickHouseClient()
    cutoff = datetime.fromisoformat("2026-07-15T00:00:00+09:00")
    result = CompanyJournalRepository(client=client).load_performance_series(["NVDA"], cutoff=cutoff)

    assert result[0]["candles"][0]["close"] == 104.2
    assert "GROUP BY symbol, event_time" in client.query
    assert client.query.index("GROUP BY symbol, event_time") < client.query.index("LIMIT 520 BY symbol")
    assert "toDate(toTimeZone(event_time, 'America/New_York'))" in client.query


def test_point_in_time_queries_bound_every_temporal_company_journal_source():
    class ClickHouseClient:
        database = "market_data"

        def __init__(self):
            self.queries = []

        def query_json_each_row(self, query, parameters=None):
            self.queries.append((query, parameters or {}))
            if "chart_candles" in query:
                return [{"date": "2026-07-13", "close": 104.2}]
            return []

    client = ClickHouseClient()
    cutoff = datetime(2026, 7, 14, 15, 0, tzinfo=timezone.utc)

    bundle = CompanyJournalRepository(client=client).load_source_bundle("NVDA", cutoff=cutoff)
    CompanyJournalRepository(client=client).load_performance_series(["NVDA", "SPY"], cutoff=cutoff)

    temporal_queries = "\n".join(query for query, _parameters in client.queries)
    assert bundle["analysisAsOf"] == "2026-07-13"
    assert all(parameters.get("cutoff") == cutoff.isoformat() for query, parameters in client.queries if "{cutoff:String}" in query)
    assert "toDate(toTimeZone(parseDateTime64BestEffort({cutoff:String}), 'America/New_York'))" in temporal_queries
    assert "filed_at < toDate(toTimeZone(parseDateTime64BestEffort({cutoff:String}), 'America/New_York'))" in temporal_queries
    assert "version_filed_at < toDate(toTimeZone(parseDateTime64BestEffort({cutoff:String}), 'America/New_York'))" in temporal_queries
    assert "collected_at <= parseDateTime64BestEffort({cutoff:String})" in temporal_queries
    assert "generated_at <= parseDateTime64BestEffort({cutoff:String})" in temporal_queries


def test_simulation_report_is_reconstructed_from_cutoff_bundle_without_latest_report_fallback():
    class PointInTimeRepository:
        def __init__(self):
            self.cutoffs = []

        def latest_verified(self, _symbol):
            raise AssertionError("simulation must not read the latest live report")

        def load_source_bundle(self, symbol, cutoff=None):
            assert symbol == "GOOGL"
            self.cutoffs.append(cutoff)
            return source_bundle()

        def input_digest(self, _bundle):
            return "point-in-time-digest"

    cutoff = datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc)
    repository = PointInTimeRepository()

    report = CompanyJournalService(repository=repository, writer=FakeWriter()).latest("GOOGL", cutoff=cutoff)

    assert repository.cutoffs == [cutoff]
    assert report is not None
    assert report["sourceMode"] == "historical_reconstruction"
    assert report["sourceCutoff"] == cutoff.isoformat()
    assert report["analysisAsOf"] == "2026-07-15"
    assert report["inputDigest"] == "point-in-time-digest"
    assert report["validationStatus"] == "verified"
    assert report["sourceReceipt"]["priceAsOf"] == "2026-07-15"


def test_simulation_report_finishes_with_explicit_missing_data_when_cutoff_has_no_evidence():
    class EmptyPointInTimeRepository:
        def load_source_bundle(self, symbol, cutoff=None):
            return {
                "symbol": symbol,
                "analysisAsOf": "2026-07-14",
                "company": {"company_name": "NVIDIA"},
                "prices": [], "benchmarkPrices": [], "news": [], "financialMetrics": [],
                "earningsActuals": [], "earningsEstimates": [], "analystActions": [],
                "analystConsensus": [], "filings": [], "graph": {},
            }

        def input_digest(self, _bundle):
            return "empty-point-in-time-digest"

        def latest_verified(self, _symbol):
            raise AssertionError("simulation must not read the latest live report")

    cutoff = datetime(2026, 7, 14, 15, 0, tzinfo=timezone.utc)

    report = CompanyJournalService(repository=EmptyPointInTimeRepository(), writer=FakeWriter()).latest(
        "NVDA", cutoff=cutoff
    )

    assert report is not None
    assert report["validationStatus"] == "verified"
    assert report["sourceMode"] == "historical_reconstruction"
    assert "recent_stock_return" in report["missingData"]
    assert "가상시각 이전의 완료 일봉이 부족" in report["recentMovement"]


def test_worker_persists_only_a_validated_report_then_completes_request():
    repository = FakeRepository()
    result = CompanyJournalService(repository=repository, writer=FakeWriter()).process_pending(10)
    assert result == {"completed": 1, "failed": 0, "superseded": 0}
    assert repository.inserted is not None
    assert [event[1] for event in repository.events] == ["processing", "completed"]
    assert repository.inserted["receipt"]["newsIds"] == ["news-1"]
    assert repository.inserted["draft"].recent_movement == "최근 3거래일 동안 시장보다 강한 흐름을 보였습니다."


def test_missing_market_data_does_not_generate_numbers():
    bundle = source_bundle(); bundle["prices"] = []; bundle["benchmarkPrices"] = []
    metrics, missing = calculate_server_metrics(bundle)
    assert metrics["stockReturnPercent"] is None and metrics["benchmarkReturnPercent"] is None
    assert metrics["relativeReturnPercentagePoints"] is None
    assert "recent_stock_return" in missing and "benchmark_return" in missing


def test_route_returns_pending_and_enqueues_outside_request_handler():
    class RouteService:
        def __init__(self): self.enqueued = []
        def latest(self, symbol): return None
        def enqueue_if_stale(self, symbol, source): self.enqueued.append((symbol, source))

    service = RouteService()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_company_journal_service] = lambda: service
    response = TestClient(app).get("/api/company-journal/googl")
    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    assert response.json()["report"] is None
    assert service.enqueued == [("GOOGL", "panel")]


def test_company_journal_evidence_route_is_read_only_and_simulation_safe():
    class RouteService:
        def __init__(self):
            self.calls = []

        def panel_evidence(self, symbol, benchmark_symbols, *, cutoff=None):
            self.calls.append((symbol, benchmark_symbols, cutoff))
            return {
                "contractVersion": "company-journal-evidence.v1",
                "symbol": symbol,
                "sourceAsOf": "2026-06-30",
                "financialSeries": [{"period": "2026Q2", "revenue": 100}],
                "earningsSeries": [{"period": "2026Q2", "actualEps": 2.3, "estimatedEps": 2.1}],
                "performanceSeries": [{"symbol": symbol, "candles": [{"timestamp": "2026-07-15", "close": 104}]}],
                "analystSummary": None,
                "missingData": [],
            }

    service = RouteService()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_company_journal_service] = lambda: service
    response = TestClient(app).get("/api/company-journal/googl/evidence?benchmarks=SPY,XLK")
    assert response.status_code == 200
    assert response.json()["contractVersion"] == "company-journal-evidence.v1"
    assert response.json()["analystSummary"] is None
    assert service.calls == [("GOOGL", ["SPY", "XLK"], None)]


def test_company_journal_evidence_route_passes_server_owned_simulation_cutoff():
    class RouteService:
        def __init__(self):
            self.cutoff = None

        def panel_evidence(self, symbol, benchmark_symbols, cutoff=None):
            self.cutoff = cutoff
            return {
                "contractVersion": "company-journal-evidence.v1",
                "symbol": symbol,
                "sourceAsOf": None,
                "financialSeries": [],
                "earningsSeries": [],
                "performanceSeries": [],
                "missingData": ["company_daily_prices"],
                "simulation": True,
                "sourceMode": "historical_reconstruction",
                "cutoff": cutoff.isoformat(),
            }

    service = RouteService()
    app = FastAPI()
    app.state.simulator_gateway = type("Gateway", (), {"status": lambda self: {
        "mode": "simulation",
        "virtualTime": "2026-07-15T00:00:00+09:00",
    }})()
    app.include_router(router)
    app.dependency_overrides[get_company_journal_service] = lambda: service

    response = TestClient(app).get("/api/company-journal/NVDA/evidence?benchmarks=SPY")

    assert response.status_code == 200
    assert response.json()["cutoff"] == "2026-07-14T15:00:00+00:00"
    assert service.cutoff == datetime(2026, 7, 14, 15, 0, tzinfo=timezone.utc)


def test_live_company_journal_evidence_requests_history_from_2021(monkeypatch):
    calls = []

    class Adapter:
        def financial_series(self, symbol, years, period, cutoff=None):
            calls.append(("financial", symbol, years, period, cutoff))
            return []

        def earnings_series(self, symbol, years, cutoff=None):
            calls.append(("earnings", symbol, years, cutoff))
            return []

    class EvidenceRepository:
        def load_performance_series(self, symbols, cutoff=None):
            assert symbols == ["NVDA", "SPY"]
            assert cutoff is None
            return []

        def load_analyst_summary(self, symbol, cutoff=None):
            assert symbol == "NVDA"
            assert cutoff is None
            return None

    adapter = Adapter()
    monkeypatch.setattr(
        "app.market_data.fundamentals.service.build_fundamentals_adapter",
        lambda: adapter,
    )

    assert company_journal_history_years(2026) == 6
    result = CompanyJournalService(repository=EvidenceRepository(), writer=FakeWriter()).panel_evidence(
        "NVDA",
        ["SPY"],
    )

    assert calls == [
        ("financial", "NVDA", 6, "quarterly", None),
        ("earnings", "NVDA", 6, None),
    ]
    assert result["financialSeries"] == []
    assert result["earningsSeries"] == []
    assert result["analystSummary"] is None
    assert "yahoo_analyst_summary" in result["missingData"]
    assert result["sourceAsOf"] is None
    assert result["cutoff"] is None


def test_company_journal_analyst_evidence_is_cutoff_safe_and_compact(monkeypatch):
    class Adapter:
        def financial_series(self, symbol, years, period, cutoff=None):
            return []

        def earnings_series(self, symbol, years, cutoff=None):
            return []

    class EvidenceRepository:
        def load_performance_series(self, symbols, cutoff=None):
            return [{
                "symbol": "NVDA",
                "candles": [{"timestamp": "2026-07-15T13:00:00Z", "close": 104}],
            }]

        def load_analyst_summary(self, symbol, cutoff=None):
            assert symbol == "NVDA"
            assert cutoff is None
            return {
                "statement": "JPMorgan 의견과 시장 평균을 조합한 문장입니다.",
                "tone": "positive",
                "source_as_of": "2026-07-15 12:00:00.000",
                "collected_at": "2026-07-15 13:30:00.000",
                "source": "yahoo-finance",
            }

    monkeypatch.setattr(
        "app.market_data.fundamentals.service.build_fundamentals_adapter",
        lambda: Adapter(),
    )
    result = CompanyJournalService(repository=EvidenceRepository(), writer=FakeWriter()).panel_evidence(
        "NVDA",
        ["SPY"],
    )

    assert result["analystSummary"] == {
        "statement": "JPMorgan 의견과 시장 평균을 조합한 문장입니다.",
        "tone": "positive",
        "sourceAsOf": "2026-07-15 12:00:00.000",
        "collectedAt": "2026-07-15 13:30:00.000",
        "source": "yahoo-finance",
    }
    assert result["sourceAsOf"] == "2026-07-15T13:00:00Z"
    assert "yahoo_analyst_summary" not in result["missingData"]


def test_analyst_repository_filters_event_and_collection_times_by_cutoff():
    class ClickHouseClient:
        database = "market_data"

        def __init__(self):
            self.calls = []

        def query_json_each_row(self, query, parameters=None):
            self.calls.append((query, parameters))
            return [{
                "statement": "현재 조합 문장입니다.",
                "tone": "neutral",
                "source_as_of": "2026-07-15 12:00:00.000",
                "collected_at": "2026-07-15 13:30:00.000",
            }]

    client = ClickHouseClient()
    cutoff = datetime.fromisoformat("2026-07-15T23:00:00+09:00")
    result = CompanyJournalRepository(client=client).load_analyst_summary("NVDA", cutoff=cutoff)

    assert result["statement"] == "현재 조합 문장입니다."
    query, parameters = client.calls[0]
    assert "yahoo_analyst_summaries FINAL" in query
    assert "collected_at >= now64(3) - INTERVAL 1 DAY" in query
    assert "collected_at <= parseDateTime64BestEffort({cutoff:String})" in query
    assert "LIMIT 1" in query
    assert parameters["cutoff"] == "2026-07-15T14:00:00+00:00"


def test_point_in_time_evidence_uses_cutoff_repository_instead_of_live_fundamentals_adapter(monkeypatch):
    class EvidenceRepository:
        def load_financial_series_rows(self, symbol, cutoff, history_start_year):
            assert (symbol, cutoff, history_start_year) == ("NVDA", replay_cutoff, 2021)
            return [{
                "symbol": "NVDA", "metric": "revenue", "value": 100,
                "fiscalYear": 2026, "fiscalPeriod": "Q1", "periodEndDate": "2026-03-31",
                "filedAt": "2026-05-01",
            }]

        def load_earnings_series_rows(self, symbol, cutoff, history_start_year):
            assert (symbol, cutoff, history_start_year) == ("NVDA", replay_cutoff, 2021)
            return ([{
                "symbol": "NVDA", "metric": "eps", "value": 2.3,
                "fiscalYear": 2026, "fiscalPeriod": "Q1", "periodEndDate": "2026-03-31",
                "filedAt": "2026-05-01",
            }], [])

        def load_performance_series(self, symbols, cutoff=None):
            assert symbols == ["NVDA", "SPY"]
            assert cutoff == replay_cutoff
            return [{"symbol": "NVDA", "candles": [{"timestamp": "2026-07-13T04:00:00Z", "close": 104}]}]

        def load_analyst_summary(self, symbol, cutoff=None):
            assert symbol == "NVDA"
            assert cutoff == replay_cutoff
            return None

    monkeypatch.setattr(
        "app.market_data.fundamentals.service.build_fundamentals_adapter",
        lambda: (_ for _ in ()).throw(AssertionError("live fundamentals adapter must not be used")),
    )
    replay_cutoff = datetime(2026, 7, 14, 15, 0, tzinfo=timezone.utc)

    result = CompanyJournalService(repository=EvidenceRepository(), writer=FakeWriter()).panel_evidence(
        "NVDA", ["SPY"], cutoff=replay_cutoff
    )

    assert result["simulation"] is True
    assert result["sourceMode"] == "historical_reconstruction"
    assert result["cutoff"] == replay_cutoff.isoformat()
    assert result["financialSeries"][0]["filedAt"] == "2026-05-01"
    assert result["earningsSeries"][0]["actualEps"] == 2.3
    assert result["performanceSeries"][0]["candles"][0]["timestamp"] == "2026-07-13T04:00:00Z"


def test_company_journal_evidence_does_not_remove_other_simulation_guards():
    assert requires_point_in_time_data("/api/market/fundamentals/NVDA/series") is True
    assert requires_point_in_time_data("/api/agents/analyze") is True
    assert requires_point_in_time_data("/api/charts/analysis-assets", "GET") is False
    assert requires_point_in_time_data("/api/charts/order-flow/symbols", "GET") is False
    assert requires_point_in_time_data("/api/charts/order-flow/intraday", "GET") is False
    assert requires_point_in_time_data("/api/charts/order-flow/daily", "GET") is True
    assert requires_point_in_time_data("/api/charts/analysis-assets", "DELETE") is True
    assert requires_point_in_time_data("/api/charts/analysis-assets/build", "GET") is True
    assert requires_point_in_time_data("/api/company-journal/NVDA/evidence") is False
    assert requires_point_in_time_data("/api/company-journal/NVDA") is False
    assert requires_point_in_time_data("/api/company-journal/NVDA", "POST") is True
    assert supports_cutoff_safe_simulation_read("/api/company-journal/NVDA/evidence") is True
    assert supports_cutoff_safe_simulation_read("/api/company-journal/NVDA/evidence", "POST") is False
    assert supports_cutoff_safe_simulation_read("/api/company-journal/NVDA") is False
