import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
AGENT_SHARED = ROOT / "systems" / "agent-orchestration" / "shared"
for path in (str(MARKET_SHARED), str(AGENT_SHARED)):
    if path not in sys.path:
        sys.path.insert(0, path)

from gops_agents.company_compare import (  # noqa: E402
    BANNED_LANGUAGE,
    CompanyCompareAgent,
    CompanyCompareError,
    CompanyCompareNarrativeError,
    CompanyCompareNarrativeSynthesizer,
    MemoryCompanyCompareNarrativeCache,
    company_compare_schema,
    company_compare_cache_key,
    find_unsupported_numbers,
    suggest_peers,
    validate_narrative,
)
from gops_agents.contracts import EvidenceItem  # noqa: E402
from gops_agents.providers import ProviderRequest, TenKProfileProvider  # noqa: E402


class FakeRequest:
    def __init__(self, base="NVDA", peers=None, question=None):
        self.baseSymbol = base
        self.compareSymbols = peers if peers is not None else ["AMD"]
        self.question = question


class FakeFinancialProvider:
    def __init__(self, summaries=None, peer_summary=None, fail_symbol=None):
        self.summaries = summaries if summaries is not None else sample_summaries()
        self.peer_summary = peer_summary if peer_summary is not None else sample_peer_summary()
        self.fail_symbol = fail_symbol

    def financial_summary(self, symbol):
        if symbol == self.fail_symbol:
            raise TimeoutError("financial timeout")
        return self.summaries.get(symbol)

    def financial_peer_summary(self, _symbol):
        return self.peer_summary


class FakeOntologyProvider:
    def __init__(self, evidence):
        self.evidence = evidence

    def fetch(self, _request):
        return self.evidence


class FakeEvidenceProvider:
    def __init__(self, evidence):
        self.evidence = evidence

    def fetch(self, _request):
        return list(self.evidence)


class FakeRedis:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key):
        return self.values.get(key)


def metric(name, value, period="2026-01-25", quality="available"):
    return {
        "metric": name,
        "value": value,
        "periodEnd": period,
        "asOf": period,
        "quality": quality,
    }


def summary(symbol, values):
    return {
        "symbol": symbol,
        "companyName": "NVIDIA" if symbol == "NVDA" else "Advanced Micro Devices",
        "latest_period": "2026 FY",
        "as_of": "2026-01-25",
        "source_accession": f"{symbol}-accession",
        "metrics": [metric(name, value) for name, value in values.items()],
    }


def sample_summaries():
    return {
        "NVDA": summary("NVDA", {
            "revenue_growth_yoy": 0.55,
            "operating_income_growth_yoy": 0.70,
            "net_income_growth_yoy": 0.62,
            "net_margin": 0.56,
            "operating_margin": 0.61,
            "roe": 0.90,
            "total_debt_to_assets": 0.08,
            "total_debt_to_equity": 0.18,
            "current_ratio": 4.1,
            "free_cash_flow": 60_000_000_000,
            "interest_coverage": 120.0,
        }),
        "AMD": summary("AMD", {
            "revenue_growth_yoy": 0.14,
            "operating_income_growth_yoy": 0.21,
            "net_income_growth_yoy": 0.28,
            "net_margin": 0.16,
            "operating_margin": 0.20,
            "roe": 0.12,
            "total_debt_to_assets": 0.04,
            "total_debt_to_equity": 0.07,
            "current_ratio": 2.5,
            "free_cash_flow": 5_000_000_000,
            "interest_coverage": 35.0,
        }),
    }


