from __future__ import annotations

from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.company_journal.models import GenerationRequest, NarrativeDraft
from app.company_journal.repository import compact_graph_expansion
from app.company_journal.routes import get_company_journal_service, router
from app.company_journal.service import CompanyJournalService, calculate_server_metrics, source_receipt
from app.services.simulation_guard import requires_point_in_time_data


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
    assert "revenue_growth_yoy" in first_missing


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


def test_worker_persists_only_a_validated_report_then_completes_request():
    repository = FakeRepository()
    result = CompanyJournalService(repository=repository, writer=FakeWriter()).process_pending(10)
    assert result == {"completed": 1, "failed": 0, "superseded": 0}
    assert repository.inserted is not None
    assert [event[1] for event in repository.events] == ["processing", "completed"]
    assert repository.inserted["receipt"]["newsIds"] == ["news-1"]


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

        def panel_evidence(self, symbol, benchmark_symbols):
            self.calls.append((symbol, benchmark_symbols))
            return {
                "contractVersion": "company-journal-evidence.v1",
                "symbol": symbol,
                "sourceAsOf": "2026-06-30",
                "financialSeries": [{"period": "2026Q2", "revenue": 100}],
                "earningsSeries": [{"period": "2026Q2", "actualEps": 2.3, "estimatedEps": 2.1}],
                "performanceSeries": [{"symbol": symbol, "candles": [{"timestamp": "2026-07-15", "close": 104}]}],
                "missingData": [],
            }

    service = RouteService()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_company_journal_service] = lambda: service
    response = TestClient(app).get("/api/company-journal/googl/evidence?benchmarks=SPY,XLK")
    assert response.status_code == 200
    assert response.json()["contractVersion"] == "company-journal-evidence.v1"
    assert service.calls == [("GOOGL", ["SPY", "XLK"])]


def test_company_journal_evidence_does_not_remove_other_simulation_guards():
    assert requires_point_in_time_data("/api/market/fundamentals/NVDA/series") is True
    assert requires_point_in_time_data("/api/agents/analyze") is True
    assert requires_point_in_time_data("/api/charts/analysis-assets", "GET") is False
    assert requires_point_in_time_data("/api/charts/analysis-assets", "DELETE") is True
    assert requires_point_in_time_data("/api/charts/analysis-assets/build", "GET") is True
    assert requires_point_in_time_data("/api/company-journal/NVDA/evidence") is False
