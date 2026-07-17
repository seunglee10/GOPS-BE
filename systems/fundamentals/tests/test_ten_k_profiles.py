import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))

from fundamentals.ten_k_profiles import (  # noqa: E402
    RISK_CATEGORIES,
    TenKProfileBackfillConfig,
    TenKProfileSummarizer,
    extract_10k_sections,
    latest_ten_k_filing,
    run_ten_k_profile_backfill,
    ten_k_profile_key,
    validate_generated_profile,
)


def sample_document() -> str:
    business = "The company designs products and earns revenue from product sales and services. " * 50
    risks = "The company depends on suppliers, customers, competition, regulation, and global markets. " * 55
    return f"""
    <html><body>
      <div>Table of Contents</div><div>Item 1. Business</div><div>Item 1A. Risk Factors</div>
      <h1>Item 1. Business</h1><p>{business}</p>
      <h1>Item 1A. Risk Factors</h1><p>{risks}</p>
      <h1>Item 1B. Unresolved Staff Comments</h1><p>None.</p>
    </body></html>
    """


class FakeRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def setex(self, key, _ttl, value):
        self.values[key] = value


class FakeS3:
    def __init__(self):
        self.objects = []

    def put_object(self, **kwargs):
        self.objects.append(kwargs)


class FakeSecClient:
    def company_tickers_exchange(self):
        return {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [[1045810, "NVIDIA CORP", "NVDA", "Nasdaq"]],
        }

    def submissions(self, _cik):
        return {
            "filings": {
                "recent": {
                    "form": ["8-K", "10-K"],
                    "accessionNumber": ["0000000000-26-000001", "0001045810-26-000023"],
                    "primaryDocument": ["event.htm", "nvda-20260125.htm"],
                    "filingDate": ["2026-03-01", "2026-02-20"],
                    "reportDate": ["2026-03-01", "2026-01-25"],
                }
            }
        }

    def filing_document_url(self, cik, accession, primary_document):
        return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{primary_document}"

    def filing_document(self, _cik, _accession, _primary_document):
        return sample_document()


def sample_business_model() -> dict:
    return {
        "structure": "팹리스 — 설계 전담, 생산 외주",
        "segments": [
            {"name": "데이터센터", "detail": "GPU · 네트워킹 · AI 솔루션"},
            {"name": "게이밍", "detail": "GeForce GPU"},
        ],
        "revenueModel": ["하드웨어 판매", "소프트웨어 유상 라이선스"],
        "platform": "CUDA 중심 소프트웨어 스택",
    }


class FakeSummarizer:
    def __init__(self):
        self.calls = []

    def summarize(self, *, company, filing, sections, source_url, raw_sections_s3_key):
        self.calls.append((company.symbol, filing.accession, len(sections.item_1_business)))
        return {
            "symbol": company.symbol,
            "companyName": company.company_name,
            "sourceFiling": f"10-K 2026 accession {filing.accession}",
            "sourceAccession": filing.accession,
            "sourceUrl": source_url,
            "filingDate": filing.filing_date,
            "reportDate": filing.report_date,
            "generatedAt": "2026-07-16T00:00:00Z",
            "businessModel": sample_business_model(),
            "revenueDrivers": ["제품 판매"],
            "competitivePosition": "문서에 기재된 경쟁 환경을 요약합니다.",
            "riskFactors": [{"category": "공급망", "summary": "공급자 의존이 있습니다.", "severityHint": "high"}],
            "rawSectionsS3Key": raw_sections_s3_key,
        }


