from __future__ import annotations

import re
from typing import Any, Callable, Protocol

from gops_agents.providers import EvidenceItem, ProviderRequest

from .context import build_qualitative_context, build_quantitative_context


AGENT_ID = "company-compare-agent"
MAX_COMPARE_SYMBOLS = 3
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,9}$")


class CompanyCompareError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class CompanyCompareRequestLike(Protocol):
    baseSymbol: str
    compareSymbols: list[str]
    question: str | None


class FinancialCompareProvider(Protocol):
    def financial_summary(self, symbol: str) -> dict[str, Any] | None:
        ...

    def financial_peer_summary(self, symbol: str) -> dict[str, Any] | None:
        ...


class OntologyCompareProvider(Protocol):
    def fetch(self, request: ProviderRequest) -> list[EvidenceItem]:
        ...


class QualitativeCompareProvider(Protocol):
    def fetch(self, request: ProviderRequest) -> list[EvidenceItem]:
        ...


class CompanyCompareAgent:
    """기업 비교 패널 전담 모듈.

    M1은 저장된 재무/컨센서스 데이터만 조립한다. OpenAI를 호출하지 않으며,
    quantitative와 narrative를 분리해 이후 서술 연결이 정량 렌더링을 막지 않게 한다.
    """

    def __init__(
        self,
        *,
        configured_symbols: Callable[[], list[str]],
        financial_provider: FinancialCompareProvider,
        earnings_lookup: Callable[[list[str]], dict[str, list[dict[str, Any]]]],
        ten_k_provider: QualitativeCompareProvider | None = None,
        ontology_provider: QualitativeCompareProvider | None = None,
        news_provider: QualitativeCompareProvider | None = None,
    ):
        self.configured_symbols = configured_symbols
        self.financial_provider = financial_provider
        self.earnings_lookup = earnings_lookup
        self.ten_k_provider = ten_k_provider
        self.ontology_provider = ontology_provider
        self.news_provider = news_provider

    def compare(self, request: CompanyCompareRequestLike) -> dict[str, Any]:
        symbols = validate_symbols(
            request.baseSymbol,
            request.compareSymbols,
            configured_symbols=self.configured_symbols,
        )
        summaries: dict[str, dict[str, Any] | None] = {}
        provider_gaps: list[str] = []
        for symbol in symbols:
            try:
                summaries[symbol] = self.financial_provider.financial_summary(symbol)
            except Exception as exc:
                summaries[symbol] = None
                provider_gaps.append(f"{symbol}: 재무 provider 조회 실패 ({exc.__class__.__name__})")

        try:
            peer_summary = self.financial_provider.financial_peer_summary(symbols[0])
        except Exception as exc:
            peer_summary = None
            provider_gaps.append(f"SEC frames 조회 실패 ({exc.__class__.__name__})")

        try:
            earnings = self.earnings_lookup(symbols)
        except Exception as exc:
            earnings = {}
            provider_gaps.append(f"실적 컨센서스 조회 실패 ({exc.__class__.__name__})")

        quantitative = build_quantitative_context(
            symbols,
            summaries,
            peer_summary,
            earnings,
            provider_gaps=provider_gaps,
        )
        sources = quantitative.pop("sources")
        data_gaps = list(quantitative.get("dataGaps") or [])

        qualitative_providers_configured = any(
            provider is not None
            for provider in (self.ten_k_provider, self.ontology_provider, self.news_provider)
        )
        qualitative_request = ProviderRequest(
            symbols[0],
            (request.question or "").strip() or "기업 성향 비교",
            symbols=tuple(symbols),
        )
        qualitative_gaps: list[str] = []
        ten_k_evidence = fetch_optional_provider(
            self.ten_k_provider,
            qualitative_request,
            label="10-K 프로파일",
            gaps=qualitative_gaps,
        )
        ontology_evidence = fetch_optional_provider(
            self.ontology_provider,
            qualitative_request,
            label="온톨로지",
            gaps=qualitative_gaps,
        )
        news_evidence = fetch_optional_provider(
            self.news_provider,
            qualitative_request,
            label="뉴스",
            gaps=qualitative_gaps,
        )
        qualitative = build_qualitative_context(
            symbols,
            ten_k_evidence,
            ontology_evidence,
            news_evidence,
            provider_gaps=qualitative_gaps,
        ) if qualitative_providers_configured else {
            "status": "not-requested",
            "sections": [],
            "dataGaps": [],
            "sources": [],
        }
        sources.extend(qualitative.pop("sources"))
        data_gaps.extend(qualitative.get("dataGaps") or [])
        data_gaps = unique_strings(data_gaps)
        overall_status = quantitative["status"]
        if qualitative_providers_configured and qualitative.get("status") != "ready":
            overall_status = "partial"
        return {
            "version": "company-compare.v1",
            "status": overall_status,
            "baseSymbol": symbols[0],
            "compareSymbols": symbols[1:],
            "comparedSymbols": symbols,
            "question": (request.question or "").strip() or None,
            "quantitative": quantitative,
            "qualitative": qualitative,
            "narrative": {
                "status": "not-requested",
                "summary": None,
                "sections": [],
                "insights": [],
                "dataGaps": [],
            },
            "sources": sources,
            "dataGaps": data_gaps,
            "createdByAgentId": AGENT_ID,
        }


