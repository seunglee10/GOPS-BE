#!/usr/bin/env python3
"""agent_sse_latency.py 결과 JSON을 합성 LLM 경로 기준으로 다시 집계한다 (이슈 #43).

왜 필요한가: 일부 질문은 근거 부족으로 합성 LLM 단계까지 가지 않고 기본 답변("추가 확인 필요")으로 끝난다.
그런 실행은 스트리밍과 무관하므로, 전후 비교는 `financial-synthesis` 호출이 있었던 실행만 모아서 한다.

사용: python scripts/benchmarks/summarize_agent_sse_latency.py a.json b.json ...
"""
from __future__ import annotations

import json
import statistics
import sys


def stats(values: list[float]) -> str:
    if not values:
        return "측정 못 함"
    return f"{statistics.fmean(values):.0f} / {statistics.median(values):.0f} / {max(values):.0f} (n={len(values)})"


def main(paths: list[str]) -> int:
    for path in paths:
        data = json.load(open(path, encoding="utf-8"))
        ok = [r for r in data["runs"] if r["ok"] and r["cacheHit"] is not True and r["financialCacheHit"] is not True]
        synth = [r for r in ok if "financial-synthesis" in (r.get("llmLabels") or [])]
        fallback = [r for r in synth if r.get("synthesisFallback")]
        print(f"== {data['label']}  (전체 {len(data['runs'])}회, 정상 {len(ok)}회, 합성 경로 {len(synth)}회, 합성 폴백 {len(fallback)}회)")
        print("   첫 글자 평균/중앙값/최대 ms :", stats([r["firstTextMs"] for r in synth if r["firstTextMs"] is not None]))
        print("   답변 완성 평균/중앙값/최대 ms:", stats([r["completeMs"] for r in synth if r["completeMs"] is not None]))
        done = [r for r in synth if not r.get("synthesisFallback")]
        if fallback:
            print("   (폴백 제외) 첫 글자 ms      :", stats([r["firstTextMs"] for r in done if r["firstTextMs"] is not None]))
            print("   (폴백 제외) 완성 ms         :", stats([r["completeMs"] for r in done if r["completeMs"] is not None]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
