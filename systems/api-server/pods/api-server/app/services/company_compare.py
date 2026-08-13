from typing import Any

from fastapi import HTTPException

from app.contracts.compare import CompanyCompareRequest
from app.market_data.fundamentals.service import build_fundamentals_adapter
from app.services.agent_gateway import request_agent_company_compare_narrative
from app.services.alfaka_market_data import configured_universe_symbols, get_market_data_provider
from gops_agents.company_compare import CompanyCompareAgent, CompanyCompareError, suggest_peers
from gops_agents.providers import (
    ClickHouseFinancialProvider,
    ClickHouseNewsProvider,
    GraphDBOntologyProvider,
    TenKProfileProvider,
)


def _configured_symbols() -> list[str]:
    return configured_universe_symbols()


def _financial_provider() -> ClickHouseFinancialProvider:
    market_provider = get_market_data_provider()
    redis_provider = getattr(market_provider, "redis_provider", None)
    return ClickHouseFinancialProvider(
        clickhouse_provider=getattr(market_provider, "clickhouse_provider", None),
        redis_client=getattr(redis_provider, "redis", None),
    )


def _earnings_lookup(symbols: list[str]) -> dict[str, list[dict[str, Any]]]:
    adapter = build_fundamentals_adapter(provider=get_market_data_provider())
    return {
        symbol: [point.to_public_dict() for point in adapter.earnings_series(symbol, years=3)]
        for symbol in symbols
    }


def _agent() -> CompanyCompareAgent:
    market_provider = get_market_data_provider()
    redis_provider = getattr(market_provider, "redis_provider", None)
    redis_client = getattr(redis_provider, "redis", None)
    clickhouse_provider = getattr(market_provider, "clickhouse_provider", None)
    return CompanyCompareAgent(
        configured_symbols=_configured_symbols,
        financial_provider=_financial_provider(),
        earnings_lookup=_earnings_lookup,
        ten_k_provider=TenKProfileProvider(redis_client=redis_client),
        ontology_provider=GraphDBOntologyProvider(),
        news_provider=ClickHouseNewsProvider(
            clickhouse_provider=clickhouse_provider,
            redis_provider=redis_provider,
            direct_fallback=False,
        ),
    )


def _map_compare_error(error: CompanyCompareError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.detail)


def company_compare_analysis(request: CompanyCompareRequest) -> dict[str, Any]:
    result = company_compare_quantitative(request)
    result["narrative"] = _narrative_or_fallback(result)
    if result["narrative"].get("status") != "ready":
        result["status"] = "partial"
    return result


def company_compare_quantitative(request: CompanyCompareRequest) -> dict[str, Any]:
    """Build only the deterministic layer so the panel never waits for an LLM."""
    try:
        return _agent().compare(request)
    except CompanyCompareError as exc:
        raise _map_compare_error(exc) from exc


def _narrative_or_fallback(result: dict[str, Any]) -> dict[str, Any]:
    try:
        narrative = request_agent_company_compare_narrative({
            "baseSymbol": result.get("baseSymbol"),
            "compareSymbols": result.get("compareSymbols") or [],
            "question": result.get("question"),
            "quantitative": result.get("quantitative") or {},
            "qualitative": result.get("qualitative") or {},
            "sources": result.get("sources") or [],
            "dataGaps": result.get("dataGaps") or [],
        })
        if isinstance(narrative, dict) and narrative.get("status") == "ready":
            return narrative
    except Exception:
        pass
    return {
        "status": "failed",
        "summary": None,
        "sections": [],
        "insights": [],
        "dataGaps": ["성향 서술을 생성하지 못했습니다. 정량 비교는 계속 사용할 수 있습니다."],
    }


def company_compare_candidates(symbol: str) -> dict[str, Any]:
    try:
        return suggest_peers(
            symbol,
            configured_symbols=_configured_symbols,
            ontology_provider=GraphDBOntologyProvider(),
        )
    except CompanyCompareError as exc:
        raise _map_compare_error(exc) from exc