def sample_peer_summary():
    return {
        "symbol": "NVDA",
        "frames": [
            {
                "concept": "RevenueFromContractWithCustomerExcludingAssessedTax",
                "unit": "USD",
                "display_period": "CY2025",
                "peers": [
                    {"symbol": "NVDA", "value": 130_000_000_000, "unit": "USD"},
                    {"symbol": "AMD", "value": 30_000_000_000, "unit": "USD"},
                ],
            },
            {
                "concept": "NetIncomeLoss",
                "unit": "USD",
                "display_period": "CY2025",
                "peers": [
                    {"symbol": "NVDA", "value": 70_000_000_000, "unit": "USD"},
                    {"symbol": "AMD", "value": 4_000_000_000, "unit": "USD"},
                ],
            },
        ],
    }


def sample_earnings():
    return {
        "NVDA": [
            {"period": "2025 Q3", "periodEndDate": "2025-10-25", "actualEps": 1.20, "estimatedEps": 1.00},
            {"period": "2025 Q4", "periodEndDate": "2026-01-25", "actualEps": 1.35, "estimatedEps": 1.20},
        ],
        "AMD": [
            {"period": "2025 Q3", "periodEndDate": "2025-09-30", "actualEps": 0.95, "estimatedEps": 1.00},
            {"period": "2025 Q4", "periodEndDate": "2025-12-31", "actualEps": 1.05, "estimatedEps": 1.00},
        ],
    }


def make_agent(*, symbols=("NVDA", "AMD", "TSM"), provider=None, earnings=None):
    return CompanyCompareAgent(
        configured_symbols=lambda: list(symbols),
        financial_provider=provider or FakeFinancialProvider(),
        earnings_lookup=lambda _symbols: sample_earnings() if earnings is None else earnings,
    )


def make_m3_agent():
    profiles = [
        EvidenceItem(
            provider="ten-k-profile",
            status="available",
            title=f"{symbol} 10-K 2026",
            summary=f"{symbol} 사업 모델",
            observedAt="2026-02-20T00:00:00Z",
            url=f"https://www.sec.gov/{symbol}",
            raw={
                "symbol": symbol,
                "sourceFiling": f"10-K 2026 accession {symbol}-10K",
                "sourceAccession": f"{symbol}-10K",
                "reportDate": "2026-01-25",
                "businessModel": f"{symbol}는 제품과 플랫폼으로 수익을 창출합니다.",
                "revenueDrivers": ["제품 수요", "플랫폼 채택"],
                "competitivePosition": "경쟁과 기술 변화가 빠른 시장입니다.",
                "riskFactors": [{"category": "공급망", "summary": "공급자 의존이 있습니다.", "severityHint": "high"}],
            },
        )
        for symbol in ("NVDA", "AMD")
    ]
    ontology = [
        EvidenceItem(
            provider="ontology",
            status="available",
            title=f"{symbol} AI 반도체 테마",
            summary=f"{symbol}는 AI 반도체 테마에 속합니다.",
            observedAt="2026-07-16T00:00:00Z",
            raw={"type": "ticker-theme", "relationType": "theme", "ticker": symbol, "themeName": "AI 반도체"},
        )
        for symbol in ("NVDA", "AMD")
    ]
    ontology.append(EvidenceItem(
        provider="ontology",
        status="available",
        title="NVDA-AMD 공통 테마",
        summary="NVDA와 AMD는 모두 AI 반도체 테마에 속합니다.",
        observedAt="2026-07-16T00:00:00Z",
        raw={"type": "cross-symbol-shared-theme", "relationType": "shared-theme", "themeName": "AI 반도체", "symbols": ["NVDA", "AMD"]},
    ))
    news = [
        EvidenceItem(
            provider="news",
            status="available",
            title=f"{symbol} recent event",
            summary=f"{symbol} 관련 최근 이벤트입니다.",
            observedAt="2026-07-15T00:00:00Z",
            url=f"https://news.example/{symbol}",
            raw={
                "articleId": f"article-{symbol}",
                "targetSymbol": symbol,
                "source": "Example News",
                "publishedAt": "2026-07-15T00:00:00Z",
                "eventType": "regulation" if symbol == "NVDA" else "product",
                "impactDirection": "negative" if symbol == "NVDA" else "neutral",
                "importanceScore": 0.9,
                "relevanceScore": 0.95,
            },
        )
        for symbol in ("NVDA", "AMD")
    ]
    return CompanyCompareAgent(
        configured_symbols=lambda: ["NVDA", "AMD", "TSM"],
        financial_provider=FakeFinancialProvider(),
        earnings_lookup=lambda _symbols: sample_earnings(),
        ten_k_provider=FakeEvidenceProvider(profiles),
        ontology_provider=FakeEvidenceProvider(ontology),
        news_provider=FakeEvidenceProvider(news),
    )


