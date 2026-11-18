#!/usr/bin/env python3
"""AI 답변 SSE 지연 측정 스크립트 (이슈 #43).

역할: POST /api/agents/analyze 후 SSE(/stream)를 읽으며 클라이언트 쪽 시각만으로
      "첫 글자 표시까지"와 "답변 완성까지"를 잰다. 서버 로그·내부 타이밍은 참고값으로만 쓴다.
조건: 요청마다 답변 캐시를 비워 캐시 미적중 경로만 잰다. 질문 유형별 3개 이상, 각 N회 반복한다.
사용:
    python scripts/benchmarks/agent_sse_latency.py --label before-5s --repeat 5 --out result.json

"첫 글자"의 정의
    - delta 이벤트가 있으면 첫 delta가 도착한 시각 (구현 후)
    - delta가 없으면 finalAnswer 요약 문장이 들어 있는 첫 report 이벤트가 도착한 시각 (구현 전)
    비교 가능하도록 두 경우 모두 "사용자 화면에 본문 글자가 처음 나올 수 있는 시각"이다.
측정값을 지어내지 않는다: 실패·타임아웃·폴백 여부는 모두 기록하고, 통계에서는 따로 센다.
표준 라이브러리만 쓴다.
"""
from __future__ import annotations

import argparse
import http.client
import json
import statistics
import subprocess
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass, field

# 질문 유형별 3개(종목 3개). 근거 데이터는 로컬에 적재한 SEC 공시(AAPL, MSFT, NVDA)다.
QUESTIONS: dict[str, list[tuple[str, str]]] = {
    "profitability": [
        ("AAPL", "AAPL 최근 분기 매출과 영업이익률 재무 요약해줘"),
        ("MSFT", "MSFT 최근 매출 성장과 영업이익률은 어때?"),
        ("NVDA", "NVDA 매출과 순이익 흐름을 재무 기준으로 정리해줘"),
    ],
    "balance_sheet": [
        ("AAPL", "AAPL 재무 건전성과 부채 수준은 어때?"),
        ("MSFT", "MSFT 자산과 부채, 자기자본 구조를 설명해줘"),
        ("NVDA", "NVDA 재무 건전성은 어때?"),
    ],
    "cash_flow": [
        ("AAPL", "AAPL 영업현금흐름과 투자 지출을 요약해줘"),
        ("MSFT", "MSFT 현금흐름은 안정적이야?"),
        ("NVDA", "NVDA 현금흐름과 자본 지출 흐름을 정리해줘"),
    ],
}
CACHE_PATTERNS = ("gops:agent:analysis:*", "gops:agent:financial-final-answer:*")


@dataclass
class Run:
    questionType: str
    symbol: str
    question: str
    analysisId: str | None = None
    ok: bool = False
    error: str | None = None
    firstEventMs: float | None = None
    firstTextMs: float | None = None
    firstTextSource: str | None = None  # "delta" | "report"
    completeMs: float | None = None
    deltaCount: int = 0
    synthesisFallback: str | None = None
    serverTotalMs: float | None = None
    cacheHit: bool | None = None
    financialCacheHit: bool | None = None
    llmLabels: list[str] = field(default_factory=list)


def flush_caches(redis_container: str) -> None:
    for pattern in CACHE_PATTERNS:
        scan = subprocess.run(
            ["docker", "exec", redis_container, "redis-cli", "--scan", "--pattern", pattern],
            capture_output=True, text=True, check=True,
        )
        keys = [k for k in scan.stdout.split() if k]
        if keys:
            subprocess.run(["docker", "exec", redis_container, "redis-cli", "DEL", *keys], capture_output=True, check=True)


def has_text(report: dict) -> bool:
    final = report.get("finalAnswer") or {}
    return bool(str(final.get("summary") or "").strip())