class TenKProfileTests(unittest.TestCase):
    def test_latest_ten_k_ignores_newer_non_ten_k_filing(self):
        filing = latest_ten_k_filing(FakeSecClient().submissions("1045810"), "1045810")

        self.assertEqual(filing.accession, "0001045810-26-000023")
        self.assertEqual(filing.primary_document, "nvda-20260125.htm")

    def test_parser_skips_toc_and_extracts_item_1_and_item_1a_body(self):
        sections = extract_10k_sections(sample_document())

        self.assertGreater(len(sections.item_1_business), 1200)
        self.assertGreater(len(sections.item_1a_risk_factors), 1500)
        self.assertIn("earns revenue", sections.item_1_business)
        self.assertIn("depends on suppliers", sections.item_1a_risk_factors)

    def test_structured_profile_enforces_fixed_risk_enum_and_deduplicates(self):
        payload = validate_generated_profile({
            "businessModel": sample_business_model(),
            "revenueDrivers": ["제품", "제품"],
            "competitivePosition": "경쟁 환경 설명",
            "riskFactors": [
                {"category": RISK_CATEGORIES[0], "summary": "공급 의존", "severityHint": "high"},
                {"category": RISK_CATEGORIES[0], "summary": "중복", "severityHint": "low"},
            ],
        })

        self.assertEqual(payload["revenueDrivers"], ["제품"])
        self.assertEqual(len(payload["riskFactors"]), 1)
        self.assertEqual(payload["businessModel"]["structure"], "팹리스 — 설계 전담, 생산 외주")
        self.assertEqual(len(payload["businessModel"]["segments"]), 2)

    def test_business_model_requires_structured_fields(self):
        with self.assertRaises(ValueError):
            validate_generated_profile({
                "businessModel": "제품을 판매합니다.",
                "revenueDrivers": ["제품"],
                "competitivePosition": "경쟁 환경 설명",
                "riskFactors": [
                    {"category": RISK_CATEGORIES[0], "summary": "공급 의존", "severityHint": "high"},
                ],
            })

    def test_business_model_platform_is_optional(self):
        business = sample_business_model()
        business["platform"] = None
        payload = validate_generated_profile({
            "businessModel": business,
            "revenueDrivers": ["제품"],
            "competitivePosition": "경쟁 환경 설명",
            "riskFactors": [
                {"category": RISK_CATEGORIES[0], "summary": "공급 의존", "severityHint": "high"},
            ],
        })
        self.assertIsNone(payload["businessModel"]["platform"])

    def test_summarizer_builds_document_only_strict_schema_request(self):
        captured = {}

        def requester(payload):
            captured.update(payload)
            return {
                "businessModel": sample_business_model(),
                "revenueDrivers": ["제품 수요"],
                "competitivePosition": "경쟁이 빠르게 변합니다.",
                "riskFactors": [{"category": "경쟁", "summary": "경쟁 압력이 있습니다.", "severityHint": "medium"}],
            }

        company = type("Company", (), {"symbol": "NVDA", "company_name": "NVIDIA"})()
        filing = latest_ten_k_filing(FakeSecClient().submissions("1045810"), "1045810")
        card = TenKProfileSummarizer(
            read_config=lambda name: "present" if name == "OPENAI_API_KEY" else None,
            response_requester=requester,
        ).summarize(
            company=company,
            filing=filing,
            sections=extract_10k_sections(sample_document()),
            source_url="https://www.sec.gov/example",
            raw_sections_s3_key="fundamentals/sec/10k-profiles/NVDA/raw.json",
        )

        self.assertTrue(captured["text"]["format"]["strict"])
        self.assertEqual(
            captured["text"]["format"]["schema"]["properties"]["riskFactors"]["items"]["properties"]["category"]["enum"],
            list(RISK_CATEGORIES),
        )
        self.assertEqual(card["sourceAccession"], "0001045810-26-000023")

    def test_backfill_writes_raw_sections_to_s3_and_card_only_to_redis(self):
        redis = FakeRedis()
        s3 = FakeS3()
        summarizer = FakeSummarizer()
        config = TenKProfileBackfillConfig(
            dry_run=False,
            symbols=["NVDA"],
            s3_bucket="test-bucket",
            user_agent="GOPS test contact@example.com",
        )
        with patch.dict(os.environ, {
            "REDIS_URL": "redis://example",
            "OPENAI_API_KEY": "not-a-real-key",
        }):
            stats = run_ten_k_profile_backfill(
                config,
                sec_client=FakeSecClient(),
                redis_client=redis,
                s3_client=s3,
                summarizer=summarizer,
            )

        self.assertEqual(stats.profiles_written, 1)
        self.assertEqual(len(s3.objects), 1)
        raw_payload = json.loads(s3.objects[0]["Body"])
        self.assertIn("item1Business", raw_payload)
        card = json.loads(redis.values[ten_k_profile_key("NVDA")])
        self.assertNotIn("item1Business", card)
        self.assertEqual(card["sourceAccession"], "0001045810-26-000023")

    def test_unchanged_accession_skips_download_and_llm(self):
        redis = FakeRedis()
        redis.values[ten_k_profile_key("NVDA")] = json.dumps({"sourceAccession": "0001045810-26-000023"})
        summarizer = FakeSummarizer()
        config = TenKProfileBackfillConfig(
            dry_run=False,
            symbols=["NVDA"],
            s3_bucket="test-bucket",
            user_agent="GOPS test contact@example.com",
        )
        with patch.dict(os.environ, {"REDIS_URL": "redis://example", "OPENAI_API_KEY": "not-a-real-key"}):
            stats = run_ten_k_profile_backfill(
                config,
                sec_client=FakeSecClient(),
                redis_client=redis,
                s3_client=FakeS3(),
                summarizer=summarizer,
            )

        self.assertEqual(stats.unchanged_skipped, 1)
        self.assertEqual(summarizer.calls, [])


if __name__ == "__main__":
    unittest.main()
