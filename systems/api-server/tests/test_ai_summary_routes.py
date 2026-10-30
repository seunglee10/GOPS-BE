import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for path in (
    ROOT / "systems" / "market-data" / "shared",
    ROOT / "systems" / "order" / "shared",
    ROOT / "systems" / "agent-orchestration" / "shared",
    ROOT / "systems" / "api-server" / "pods" / "api-server",
    ROOT,
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

try:
    from fastapi.testclient import TestClient

    from app.main import create_app

    FASTAPI_TESTCLIENT_AVAILABLE = True
except Exception:
    TestClient = None
    FASTAPI_TESTCLIENT_AVAILABLE = False


class FakeRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)


SUMMARY = {
    "symbol": "NVDA",
    "generatedAt": "2026-10-30T21:00:00Z",
    "head": "데이터센터 수요가 다시 확인되며 거래량이 늘었어요",
    "issueLine": "데이터센터 수요 전망 상향",
    "news": [{"text": "TSMC 9월 매출이 39% 늘었어요.", "newsId": "n_123"}],
    "risks": [{"text": "수출 규제 세부안이 연말까지 나와요.", "newsId": "n_124"}],
    "events": [{"title": "실적 발표", "at": "2026-11-20", "note": "시장 예상 EPS 0.85달러"}],
    "chartNote": "20일선 위에서 거래량 늘며 상승",
}

@unittest.skipUnless(FASTAPI_TESTCLIENT_AVAILABLE, "fastapi TestClient unavailable")
class AiSummaryRoutesTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.redis = FakeRedis()
        self.app.state.summary_cache_redis = self.redis
        self.client = TestClient(self.app)

    def test_stock_ai_summary_returns_cached_d14_shape(self):
        self.redis.values["gops:stocks:ai-summary:NVDA"] = json.dumps(SUMMARY, ensure_ascii=False)
        response = self.client.get("/api/stocks/nvda/ai-summary")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), SUMMARY)

    def test_stock_ai_summary_missing_is_404(self):
        response = self.client.get("/api/stocks/AMD/ai-summary")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "summary_not_ready")

    def test_stock_ai_summary_corrupt_cache_is_404(self):
        self.redis.values["gops:stocks:ai-summary:AMD"] = "not-json"
        response = self.client.get("/api/stocks/AMD/ai-summary")
        self.assertEqual(response.status_code, 404)

    def test_stock_ai_summary_rejects_bad_symbol(self):
        response = self.client.get("/api/stocks/NV$DA/ai-summary")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["code"], "invalid_symbol")


if __name__ == "__main__":
    unittest.main()