def one_run(base: urllib.parse.ParseResult, qtype: str, symbol: str, question: str, timeout_s: float) -> Run:
    run = Run(questionType=qtype, symbol=symbol, question=question)
    body = json.dumps({
        "symbol": symbol, "intent": "analysis", "responseMode": "stream",
        "messages": [{"role": "user", "content": question}],
    }).encode()
    conn_cls = http.client.HTTPSConnection if base.scheme == "https" else http.client.HTTPConnection
    started = time.perf_counter()
    try:
        post = conn_cls(base.netloc, timeout=timeout_s)
        post.request("POST", "/api/agents/analyze", body=body, headers={"Content-Type": "application/json"})
        resp = post.getresponse()
        ack = json.loads(resp.read().decode())
        post.close()
        run.analysisId = ack.get("analysisId")
        if not run.analysisId:
            run.error = f"no analysisId: HTTP {resp.status}"
            return run

        stream = conn_cls(base.netloc, timeout=timeout_s)
        stream.request("GET", f"/api/agents/reports/{run.analysisId}/stream", headers={"Accept": "text/event-stream"})
        sresp = stream.getresponse()
        event, data_lines = None, []
        while True:
            raw = sresp.readline()
            if not raw:
                break
            now = (time.perf_counter() - started) * 1000
            line = raw.decode("utf-8").rstrip("\n")
            if line.startswith(":"):
                continue  # 하트비트 주석
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif line == "" and event:
                try:
                    payload = json.loads("\n".join(data_lines)) if data_lines else {}
                except json.JSONDecodeError:
                    payload = {}
                if run.firstEventMs is None:
                    run.firstEventMs = now
                if event == "delta":
                    run.deltaCount += 1
                    if run.firstTextMs is None:
                        run.firstTextMs, run.firstTextSource = now, "delta"
                elif event == "report":
                    if run.firstTextMs is None and has_text(payload):
                        run.firstTextMs, run.firstTextSource = now, "report"
                    if payload.get("status") in {"completed", "failed", "deep_completed", "canceled"}:
                        run.completeMs = now
                        timing = payload.get("timing") or {}
                        run.synthesisFallback = timing.get("synthesisFallbackReason")
                        run.serverTotalMs = timing.get("totalMs")
                        run.cacheHit = timing.get("cacheHit")
                        run.financialCacheHit = timing.get("financialFinalAnswerCacheHit")
                        run.llmLabels = list(timing.get("llmCallLabels") or [])
                        run.ok = payload.get("status") == "completed"
                        if not run.ok:
                            run.error = f"status={payload.get('status')}"
                        break
                elif event in {"timeout", "error"}:
                    run.error = f"sse {event}"
                    break
                event, data_lines = None, []
        stream.close()
    except Exception as exc:  # 측정 실패도 기록한다
        run.error = f"{exc.__class__.__name__}: {exc}"
    return run


def summarize(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"n": 0, "mean": None, "median": None, "max": None}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 1),
        "median": round(statistics.median(values), 1),
        "max": round(max(values), 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--label", required=True, help="조건 이름 (예: before-5s, before-120s, after-5s)")
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=180.0, help="요청당 클라이언트 타임아웃(초)")
    parser.add_argument("--redis-container", default="alfaka-redis")
    parser.add_argument("--keep-cache", action="store_true", help="캐시를 비우지 않는다 (미적중 측정에는 쓰지 말 것)")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    base = urllib.parse.urlparse(args.base_url)
    runs: list[Run] = []
    total = sum(len(v) for v in QUESTIONS.values()) * args.repeat
    for rep in range(args.repeat):
        for qtype, items in QUESTIONS.items():
            for symbol, question in items:
                if not args.keep_cache:
                    flush_caches(args.redis_container)
                run = one_run(base, qtype, symbol, question, args.timeout)
                runs.append(run)
                print(f"[{len(runs)}/{total}] {qtype}/{symbol} first={run.firstTextMs and round(run.firstTextMs)}ms "
                      f"done={run.completeMs and round(run.completeMs)}ms deltas={run.deltaCount} "
                      f"fallback={run.synthesisFallback} err={run.error}", flush=True)

    valid = [r for r in runs if r.ok and r.cacheHit is not True and r.financialCacheHit is not True]
    llm_done = [r for r in valid if not r.synthesisFallback]  # 합성 LLM 답이 실제로 쓰인 실행
    result = {
        "label": args.label,
        "baseUrl": args.base_url,
        "repeat": args.repeat,
        "totalRuns": len(runs),
        "okRuns": len(valid),
        "cacheHitOrFailedRuns": len(runs) - len(valid),
        "synthesisFallbackRuns": len(valid) - len(llm_done),
        "firstTextMs_allOk": summarize([r.firstTextMs for r in valid if r.firstTextMs is not None]),
        "completeMs_allOk": summarize([r.completeMs for r in valid if r.completeMs is not None]),
        "firstTextMs_llmAnswerOnly": summarize([r.firstTextMs for r in llm_done if r.firstTextMs is not None]),
        "completeMs_llmAnswerOnly": summarize([r.completeMs for r in llm_done if r.completeMs is not None]),
        "runs": [asdict(r) for r in runs],
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in result.items() if k != "runs"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