class CompanyCompareAgentTests(unittest.TestCase):
    def test_m1_returns_quantitative_without_openai_or_verdict(self):
        result = make_agent().compare(FakeRequest(question="성향을 비교해줘"))

        self.assertEqual(result["version"], "company-compare.v1")
        self.assertEqual(result["createdByAgentId"], "company-compare-agent")
        self.assertEqual(result["comparedSymbols"], ["NVDA", "AMD"])
        self.assertEqual(result["narrative"]["status"], "not-requested")
        self.assertNotIn("verdict", result)
        self.assertNotIn("better", str(result))

    def test_quantitative_sections_cover_four_m1_themes(self):
        result = make_agent().compare(FakeRequest())
        ids = [section["id"] for section in result["quantitative"]["sections"]]

        self.assertEqual(ids, ["growth_style", "profit_structure", "financial_health", "earnings_stability"])

    def test_growth_chart_keeps_backend_values_and_display(self):
        result = make_agent().compare(FakeRequest())
        chart = result["quantitative"]["growthChart"]

        self.assertEqual(chart["categories"][0]["id"], "revenue_growth_yoy")
        self.assertEqual(chart["series"][0]["values"][0]["value"], 0.55)
        self.assertEqual(chart["series"][0]["values"][0]["display"], "+55.0%")

    def test_duplicate_metric_history_selects_latest_period_not_first_row(self):
        summaries = sample_summaries()
        summaries["NVDA"]["metrics"] = [
            metric("revenue_growth_yoy", -0.10, period="2013-01-27"),
            metric("revenue_growth_yoy", 0.55, period="2026-04-26"),
        ]
        result = make_agent(provider=FakeFinancialProvider(summaries=summaries)).compare(FakeRequest())
        growth = next(section for section in result["quantitative"]["sections"] if section["id"] == "growth_style")
        row = next(item for item in growth["metrics"] if item["id"] == "revenue_growth_yoy")

        self.assertEqual(row["values"][0]["value"], 0.55)
        self.assertEqual(row["values"][0]["asOf"], "2026-04-26")

    def test_metric_older_than_recency_guard_is_exposed_as_data_gap(self):
        summaries = sample_summaries()
        summaries["NVDA"]["metrics"] = [
            metric("current_ratio", 4.0, period="2026-04-26"),
            metric("interest_coverage", 22.0, period="2024-04-28"),
        ]
        result = make_agent(provider=FakeFinancialProvider(summaries=summaries)).compare(FakeRequest())

        self.assertTrue(any(gap == "NVDA: 이자보상배율 데이터 없음" for gap in result["dataGaps"]))

    def test_sec_frames_create_same_period_aligned_fact_rows(self):
        result = make_agent().compare(FakeRequest())
        quantitative = result["quantitative"]

        self.assertEqual(quantitative["periodAlignment"]["status"], "aligned")
        self.assertEqual(quantitative["periodAlignment"]["framePeriods"], ["CY2025"])
        self.assertEqual(quantitative["alignedFacts"][0]["id"], "revenue")
        self.assertEqual(quantitative["alignedFacts"][0]["values"][1]["symbol"], "AMD")

    def test_earnings_stability_is_calculated_before_narrative(self):
        result = make_agent().compare(FakeRequest())
        section = next(item for item in result["quantitative"]["sections"] if item["id"] == "earnings_stability")
        mean_row = next(item for item in section["metrics"] if item["id"] == "eps_surprise_mean")

        self.assertAlmostEqual(mean_row["values"][0]["value"], 0.1625)
        self.assertEqual(mean_row["values"][0]["sourceRef"], "earnings:NVDA")

    def test_missing_company_data_degrades_to_partial_and_data_gaps(self):
        provider = FakeFinancialProvider(summaries={"NVDA": sample_summaries()["NVDA"]})
        result = make_agent(provider=provider, earnings={"NVDA": sample_earnings()["NVDA"]}).compare(FakeRequest())

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["quantitative"]["missingFundamentals"], ["AMD"])
        self.assertTrue(any(gap.startswith("AMD:") for gap in result["dataGaps"]))

    def test_provider_failure_is_a_data_gap_not_whole_request_failure(self):
        provider = FakeFinancialProvider(fail_symbol="AMD")
        result = make_agent(provider=provider).compare(FakeRequest())

        self.assertEqual(result["status"], "partial")
        self.assertTrue(any("TimeoutError" in gap for gap in result["dataGaps"]))

    def test_m3_builds_all_four_qualitative_sections_from_stored_evidence(self):
        result = make_m3_agent().compare(FakeRequest(question="성향을 비교해줘"))

        self.assertEqual(
            [section["id"] for section in result["qualitative"]["sections"]],
            ["business_model", "risk_profile", "relationship", "recent_flow"],
        )
        all_ids = {
            *[section["id"] for section in result["quantitative"]["sections"]],
            *[section["id"] for section in result["qualitative"]["sections"]],
        }
        self.assertEqual(len(all_ids), 8)
        self.assertIn("tenk:NVDA", {source["id"] for source in result["sources"]})
        self.assertIn("news:AMD:article-AMD", {source["id"] for source in result["sources"]})

    def test_ten_k_provider_reads_redis_card_without_external_fallback(self):
        redis = FakeRedis({
            "profile:10k:NVDA": __import__("json").dumps({
                "symbol": "NVDA",
                "sourceFiling": "10-K 2026 accession test",
                "sourceAccession": "test",
                "generatedAt": "2026-07-16T00:00:00Z",
                "businessModel": "제품 판매 중심입니다.",
                "revenueDrivers": ["제품"],
                "competitivePosition": "경쟁 시장입니다.",
                "riskFactors": [{"category": "경쟁", "summary": "경쟁 압력", "severityHint": "medium"}],
            }, ensure_ascii=False),
        })
        evidence = TenKProfileProvider(redis_client=redis).fetch(ProviderRequest("NVDA", "비교"))

        self.assertEqual(evidence[0].provider, "ten-k-profile")
        self.assertEqual(evidence[0].status, "available")
        self.assertEqual(evidence[0].raw["sourceAccession"], "test")

    def test_rejects_missing_peers(self):
        with self.assertRaises(CompanyCompareError) as ctx:
            make_agent().compare(FakeRequest(peers=[]))
        self.assertEqual(ctx.exception.status_code, 422)

    def test_rejects_unsupported_symbol(self):
        with self.assertRaises(CompanyCompareError) as ctx:
            make_agent(symbols=("NVDA", "AMD")).compare(FakeRequest(peers=["ZZZZ"]))
        self.assertEqual(ctx.exception.status_code, 422)

    def test_rejects_too_many_peers(self):
        with self.assertRaises(CompanyCompareError) as ctx:
            make_agent(symbols=()).compare(FakeRequest(peers=["A", "B", "C", "D"]))
        self.assertEqual(ctx.exception.status_code, 422)

    def test_duplicate_and_base_symbols_are_removed(self):
        result = make_agent().compare(FakeRequest(peers=["AMD", "amd", "NVDA"]))
        self.assertEqual(result["comparedSymbols"], ["NVDA", "AMD"])

    def test_narrative_schema_has_plan_ids_and_no_judgment_fields(self):
        schema = company_compare_schema()
        section_ids = schema["properties"]["sections"]["items"]["properties"]["id"]["enum"]

        self.assertIn("business_model", section_ids)
        self.assertIn("recent_flow", section_ids)
        self.assertNotIn("verdict", schema["properties"])
        self.assertNotIn("better", str(schema))

    def test_candidates_come_from_ontology_theme_members_not_hardcoded_map(self):
        evidence = [
            EvidenceItem(
                provider="ontology",
                status="available",
                title="AI 반도체 관련 기업",
                summary="AMD는 AI 반도체 테마 기업입니다.",
                observedAt="2026-07-16T00:00:00Z",
                raw={
                    "type": "theme-company",
                    "ticker": "AMD",
                    "companyName": "Advanced Micro Devices",
                    "themeName": "AI 반도체",
                },
            ),
            EvidenceItem(
                provider="ontology",
                status="available",
                title="AI 반도체 관련 기업",
                summary="NVDA는 AI 반도체 테마 기업입니다.",
                observedAt="2026-07-16T00:00:00Z",
                raw={"type": "theme-company", "ticker": "NVDA", "themeName": "AI 반도체"},
            ),
        ]

        payload = suggest_peers(
            "NVDA",
            configured_symbols=lambda: ["NVDA", "AMD", "TSM"],
            ontology_provider=FakeOntologyProvider(evidence),
        )

        self.assertEqual(payload["candidates"], [{
            "symbol": "AMD",
            "companyName": "Advanced Micro Devices",
            "relationType": "same-theme",
            "themes": ["AI 반도체"],
        }])


