"""합성 LLM 응답의 서술 문단(summary)을 SSE delta 이벤트로 중계하는 도구 (이슈 #43).

흐름: 워커가 delta_stream_for(analysisId)로 발행기를 켠다 → 합성 코드가 OpenAI를 stream:true로 부르고
      summary 문장이 생기는 대로 Redis `agent.reports.delta:{analysisId}`에 publish → API 서버 SSE 라우트가
      `event: delta`로 클라이언트에 전달한다. 구조화 JSON 전체는 기존처럼 완료 시 `report` 이벤트로 간다.
발행기가 꺼져 있으면(테스트·배치·플래그 off) 기존의 한 번에 받는 호출 경로가 그대로 쓰인다.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import time
import urllib.request
from collections.abc import Iterator
from typing import Any

from ..security import sanitize_text

DEFAULT_DELTA_CHANNEL = "agent.reports.delta"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
_SUMMARY_KEY = re.compile(r'"summary"\s*:\s*"')
_TRUE_VALUES = {"1", "true", "yes", "on"}
_MAX_LINE_BYTES = 1 << 20
_HOLD_BACK_CHARS = 64  # 정화 패턴(이메일·전화번호·비밀값)이 완성될 때까지 보류하는 글자 수


def delta_channel(analysis_id: str, base: str | None = None) -> str:
    return f"{base or os.getenv('AGENT_REPORT_DELTA_CHANNEL', DEFAULT_DELTA_CHANNEL)}:{analysis_id}"


def delta_stream_enabled() -> bool:
    # contracts의 stream_synthesis_response 정책 필드와 같은 환경 변수를 스위치로 쓴다.
    return os.getenv("AGENT_STREAM_SYNTHESIS_RESPONSE", "true").strip().lower() in _TRUE_VALUES


class DeltaPublisher:
    """델타를 Redis pubsub으로 보낸다. 발행 실패는 삼킨다: 스트리밍이 답변 생성을 깨뜨리면 안 된다."""

    def __init__(self, analysis_id: str, redis_client: Any | None = None):
        self.analysis_id = analysis_id
        self._redis = redis_client
        self._seq = 0
        self._raw = ""
        self._released = ""
        self._poisoned = False
        self._channel = delta_channel(analysis_id)

    @property
    def published(self) -> int:
        return self._seq

    def _client(self) -> Any | None:
        if self._redis is not None:
            return self._redis
        url = os.getenv("REDIS_URL")
        if not url:
            return None
        try:
            import redis

            self._redis = redis.from_url(url, decode_responses=True)
        except Exception:
            return None
        return self._redis

    def feed(self, text: str) -> None:
        """summary 조각을 받아, 정화한 전체 텍스트 중 '안전한 앞부분'만 내보낸다.

        최종 답변은 sanitize_final_answer를 거치므로 델타도 같은 개인정보·비밀값·비속어 정화를 거쳐야 한다.
        구간을 따로 정화하면 경계에 걸친 패턴(공백이 낀 전화번호 등)을 놓치므로, 지금까지 받은 전체를
        매번 정화하고 끝의 _HOLD_BACK_CHARS 글자는 보류한다. 패턴은 보류 구간 안에서 완성되어 정화된다.
        정화로 앞부분이 바뀌어 이미 보낸 글과 어긋나면 델타를 멈춘다(최종 report가 전체를 교체한다).
        """
        if self._poisoned or not text:
            return
        self._raw += text
        self._release(final=False)

    def flush(self) -> None:
        if not self._poisoned:
            self._release(final=True)

    def _release(self, *, final: bool) -> None:
        sanitized = str(sanitize_text(self._raw).value)
        if not sanitized.startswith(self._released):
            self._poisoned = True
            return
        end = len(sanitized) if final else len(sanitized) - _HOLD_BACK_CHARS
        if not final and end > len(self._released):
            space = sanitized.rfind(" ", len(self._released), end + 1)
            end = space + 1 if space >= 0 else len(self._released)
        if end <= len(self._released):
            return
        piece = sanitized[len(self._released) : end]
        self._released = sanitized[:end]
        self.publish_delta(piece)

    def publish_delta(self, text: str) -> None:
        if not text:
            return
        client = self._client()
        if client is None:
            return
        self._seq += 1
        payload = json.dumps(
            {"analysisId": self.analysis_id, "seq": self._seq, "text": text},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            client.publish(self._channel, payload)
        except Exception:
            return


_current: contextvars.ContextVar[DeltaPublisher | None] = contextvars.ContextVar("agent_delta_publisher", default=None)


@contextlib.contextmanager
def delta_stream_for(analysis_id: str, *, redis_client: Any | None = None) -> Iterator[DeltaPublisher | None]:
    if not analysis_id or not delta_stream_enabled():
        yield None
        return
    publisher = DeltaPublisher(analysis_id, redis_client=redis_client)
    token = _current.set(publisher)
    try:
        yield publisher
    finally:
        _current.reset(token)


def current_delta_publisher() -> DeltaPublisher | None:
    return _current.get()


class SummaryDeltaExtractor:
    """스트리밍 중인 JSON 텍스트에서 최상위 `summary` 문자열 값만 증분으로 꺼낸다.

    청크가 JSON 이스케이프(\\n, \\uXXXX, 서로게이트 쌍)의 중간에서 끊겨도 완성된 글자만 내보낸다.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._pos: int | None = None  # summary 여는 따옴표 다음 위치
        self.finished = False

    def feed(self, chunk: str) -> str:
        if self.finished or not chunk:
            return ""
        self._buffer += chunk
        if self._pos is None:
            match = _SUMMARY_KEY.search(self._buffer)
            if match is None:
                return ""
            self._pos = match.end()
        out: list[str] = []
        buf, i = self._buffer, self._pos
        while i < len(buf):
            ch = buf[i]
            if ch == '"':
                self.finished = True
                i += 1
                break
            if ch != "\\":
                out.append(ch)
                i += 1
                continue
            if i + 1 >= len(buf):
                break
            esc = buf[i + 1]
            if esc == "u":
                if i + 6 > len(buf):
                    break
                code = int(buf[i + 2 : i + 6], 16) if re.fullmatch(r"[0-9a-fA-F]{4}", buf[i + 2 : i + 6]) else None
                if code is None:
                    i += 2
                    continue
                if 0xD800 <= code <= 0xDBFF:
                    if i + 12 > len(buf):
                        break
                    low_text = buf[i + 6 : i + 12]
                    if low_text.startswith("\\u") and re.fullmatch(r"[0-9a-fA-F]{4}", low_text[2:]):
                        low = int(low_text[2:], 16)
                        if 0xDC00 <= low <= 0xDFFF:
                            out.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
                            i += 12
                            continue
                    i += 6
                    continue
                out.append(chr(code))
                i += 6
                continue
            mapping = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", '"': '"', "\\": "\\", "/": "/"}
            out.append(mapping.get(esc, esc))
            i += 2
        self._pos = i
        return "".join(out)


