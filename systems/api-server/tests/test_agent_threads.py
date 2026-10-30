"""D16 에이전트 대화 목록, 화면 context, 리포트 view."""

import asyncio
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

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
    from fastapi import HTTPException
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.routes import agents as agent_routes
    from app.services.agent_threads import InMemoryAgentThreadStore, RedisAgentThreadStore, thread_title

    TESTCLIENT_AVAILABLE = True
except Exception:
    TESTCLIENT_AVAILABLE = False

RICH_REPORT = ROOT / "systems" / "agent-orchestration" / "tests" / "fixtures" / "agent_reports" / "rich_price_move.json"


def rich_report(analysis_id="analysis-rich-1"):
    report = json.loads(RICH_REPORT.read_text(encoding="utf-8"))
    report["analysisId"] = analysis_id
    return report


class FakePipeline:
    def __init__(self, redis):
        self.redis, self.ops = redis, []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.ops.append((name, args, kwargs))
            return self
        return record

    def execute(self):
        for name, args, kwargs in self.ops:
            getattr(self.redis, name)(*args, **kwargs)


class FakeRedis:
    """hash·sorted set 명령만 흉내 낸다."""

    def __init__(self):
        self.hashes, self.zsets = {}, {}

    def pipeline(self):
        return FakePipeline(self)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update(mapping)

    def delete(self, key):
        self.hashes.pop(key, None)

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    def zrem(self, key, *members):
        for member in members:
            self.zsets.get(key, {}).pop(member, None)

    def _sorted(self, key):
        return [m for m, _ in sorted(self.zsets.get(key, {}).items(), key=lambda kv: (kv[1], kv[0]))]

    def zrange(self, key, start, end):
        items = self._sorted(key)
        end = len(items) + end if end < 0 else end
        return items[start:end + 1] if end >= start else []

    def zrevrange(self, key, start, end):
        return list(reversed(self._sorted(key)))[start:end + 1]


