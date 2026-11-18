"""합성 LLM summary 델타 스트리밍 테스트 (이슈 #43)."""
from __future__ import annotations

import io
import json
import os
import unittest
import urllib.request
import uuid
from unittest.mock import patch

from gops_agents.synthesis import delta_stream
from gops_agents.synthesis.delta_stream import (
    DeltaPublisher,
    SummaryDeltaExtractor,
    current_delta_publisher,
    delta_channel,
    delta_stream_for,
    fetch_openai_json,
)

ANSWER = {
    "title": "제목",
    "summary": '첫 문장입니다. "따옴표"와 줄바꿈\n그리고 😀 끝.',
    "sections": [{"title": "summary", "bullets": ["bullet"]}],
    "citations": [],
    "limitations": [],
}


class FakeRedis:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.messages: list[tuple[str, dict]] = []

    def publish(self, channel, payload):
        if self.fail:
            raise ConnectionError("redis down")
        self.messages.append((channel, json.loads(payload)))


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def sse_lines(events: list[dict]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events).encode()


def stream_events(chunk_size: int = 3, text: str | None = None) -> list[dict]:
    body = text if text is not None else json.dumps(ANSWER, ensure_ascii=False)
    events = [{"type": "response.created"}]
    for i in range(0, len(body), chunk_size):
        events.append({"type": "response.output_text.delta", "delta": body[i : i + chunk_size]})
    events.append({"type": "response.output_text.done", "text": body})
    events.append({"type": "response.completed"})
    return events


def request() -> urllib.request.Request:
    return urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps({"model": "m", "input": "x"}).encode(),
        headers={"Authorization": "Bearer k", "Content-Type": "application/json"},
        method="POST",
    )


class SummaryDeltaExtractorTest(unittest.TestCase):
    def test_summary_is_reassembled_for_any_chunk_size(self):
        for ensure_ascii in (True, False):
            full = json.dumps(ANSWER, ensure_ascii=ensure_ascii)
            for size in (1, 2, 5, 11, 400):
                extractor = SummaryDeltaExtractor()
                out = "".join(extractor.feed(full[i : i + size]) for i in range(0, len(full), size))
                self.assertEqual(out, ANSWER["summary"], (ensure_ascii, size))
                self.assertTrue(extractor.finished)

    def test_text_after_summary_and_other_keys_named_summary_are_ignored(self):
        extractor = SummaryDeltaExtractor()
        out = extractor.feed(json.dumps(ANSWER, ensure_ascii=False))
        self.assertEqual(out, ANSWER["summary"])
        self.assertEqual(extractor.feed('"summary":"다시"'), "")

    def test_nothing_is_emitted_before_summary_starts(self):
        extractor = SummaryDeltaExtractor()
        self.assertEqual(extractor.feed('{"title":"제'), "")
        self.assertEqual(extractor.feed('목","summ'), "")


