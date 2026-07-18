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

from gops_agents.company_compare import build_qualitative_context  # noqa: E402
from gops_agents.providers import ProviderRequest, TenKProfileProvider  # noqa: E402


class FakeRedis:
    def __init__(self, values: dict[str, str]):
        self.values = values

    def get(self, key: str):
        return self.values.get(key)


def profile_card(business_model, *, risk_factors=None) -> dict:
    return {
        "symbol": "NVDA",
        "sourceFiling": "NVDA 10-K 2026",
        "sourceAccession": "NVDA-10K-2026",
        "reportDate": "2026-01-25",
        "generatedAt": "2026-07-18T00:00:00Z",
        "businessModel": business_model,
        "revenueDrivers": ["데이터센터 수요"],
        "competitivePosition": "CUDA 생태계",
        "riskFactors": risk_factors or [],
    }


def fetch_profile(payload: dict):
    redis = FakeRedis({
        "profile:10k:NVDA": json.dumps(payload, ensure_ascii=False),
    })
    return TenKProfileProvider(redis_client=redis).fetch(ProviderRequest("NVDA", "비교"))[0]


class TenKProfileProviderContractTests(unittest.TestCase):
    def test_structured_business_model_survives_provider_and_context(self):
        evidence = fetch_profile(profile_card({
            "structure": "팹리스 — 설계 전담, 생산 외주",
            "segments": [
                {"name": "Compute & Networking", "detail": "GPU · 네트워킹 · AI 솔루션"},
                {"name": "Graphics", "detail": "게이밍 및 시각화 GPU"},
            ],
            "revenueModel": ["하드웨어 판매", "소프트웨어 유상 라이선스"],
            "platform": "CUDA 중심 소프트웨어 스택",
        }))

        self.assertEqual(evidence.status, "available")
        self.assertIsInstance(evidence.raw["businessModel"], dict)
        self.assertEqual(evidence.summary, "팹리스 — 설계 전담, 생산 외주")

        qualitative = build_qualitative_context(["NVDA"], [evidence], [], [], provider_gaps=[])
        business_section = next(
            section for section in qualitative["sections"] if section["id"] == "business_model"
        )
        business = next(item for item in business_section["items"] if item["kind"] == "10k-business")

        self.assertEqual(business["structure"], "팹리스 — 설계 전담, 생산 외주")
        self.assertEqual(
            business["segments"][0],
            {"name": "Compute & Networking", "detail": "GPU · 네트워킹 · AI 솔루션"},
        )
        self.assertEqual(business["revenueModel"], ["하드웨어 판매", "소프트웨어 유상 라이선스"])
        self.assertEqual(business["platform"], "CUDA 중심 소프트웨어 스택")
        self.assertNotIn("{'structure':", json.dumps(qualitative, ensure_ascii=False))

    def test_legacy_string_business_model_remains_supported(self):
        evidence = fetch_profile(profile_card("제품 판매와 플랫폼 라이선스 중심입니다."))

        self.assertEqual(evidence.status, "available")
        self.assertEqual(evidence.raw["businessModel"], "제품 판매와 플랫폼 라이선스 중심입니다.")

        qualitative = build_qualitative_context(["NVDA"], [evidence], [], [], provider_gaps=[])
        business_section = next(
            section for section in qualitative["sections"] if section["id"] == "business_model"
        )
        business = next(item for item in business_section["items"] if item["kind"] == "10k-business")

        self.assertEqual(business["summary"], "제품 판매와 플랫폼 라이선스 중심입니다.")
        self.assertNotIn("segments", business)

    def test_invalid_business_model_type_never_becomes_python_repr(self):
        evidence = fetch_profile(profile_card(
            [{"structure": "잘못 중첩된 객체"}],
            risk_factors=[{
                "category": "경쟁",
                "summary": "경쟁 압력이 높습니다.",
                "severityHint": "medium",
            }],
        ))

        self.assertEqual(evidence.status, "available")
        self.assertIsNone(evidence.raw["businessModel"])
        self.assertNotIn("[{'structure':", evidence.summary)

        qualitative = build_qualitative_context(["NVDA"], [evidence], [], [], provider_gaps=[])
        self.assertIn("NVDA: 10-K 사업 모델 요약 없음", qualitative["dataGaps"])
        self.assertNotIn("[{'structure':", json.dumps(qualitative, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