def _stream_request(request: urllib.request.Request) -> urllib.request.Request:
    payload = json.loads(request.data.decode("utf-8")) if request.data else {}
    payload["stream"] = True
    return urllib.request.Request(
        request.full_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=dict(request.header_items()),
        method="POST",
    )


def fetch_openai_json(
    request: urllib.request.Request,
    *,
    timeout_seconds: float,
    timing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """OpenAI /v1/responses 호출 결과(JSON 본문 텍스트를 파싱한 dict)를 돌려준다.

    델타 발행기가 켜져 있으면 stream:true로 호출해 summary 문장을 생기는 대로 중계한다.
    꺼져 있으면 기존과 같이 응답 전체를 한 번에 읽는다.

    타임아웃 의미(스트리밍): timeout_seconds는 소켓 읽기 한 번의 대기 한도, 즉 첫 토큰·토큰 간 정지 한도다.
    생성이 이어지는 동안 총 시간은 AGENT_SYNTHESIS_STREAM_MAX_SECONDS(기본 60초)로 제한한다.
    """
    from ..orchestration.routing import parse_openai_text_json

    publisher = current_delta_publisher()
    if publisher is None:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return parse_openai_text_json(json.loads(response.read().decode("utf-8")))

    max_seconds = float(os.getenv("AGENT_SYNTHESIS_STREAM_MAX_SECONDS", "60"))
    started = time.perf_counter()
    extractor = SummaryDeltaExtractor()
    text_parts: list[str] = []
    final_text: str | None = None
    first_delta_at: float | None = None
    with urllib.request.urlopen(_stream_request(request), timeout=timeout_seconds) as response:
        event_name = ""
        while True:
            if time.perf_counter() - started > max_seconds:
                raise TimeoutError("synthesis stream exceeded max seconds")
            raw = response.readline(_MAX_LINE_BYTES)  # 한 줄 길이도 제한한다
            if not raw:
                break
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith("event:"):
                event_name = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            kind = event.get("type") or event_name
            if kind == "response.output_text.delta":
                piece = str(event.get("delta") or "")
                if first_delta_at is None:
                    first_delta_at = time.perf_counter()
                text_parts.append(piece)
                publisher.feed(extractor.feed(piece))
            elif kind == "response.output_text.done" and isinstance(event.get("text"), str):
                final_text = event["text"]
            elif kind in {"response.failed", "response.incomplete", "error"}:
                raise RuntimeError(f"openai stream {kind}")
    publisher.flush()
    if isinstance(timing, dict):
        timing["synthesisStreamed"] = True
        timing["synthesisDeltaCount"] = publisher.published
        if first_delta_at is not None:
            timing["synthesisFirstDeltaMs"] = round((first_delta_at - started) * 1000, 3)
    text = final_text if final_text is not None else "".join(text_parts)
    return json.loads(text) if text.strip() else {}