@unittest.skipUnless(TESTCLIENT_AVAILABLE, "FastAPI TestClient is not available")
class AgentThreadRoutesTest(unittest.TestCase):
    def setUp(self):
        from app.auth.config import AuthConfig
        from app.auth.session_store import MemorySessionStore

        self.env = patch.dict(os.environ, {
            "AUTH_ENABLED": "true",
            "AUTH_SESSION_SECRET": "test-session-secret",
            "AGENT_RATE_LIMIT_ENABLED": "false",
        }, clear=False)
        self.env.start()
        self.app = create_app()
        self.config = AuthConfig.from_env()
        self.sessions = MemorySessionStore(self.config)
        self.app.state.auth_session_store = self.sessions
        self.app.state.agent_thread_store = InMemoryAgentThreadStore()
        clock = {"now": datetime(2026, 10, 30, 1, tzinfo=timezone.utc)}

        def tick():
            clock["now"] += timedelta(seconds=1)
            return clock["now"]

        self.app.state.agent_threads_now_provider = tick
        self.client = TestClient(self.app)
        self.counter = 0

    def tearDown(self):
        self.client.close()
        self.env.stop()

    def authenticate(self, sub):
        from app.auth.models import AuthenticatedUser

        self.client.cookies.set(
            self.config.session_cookie_name,
            self.sessions.create_session(AuthenticatedUser(sub, f"{sub}@example.com", True)),
        )

    def analyze(self, body):
        self.counter += 1
        result = {"analysisId": f"analysis-{self.counter}", "status": "queued", "_status_code": 202}
        with patch("app.routes.agents.request_agent_analysis", return_value=result) as gateway:
            response = self.client.post("/api/agents/analyze", json=body)
        return response, gateway

    def test_context_fills_symbol_and_page_context_and_thread_id_stays_out_of_payload(self):
        self.authenticate("user-a")
        response, gateway = self.analyze({
            "intent": "이 종목 왜 올랐어?",
            "threadId": "thr_client_1",
            "context": {"page": "stock", "symbol": "nvda", "chips": ["거래량", " ", "뉴스"]},
            "uiContext": {"selectedReference": {"type": "chart"}},
        })

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["threadId"], "thr_client_1")
        payload = gateway.call_args.args[0]
        self.assertEqual(payload["symbol"], "NVDA")
        self.assertEqual(payload["uiContext"]["pageContext"], {"page": "stock", "symbol": "NVDA", "chips": ["거래량", "뉴스"]})
        self.assertEqual(payload["uiContext"]["selectedReference"], {"type": "chart"})
        self.assertNotIn("context", payload)
        self.assertNotIn("threadId", payload)

        explicit, gateway = self.analyze({"symbol": "AAPL", "intent": "비교", "context": {"symbol": "NVDA"}})
        self.assertEqual(gateway.call_args.args[0]["symbol"], "AAPL")
        self.assertEqual(self.analyze({"intent": "x", "context": {"page": "home", "extra": 1}})[0].status_code, 422)
        self.assertEqual(self.analyze({"intent": "x", "threadId": "../bad"})[0].status_code, 422)

    def test_threads_group_questions_newest_first_and_are_per_user(self):
        self.authenticate("user-a")
        long_question = "엔비디아가 오늘 왜 이렇게 많이 올랐는지 거래량이랑 뉴스까지 같이 자세히 설명해 줘"
        first, _ = self.analyze({"intent": long_question, "context": {"symbol": "NVDA"}})
        thread_id = first.json()["threadId"]
        self.assertTrue(thread_id.startswith("thr_"))
        self.analyze({"intent": "그럼 AMD 랑 비교하면?", "threadId": thread_id})
        other, _ = self.analyze({"symbol": "AAPL", "intent": "애플 뉴스"})

        items = self.client.get("/api/agents/threads").json()["items"]

        self.assertEqual([item["threadId"] for item in items], [other.json()["threadId"], thread_id])
        self.assertEqual(items[1]["title"], thread_title(long_question))
        self.assertLessEqual(len(items[1]["title"]), 40)
        self.assertEqual(items[1]["symbol"], "NVDA")
        self.assertEqual(items[0]["updatedAt"], "2026-10-30T01:00:03.000Z")
        self.assertEqual(set(items[0]), {"threadId", "title", "symbol", "updatedAt"})

        def report(analysis_id, *, user_id):
            if analysis_id == "analysis-1":
                return rich_report(analysis_id)
            raise HTTPException(status_code=404, detail="Agent report not found.")

        with patch("app.routes.agents.get_agent_report", side_effect=report):
            detail = self.client.get(f"/api/agents/threads/{thread_id}").json()

        self.assertEqual(detail["threadId"], thread_id)
        first_message, second_message = detail["messages"]
        self.assertEqual(first_message["question"], long_question)
        self.assertEqual(first_message["analysisId"], "analysis-1")
        self.assertEqual(first_message["status"], "completed")
        self.assertEqual(first_message["view"]["confidence"], "높음")
        self.assertEqual(second_message, {
            "question": "그럼 AMD 랑 비교하면?", "analysisId": "analysis-2", "status": "missing", "view": None,
        })

        self.authenticate("user-b")
        self.assertEqual(self.client.get("/api/agents/threads").json(), {"items": []})
        missing = self.client.get(f"/api/agents/threads/{thread_id}")
        self.assertEqual((missing.status_code, missing.json()["detail"]["code"]), (404, "thread_not_found"))

    def test_report_responses_carry_view_only_when_there_is_content(self):
        self.authenticate("user-a")
        with patch("app.routes.agents.get_agent_report", return_value=rich_report()):
            body = self.client.get("/api/agents/reports/analysis-rich-1").json()
        self.assertEqual(body["view"]["sections"][0]["title"], "차트")
        self.assertEqual(body["view"]["suggestions"][0]["kind"], "rule")
        with patch("app.routes.agents.get_agent_report", return_value={"analysisId": "a", "status": "queued"}):
            self.assertNotIn("view", self.client.get("/api/agents/reports/a").json())

        completed = {**rich_report("analysis-sync"), "_status_code": 200}
        with patch("app.routes.agents.request_agent_analysis", return_value=completed):
            analyzed = self.client.post("/api/agents/analyze", json={"symbol": "NVDA", "intent": "왜 올랐어"}).json()
        self.assertEqual(analyzed["view"]["conclusion"], "거래량이 평소의 두 배를 넘으며 올랐고, 같은 날 데이터센터 기사가 이어졌어요.")

    def test_sse_report_event_includes_view(self):
        async def collect():
            events = []
            async for chunk in agent_routes.stream_agent_report_updates("analysis-rich-1", user_id="user-a"):
                events.append(chunk)
            return events

        with (
            patch("app.routes.agents.report_update_pubsub", return_value=None),
            patch("app.routes.agents.get_agent_report", return_value=rich_report()),
        ):
            events = asyncio.run(collect())

        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].startswith("event: report\n"))
        payload = json.loads(events[0].split("data: ", 1)[1])
        self.assertEqual(payload["view"]["steps"][0], "질문 이해")


@unittest.skipUnless(TESTCLIENT_AVAILABLE, "FastAPI TestClient is not available")
class AgentThreadStoreTest(unittest.TestCase):
    def exercise(self, store):
        start = datetime(2026, 10, 30, 1, tzinfo=timezone.utc)
        for index in range(52):
            store.record("u", f"t{index}", question=f"질문 {index}", symbol="NVDA", analysis_id=f"a{index}", at=start + timedelta(seconds=index))
        store.record("u", "t51", question="이어서", symbol=None, analysis_id="a-next", at=start + timedelta(minutes=5))
        store.record("u", "t51", question="이어서", symbol=None, analysis_id="a-next", at=start + timedelta(minutes=6))

        items = store.list("u")
        self.assertEqual(len(items), 50)
        self.assertEqual(items[0]["threadId"], "t51")
        self.assertEqual(items[-1]["threadId"], "t2")
        self.assertIsNone(store.get("u", "t0"))
        thread = store.get("u", "t51")
        self.assertEqual(thread["title"], "질문 51")
        self.assertEqual(thread["symbol"], "NVDA")
        self.assertEqual([m["analysisId"] for m in thread["messages"]], ["a51", "a-next"])
        self.assertEqual(store.list("someone-else"), [])

    def test_memory_store(self):
        self.exercise(InMemoryAgentThreadStore())

    def test_redis_store_keys(self):
        redis = FakeRedis()
        self.exercise(RedisAgentThreadStore(redis))
        self.assertIn("gops:agent:threads:v1:u", redis.zsets)
        self.assertIn("gops:agent:threads:v1:u:t:t51", redis.hashes)
        self.assertNotIn("gops:agent:threads:v1:u:t:t0", redis.hashes)


if __name__ == "__main__":
    unittest.main()