class FetchOpenAIJsonTest(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"AGENT_STREAM_SYNTHESIS_RESPONSE": "true", "AGENT_SYNTHESIS_STREAM_MAX_SECONDS": "60"})
        env.start()
        self.addCleanup(env.stop)

    def run_stream(self, events, redis=None, timing=None):
        redis = redis or FakeRedis()
        with patch.object(urllib.request, "urlopen", return_value=FakeResponse(sse_lines(events))) as opened:
            with delta_stream_for("analysis-1", redis_client=redis):
                result = fetch_openai_json(request(), timeout_seconds=5, timing=timing)
        return result, redis, opened

    def test_streamed_deltas_arrive_in_order_and_final_json_is_returned(self):
        timing: dict = {}
        result, redis, opened = self.run_stream(stream_events(), timing=timing)

        self.assertEqual(result, ANSWER)
        sent = json.loads(opened.call_args.args[0].data)
        self.assertIs(sent["stream"], True)
        channels = {channel for channel, _ in redis.messages}
        self.assertEqual(channels, {delta_channel("analysis-1")})
        seqs = [m["seq"] for _, m in redis.messages]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        self.assertEqual("".join(m["text"] for _, m in redis.messages), ANSWER["summary"])
        self.assertTrue(all(m["analysisId"] == "analysis-1" for _, m in redis.messages))
        self.assertTrue(timing["synthesisStreamed"])
        self.assertEqual(timing["synthesisDeltaCount"], len(seqs))
        self.assertIn("synthesisFirstDeltaMs", timing)

    def test_without_publisher_the_legacy_single_response_path_is_used(self):
        body = json.dumps({"output_text": json.dumps(ANSWER, ensure_ascii=False)}).encode()
        with patch.object(urllib.request, "urlopen", return_value=FakeResponse(body)) as opened:
            result = fetch_openai_json(request(), timeout_seconds=5)
        self.assertEqual(result, ANSWER)
        self.assertNotIn(b'"stream"', opened.call_args.args[0].data)

    def test_flag_off_disables_streaming(self):
        with patch.dict(os.environ, {"AGENT_STREAM_SYNTHESIS_RESPONSE": "false"}):
            with delta_stream_for("analysis-1", redis_client=FakeRedis()) as publisher:
                self.assertIsNone(publisher)
                self.assertIsNone(current_delta_publisher())

    def test_publisher_is_cleared_after_the_context_exits(self):
        with delta_stream_for("analysis-1", redis_client=FakeRedis()):
            self.assertIsNotNone(current_delta_publisher())
        self.assertIsNone(current_delta_publisher())

    def test_stream_failure_event_raises_so_the_caller_falls_back(self):
        events = stream_events()[:4] + [{"type": "response.failed"}]
        with self.assertRaises(RuntimeError):
            self.run_stream(events)

    def test_redis_publish_failure_does_not_break_the_answer(self):
        result, redis, _ = self.run_stream(stream_events(), redis=FakeRedis(fail=True))
        self.assertEqual(result, ANSWER)
        self.assertEqual(redis.messages, [])

    def test_total_time_cap_stops_a_stream_that_never_finishes(self):
        with patch.dict(os.environ, {"AGENT_SYNTHESIS_STREAM_MAX_SECONDS": "0"}):
            with self.assertRaises(TimeoutError):
                self.run_stream(stream_events())


@unittest.skipUnless(os.getenv("REDIS_URL") or os.getenv("TEST_REDIS_URL"), "real Redis is not configured")
class RealRedisRelayTest(unittest.TestCase):
    """실제 Redis에 publish한 델타가 같은 analysisId 채널 구독자에게만 닿는지 확인한다."""

    def test_deltas_reach_only_the_matching_analysis_channel(self):
        import redis

        url = os.getenv("TEST_REDIS_URL") or os.getenv("REDIS_URL")
        client = redis.from_url(url, decode_responses=True)
        try:
            client.ping()
        except Exception:
            self.skipTest("Redis is not reachable")
        a, b = f"t-{uuid.uuid4().hex[:8]}", f"t-{uuid.uuid4().hex[:8]}"
        sub_a, sub_b = client.pubsub(ignore_subscribe_messages=True), client.pubsub(ignore_subscribe_messages=True)
        sub_a.subscribe(delta_channel(a))
        sub_b.subscribe(delta_channel(b))
        DeltaPublisher(a, redis_client=client).publish_delta("A-only")

        def drain(sub):
            out = []
            for _ in range(5):
                message = sub.get_message(timeout=0.3)
                if message and message["type"] == "message":
                    out.append(json.loads(message["data"]))
            return out

        got_a, got_b = drain(sub_a), drain(sub_b)
        sub_a.close()
        sub_b.close()
        self.assertEqual([m["text"] for m in got_a], ["A-only"])
        self.assertEqual(got_b, [])


if __name__ == "__main__":
    unittest.main()