class CompanyCompareNarrativeTests(unittest.TestCase):
    def setUp(self):
        self.base_result = make_agent().compare(FakeRequest(question="성향을 비교해줘"))

    def test_strict_openai_payload_uses_only_active_sections_and_evidence_refs(self):
        captured = {}

        def requester(payload):
            captured.update(payload)
            return {
                "summary": "두 기업의 수치 차이를 중립적으로 정리한 정보성 분석입니다.",
                "sections": [
                    {
                        "id": section_id,
                        "heading": heading,
                        "analysis": "주입된 수치와 근거에서 서로 다른 성향이 관찰됩니다.",
                        "evidenceRefs": ["financial:NVDA", "financial:AMD"],
                    }
                    for section_id, heading in (
                        ("growth_style", "성장 스타일"),
                        ("profit_structure", "수익 구조"),
                        ("financial_health", "재무 체질"),
                        ("earnings_stability", "실적 안정성"),
                    )
                ],
                "insights": ["수치의 차이는 성향 대비를 보여주며 우열 판정은 포함하지 않습니다."],
                "dataGaps": [],
            }

        narrative = CompanyCompareNarrativeSynthesizer(
            read_config=lambda name: "test-key" if name == "OPENAI_API_KEY" else None,
            response_requester=requester,
        ).synthesize(self.base_result)

        schema = captured["text"]["format"]
        self.assertTrue(schema["strict"])
        self.assertEqual(
            schema["schema"]["properties"]["sections"]["items"]["properties"]["id"]["enum"],
            ["growth_style", "profit_structure", "financial_health", "earnings_stability"],
        )
        self.assertIn(
            "financial:NVDA",
            schema["schema"]["properties"]["sections"]["items"]["properties"]["evidenceRefs"]["items"]["enum"],
        )
        self.assertEqual(narrative["status"], "ready")
        self.assertEqual(narrative["sections"][0]["id"], "growth_style")

    def test_m3_strict_schema_includes_all_eight_active_sections(self):
        result = make_m3_agent().compare(FakeRequest(question="성향을 비교해줘"))
        captured = {}

        def requester(payload):
            captured.update(payload)
            section_ids = payload["text"]["format"]["schema"]["properties"]["sections"]["items"]["properties"]["id"]["enum"]
            allowed_refs = payload["text"]["format"]["schema"]["properties"]["sections"]["items"]["properties"]["evidenceRefs"]["items"]["enum"]
            return {
                "summary": "여덟 가지 축을 중립적으로 정리한 정보성 분석입니다.",
                "sections": [
                    {"id": section_id, "heading": section_id, "analysis": "근거에서 확인된 성향입니다.", "evidenceRefs": [allowed_refs[0]]}
                    for section_id in section_ids
                ],
                "insights": [],
                "dataGaps": [],
            }

        narrative = CompanyCompareNarrativeSynthesizer(
            read_config=lambda name: "test-key" if name == "OPENAI_API_KEY" else None,
            response_requester=requester,
        ).synthesize(result)

        schema = captured["text"]["format"]["schema"]
        ids = schema["properties"]["sections"]["items"]["properties"]["id"]["enum"]
        self.assertEqual(ids, [
            "business_model", "growth_style", "profit_structure", "financial_health",
            "earnings_stability", "risk_profile", "relationship", "recent_flow",
        ])
        self.assertEqual(schema["properties"]["sections"]["minItems"], 8)
        self.assertEqual(len(narrative["sections"]), 8)

    def test_lazy_cache_second_request_does_not_call_openai(self):
        calls = []
        cache = MemoryCompanyCompareNarrativeCache()

        def requester(payload):
            calls.append(payload)
            section_ids = payload["text"]["format"]["schema"]["properties"]["sections"]["items"]["properties"]["id"]["enum"]
            refs = payload["text"]["format"]["schema"]["properties"]["sections"]["items"]["properties"]["evidenceRefs"]["items"]["enum"]
            return {
                "summary": "저장 근거를 중립적으로 정리한 정보성 분석입니다.",
                "sections": [
                    {"id": section_id, "heading": section_id, "analysis": "근거에서 확인된 성향입니다.", "evidenceRefs": [refs[0]]}
                    for section_id in section_ids
                ],
                "insights": [],
                "dataGaps": [],
            }

        synthesizer = CompanyCompareNarrativeSynthesizer(
            read_config=lambda name: "test-key" if name == "OPENAI_API_KEY" else None,
            response_requester=requester,
            cache=cache,
        )
        first = synthesizer.synthesize(self.base_result)
        second = synthesizer.synthesize(self.base_result)

        self.assertEqual(len(calls), 1)
        self.assertEqual(first["cache"]["status"], "miss")
        self.assertEqual(second["cache"]["status"], "hit")
        self.assertEqual(first["sections"], second["sections"])

    def test_cache_key_changes_when_financial_as_of_changes(self):
        updated = copy.deepcopy(self.base_result)
        financial_source = next(source for source in updated["sources"] if source["id"] == "financial:NVDA")
        financial_source["asOf"] = "2026-04-26"

        original_key = company_compare_cache_key(self.base_result)
        updated_key = company_compare_cache_key(updated)

        self.assertNotEqual(original_key, updated_key)
        self.assertIn("10k=none", original_key)

    def test_cache_key_changes_when_ten_k_accession_changes(self):
        result = make_m3_agent().compare(FakeRequest(question="성향을 비교해줘"))
        updated = copy.deepcopy(result)
        ten_k_source = next(source for source in updated["sources"] if source["id"] == "tenk:NVDA")
        ten_k_source["accession"] = "NEW-ACCESSION"

        self.assertNotEqual(company_compare_cache_key(result), company_compare_cache_key(updated))

    def test_missing_openai_key_is_explicit(self):
        with self.assertRaises(CompanyCompareNarrativeError) as ctx:
            CompanyCompareNarrativeSynthesizer(read_config=lambda _name: None).synthesize(self.base_result)
        self.assertEqual(ctx.exception.status_code, 503)

    def test_prohibited_judgment_language_is_rejected(self):
        with self.assertRaises(CompanyCompareNarrativeError):
            validate_narrative(
                {
                    "summary": "NVDA가 더 좋다.",
                    "sections": [],
                    "insights": [],
                    "dataGaps": [],
                },
                section_ids=("growth_style",),
                evidence_refs=("financial:NVDA",),
            )

    def test_every_prohibited_phrase_is_rejected(self):
        for phrase in BANNED_LANGUAGE:
            with self.subTest(phrase=phrase), self.assertRaises(CompanyCompareNarrativeError):
                validate_narrative(
                    {
                        "summary": f"정보성 분석이지만 {phrase} 표현을 포함합니다.",
                        "sections": [{
                            "id": "growth_style",
                            "heading": "성장 스타일",
                            "analysis": "확장 속도의 차이가 관찰됩니다.",
                            "evidenceRefs": ["financial:NVDA"],
                        }],
                        "insights": [],
                        "dataGaps": [],
                    },
                    section_ids=("growth_style",),
                    evidence_refs=("financial:NVDA",),
                )

    def test_three_pair_golden_set_passes_strict_quality_contract(self):
        fixture_path = Path(__file__).parent / "fixtures" / "company_compare_golden.json"
        cases = json.loads(fixture_path.read_text(encoding="utf-8"))

        self.assertEqual(len(cases), 3)
        for case in cases:
            with self.subTest(pair=case["id"]):
                validated = validate_narrative(
                    case["narrative"],
                    section_ids=tuple(case["sectionIds"]),
                    evidence_refs=tuple(case["evidenceRefs"]),
                )
                self.assertEqual(
                    [section["id"] for section in validated["sections"]],
                    case["sectionIds"],
                )
                self.assertEqual(len(validated["sections"]), 8)
                combined = " ".join(iter_narrative_text_for_test(validated))
                self.assertFalse(any(term in combined for term in BANNED_LANGUAGE))

    def test_empty_section_or_missing_evidence_is_rejected(self):
        for analysis, refs in (("", ["financial:NVDA"]), ("차이가 관찰됩니다.", [])):
            with self.subTest(analysis=analysis, refs=refs), self.assertRaises(CompanyCompareNarrativeError):
                validate_narrative(
                    {
                        "summary": "정보성 분석이며 투자 판단을 대신하지 않습니다.",
                        "sections": [{
                            "id": "growth_style",
                            "heading": "성장 스타일",
                            "analysis": analysis,
                            "evidenceRefs": refs,
                        }],
                        "insights": [],
                        "dataGaps": [],
                    },
                    section_ids=("growth_style",),
                    evidence_refs=("financial:NVDA",),
                )

    def test_unknown_evidence_reference_is_rejected(self):
        with self.assertRaises(CompanyCompareNarrativeError):
            validate_narrative(
                {
                    "summary": "중립적인 정보성 분석입니다.",
                    "sections": [{
                        "id": "growth_style",
                        "heading": "성장 스타일",
                        "analysis": "확장 속도의 차이가 관찰됩니다.",
                        "evidenceRefs": ["web:unknown"],
                    }],
                    "insights": [],
                    "dataGaps": [],
                },
                section_ids=("growth_style",),
                evidence_refs=("financial:NVDA",),
            )

    def test_numeric_post_validation_flags_only_uninjected_number(self):
        warnings = find_unsupported_numbers(
            {
                "summary": "NVDA는 +55.0%, AMD는 +14.0%이며 별도 수치 999%는 근거에 없습니다.",
                "sections": [],
                "insights": [],
                "dataGaps": [],
            },
            self.base_result["quantitative"],
        )
        self.assertEqual(warnings, ["999%"])


def iter_narrative_text_for_test(payload):
    yield payload["summary"]
    for section in payload["sections"]:
        yield section["heading"]
        yield section["analysis"]
    yield from payload["insights"]
    yield from payload["dataGaps"]


if __name__ == "__main__":
    unittest.main()
