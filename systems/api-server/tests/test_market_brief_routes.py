import json
import sys
import unittest
from datetime import datetime, timezone
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


BRIEF = {
    "marketDate": "2026-10-29",
    "generatedAt": "2026-10-29T20:30:00Z",
    "paragraphs": [{"text": "반도체가 지수 상승을 이끌었어요.", "refs": [{"kind": "symbol", "value": "NVDA"}]}],
}


@unittest.skipUnless(FASTAPI_TESTCLIENT_AVAILABLE, "fastapi TestClient unavailable")
class MarketBriefRoutesTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.redis = FakeRedis()
        self.app.state.summary_cache_redis = self.redis
        # 미국 동부 2026-10-30 17:00
        self.app.state.market_brief_clock = lambda: datetime(2026, 10, 30, 21, 0, tzinfo=timezone.utc)
        self.client = TestClient(self.app)

    def test_market_brief_today(self):
        today = {**BRIEF, "marketDate": "2026-10-30"}
        self.redis.values["gops:market-brief:2026-10-30"] = json.dumps(today, ensure_ascii=False)
        self.redis.values["gops:market-brief:2026-10-29"] = json.dumps(BRIEF, ensure_ascii=False)
        response = self.client.get("/api/market-brief/today")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), today)

    def test_market_brief_falls_back_to_recent_day(self):
        self.redis.values["gops:market-brief:2026-10-29"] = json.dumps(BRIEF, ensure_ascii=False)
        response = self.client.get("/api/market-brief/today")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["marketDate"], "2026-10-29")

    def test_market_brief_older_than_three_days_is_404(self):
        self.redis.values["gops:market-brief:2026-10-26"] = json.dumps({**BRIEF, "marketDate": "2026-10-26"})
        response = self.client.get("/api/market-brief/today")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "brief_not_ready")


if __name__ == "__main__":
    unittest.main()