def fetch_optional_provider(
    provider: QualitativeCompareProvider | None,
    request: ProviderRequest,
    *,
    label: str,
    gaps: list[str],
) -> list[EvidenceItem]:
    if provider is None:
        return []
    try:
        return list(provider.fetch(request) or [])
    except Exception as exc:
        gaps.append(f"{label} provider 조회 실패 ({exc.__class__.__name__})")
        return []


def validate_symbols(
    base_symbol: str,
    compare_symbols: list[str] | None,
    *,
    configured_symbols: Callable[[], list[str]],
) -> list[str]:
    base = normalize_symbol(base_symbol)
    if not base:
        raise CompanyCompareError(422, "baseSymbol is required.")

    peers: list[str] = []
    for raw in compare_symbols or []:
        symbol = normalize_symbol(raw)
        if symbol and symbol != base and symbol not in peers:
            peers.append(symbol)
    if not peers:
        raise CompanyCompareError(422, "At least one compare symbol is required.")
    if len(peers) > MAX_COMPARE_SYMBOLS:
        raise CompanyCompareError(422, f"At most {MAX_COMPARE_SYMBOLS} compare symbols are allowed.")

    available = {normalize_symbol(symbol) for symbol in configured_symbols()}
    available.discard("")
    if available:
        unsupported = [symbol for symbol in [base, *peers] if symbol not in available]
        if unsupported:
            raise CompanyCompareError(422, f"Unsupported symbols: {', '.join(unsupported)}")
    return [base, *peers]


def suggest_peers(
    symbol: str,
    *,
    configured_symbols: Callable[[], list[str]],
    ontology_provider: OntologyCompareProvider,
    limit: int = 12,
) -> dict[str, Any]:
    base = normalize_symbol(symbol)
    if not base:
        raise CompanyCompareError(422, "symbol is required.")
    available = {normalize_symbol(item) for item in configured_symbols()}
    available.discard("")
    if available and base not in available:
        raise CompanyCompareError(422, f"Unsupported symbols: {base}")

    try:
        evidence = ontology_provider.fetch(ProviderRequest(base, "비교 후보"))
    except Exception as exc:
        evidence = []
        provider_gap = f"온톨로지 후보 조회 실패 ({exc.__class__.__name__})"
    else:
        provider_gap = ""

    candidates: dict[str, dict[str, Any]] = {}
    for item in evidence:
        raw = item.raw if isinstance(item.raw, dict) else {}
        if item.status != "available" or raw.get("type") != "theme-company":
            continue
        ticker = normalize_symbol(str(raw.get("ticker") or ""))
        if not ticker or ticker == base or (available and ticker not in available):
            continue
        candidate = candidates.setdefault(ticker, {
            "symbol": ticker,
            "companyName": raw.get("companyName") or None,
            "relationType": "same-theme",
            "themes": [],
        })
        theme = str(raw.get("themeName") or "").strip()
        if theme and theme not in candidate["themes"]:
            candidate["themes"].append(theme)

    ordered = sorted(candidates.values(), key=lambda item: (-len(item["themes"]), item["symbol"]))[: max(1, limit)]
    gaps = [provider_gap] if provider_gap else []
    if not ordered:
        gaps.append(f"{base}: 같은 테마 비교 후보 없음")
    return {
        "symbol": base,
        "candidates": ordered,
        "dataGaps": gaps,
    }


def normalize_symbol(value: str | None) -> str:
    symbol = str(value or "").strip().upper()
    return symbol if SYMBOL_PATTERN.fullmatch(symbol) else ""


def unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result
