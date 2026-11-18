"""AI 답변 SSE delta 중계 테스트 (이슈 #43)."""
import asyncio
import json
import os
import sys
import unittest
import uuid
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
    from app.routes import agents as routes

    ROUTES_AVAILABLE = True
except Exception:
    routes = None
    ROUTES_AVAILABLE = False

REPORT_CHANNEL = "agent.reports"
DELTA_CHANNEL = "agent.reports.delta"


def report_msg(analysis_id, status, **extra):
    return {
        "type": "message",
        "channel": f"{REPORT_CHANNEL}:{analysis_id}",
        "data": json.dumps({"analysisId": analysis_id, "status": status, **extra}),
    }


def delta_msg(analysis_id, seq, text):
    return {
        "type": "message",
        "channel": f"{DELTA_CHANNEL}:{analysis_id}",
        "data": json.dumps({"analysisId": analysis_id, "seq": seq, "text": text}),
    }


class FakePubSub:
    """get_message가 준비된 메시지를 하나씩 돌려주는 가짜 구독. 항목이 예외면 던진다."""

    def __init__(self, items):
        self.items = list(items)
        self.closed = False

    def get_message(self, timeout=0.0):
        if self.items:
            item = self.items.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        import time

        time.sleep(min(timeout, 0.01))
        return None

    def close(self):
        self.closed = True


def parse_events(chunks):
    events = []
    for chunk in chunks:
        if chunk.startswith(":"):
            events.append(("comment", chunk.strip()))
            continue
        name = chunk.split("\n")[0].removeprefix("event: ")
        data = json.loads(chunk.split("\n")[1].removeprefix("data: "))
        events.append((name, data))
    return events


async def collect(analysis_id, limit=50):
    out = []
    async for chunk in routes.stream_agent_report_updates(analysis_id, user_id="u1"):
        out.append(chunk)
        if len(out) >= limit:
            break
    return out


@unittest.skipUnless(ROUTES_AVAILABLE, "agent routes are not importable")
class AgentReportStreamDeltaTest(unittest.IsolatedAsyncioTestCase):
    def patched(self, pubsub, polled=None):
        stack = [
            patch.object(routes, "report_update_pubsub", return_value=pubsub),
            patch.object(routes, "get_agent_report", return_value=polled or {"analysisId": "a1", "status": "running"}),
            patch.object(routes, "with_report_view", side_effect=lambda report: report),
        ]
        for item in stack:
            item.start()
            self.addCleanup(item.stop)

    async def test_deltas_arrive_in_order_then_final_report(self):
        pubsub = FakePubSub([
            report_msg("a1", "running"),
            delta_msg("a1", 1, "첫 "),
            delta_msg("a1", 2, "문장"),
            delta_msg("a1", 3, "입니다."),
            report_msg("a1", "completed", finalAnswer={"summary": "첫 문장입니다."}),
        ])
        self.patched(pubsub)
        events = parse_events(await collect("a1"))

        names = [name for name, _ in events]
        self.assertEqual(names, ["report", "delta", "delta", "delta", "report"])
        self.assertEqual([d["seq"] for n, d in events if n == "delta"], [1, 2, 3])
        self.assertEqual("".join(d["text"] for n, d in events if n == "delta"), "첫 문장입니다.")
        self.assertEqual(events[-1][1]["status"], "completed")
        self.assertTrue(pubsub.closed)

    async def test_duplicate_or_out_of_order_deltas_are_dropped(self):
        pubsub = FakePubSub([
            delta_msg("a1", 1, "a"),
            delta_msg("a1", 2, "b"),
            delta_msg("a1", 2, "b"),
            delta_msg("a1", 1, "a"),
            delta_msg("a1", 3, "c"),
            report_msg("a1", "completed"),
        ])
        self.patched(pubsub)
        events = parse_events(await collect("a1"))
        self.assertEqual([d["seq"] for n, d in events if n == "delta"], [1, 2, 3])

    async def test_delta_for_another_analysis_is_not_forwarded(self):
        pubsub = FakePubSub([
            delta_msg("someone-else", 1, "남의 답변"),
            delta_msg("a1", 1, "내 답변"),
            report_msg("a1", "completed"),
        ])
        self.patched(pubsub)
        events = parse_events(await collect("a1"))
        self.assertEqual([d["text"] for n, d in events if n == "delta"], ["내 답변"])

    async def test_polling_fallback_delivers_final_report_when_pubsub_is_unavailable(self):
        self.patched(None, polled={"analysisId": "a1", "status": "completed", "finalAnswer": {"summary": "완성"}})
        events = parse_events(await collect("a1"))
        self.assertEqual([n for n, _ in events], ["report"])
        self.assertEqual(events[0][1]["finalAnswer"]["summary"], "완성")

    async def test_pubsub_dropping_mid_stream_falls_back_to_polling(self):
        pubsub = FakePubSub([delta_msg("a1", 1, "일부"), delta_msg("a1", 2, " 문장"), ConnectionError("redis dropped")])
        self.patched(pubsub, polled={"analysisId": "a1", "status": "completed", "finalAnswer": {"summary": "일부 문장 완성"}})
        events = parse_events(await collect("a1"))

        self.assertEqual([n for n, _ in events], ["delta", "delta", "report"])
        self.assertEqual(events[-1][1]["status"], "completed")
        self.assertTrue(pubsub.closed)

    async def test_client_disconnect_closes_the_subscription(self):
        pubsub = FakePubSub([delta_msg("a1", 1, "하나"), delta_msg("a1", 2, "둘")])
        self.patched(pubsub)
        generator = routes.stream_agent_report_updates("a1", user_id="u1")
        first = await generator.__anext__()
        self.assertIn("event: delta", first)
        self.assertFalse(pubsub.closed)

        await generator.aclose()  # Starlette는 클라이언트가 끊으면 제너레이터를 닫는다
        self.assertTrue(pubsub.closed)

    async def test_heartbeat_comment_is_sent_while_waiting(self):
        self.patched(FakePubSub([]))
        with patch.object(routes, "read_dotenv_value", side_effect=lambda name: {"AGENT_REPORT_STREAM_HEARTBEAT_SECONDS": "0.05"}.get(name)):
            chunks = await collect("a1", limit=2)
        # 첫 청크는 폴링이 보내는 running 리포트, 그다음 이벤트가 없는 동안 하트비트가 온다.
        self.assertTrue(chunks[0].startswith("event: report"))
        self.assertTrue(chunks[1].startswith(": keep-alive"))

    async def test_response_disables_proxy_buffering(self):
        from app.auth.models import AuthenticatedUser

        with patch.object(routes, "get_agent_report", return_value={"status": "running"}):
            response = await routes.agent_report_stream("a1", user=AuthenticatedUser("u1", "u1@example.com", True))
        self.assertEqual(response.media_type, "text/event-stream")
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        self.assertIn("no-cache", response.headers["cache-control"])


@unittest.skipUnless(ROUTES_AVAILABLE and (os.getenv("TEST_REDIS_URL") or os.getenv("REDIS_URL")), "real Redis is not configured")
class RealRedisTwoServerRelayTest(unittest.IsolatedAsyncioTestCase):
    """서버 2대를 흉내 낸다: 서로 다른 두 SSE 제너레이터(각자 Redis 연결)가 각자의 analysisId 델타만 받는다."""

    async def test_each_stream_receives_only_its_own_deltas(self):
        import redis

        url = os.getenv("TEST_REDIS_URL") or os.getenv("REDIS_URL")
        client = redis.from_url(url, decode_responses=True)
        try:
            client.ping()
        except Exception:
            self.skipTest("Redis is not reachable")
        a, b = f"relay-{uuid.uuid4().hex[:8]}", f"relay-{uuid.uuid4().hex[:8]}"

        def polled(analysis_id, **_):
            return {"analysisId": analysis_id, "status": "running"}

        async def read(analysis_id):
            out = []
            async for chunk in routes.stream_agent_report_updates(analysis_id, user_id="u1"):
                out.append(chunk)
                if '"status":"completed"' in chunk:
                    break
            return parse_events([c for c in out if not c.startswith(":")])

        async def publish_later():
            await asyncio.sleep(0.6)  # 두 구독이 붙은 뒤에 발행한다
            for seq, text in enumerate(["A1 ", "A2"], start=1):
                client.publish(f"{DELTA_CHANNEL}:{a}", json.dumps({"analysisId": a, "seq": seq, "text": text}))
            client.publish(f"{DELTA_CHANNEL}:{b}", json.dumps({"analysisId": b, "seq": 1, "text": "B1"}))
            for analysis_id in (a, b):
                client.publish(f"{REPORT_CHANNEL}:{analysis_id}", json.dumps({"analysisId": analysis_id, "status": "completed"}))

        with (
            patch.dict(os.environ, {"REDIS_URL": url}),
            patch.object(routes, "get_agent_report", side_effect=polled),
            patch.object(routes, "with_report_view", side_effect=lambda report: report),
            patch.object(routes, "read_dotenv_value", side_effect=lambda name: os.environ.get(name)),
        ):
            events_a, events_b, _ = await asyncio.wait_for(asyncio.gather(read(a), read(b), publish_later()), timeout=15)

        self.assertEqual([d["text"] for n, d in events_a if n == "delta"], ["A1 ", "A2"])
        self.assertEqual([d["text"] for n, d in events_b if n == "delta"], ["B1"])


if __name__ == "__main__":
    unittest.main()
