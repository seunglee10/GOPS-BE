# Chart Analysis Assets v2 구현 노트

## 문서·코드 조정

- `docs/CHART_AGENT_STRATEGY.md`의 장기 방향에는 오케스트레이터 통합과 레거시 제거가 있으나, 이번 v2 스펙의 명시적 범위에 따라 독립 chart-asset builder를 유지하고 workflow/role/provider 및 레거시 경로를 수정하지 않는다.
- 기존 자산 계약은 단일 v1 schema였다. rollout 호환을 위해 원본을 `chart-analysis-asset-v1.schema.json`으로 보존하고 canonical schema를 v1/v2 union으로 전환한다.
- 기존 worker는 주기마다 별도 query/aggregation을 수행했다. v2는 shared `analysis_candles.py`에서 canonical 1D를 한 번 읽고 1D/1W/1M을 생성하는 경계로 이동한다.
- 기존 serving merge는 timestamp와 호출 순서에 의존했다. v2 primary identity인 candle key와 명시적 source/revision/hash winner를 shared pure function으로 도입하고 기존 route shape는 유지한다.

## 검증·성능 기록

- 묶음 A: Python 3.12.13. `test_analysis_*.py` 8건, market-data 전체 374건(6 skipped), chart-data contract 검사를 통과했다. serving daily live/closed precedence 회귀 1건을 발견해 source class를 명시적으로 분류한 뒤 전체 회귀를 재통과했다.
- 묶음 B: kernel version을 `kernel-v2`로 올리고 interval 설정을 `analytics/config.py` 한 곳에 모았다. pre-seed ATR, tactical/structural pivot과 prominence, bounded zone/touch episode/state, event episode, 3-touch/current-relevance trend, dual-boundary channel, 독립 range를 구현했다. 기존 “range는 항상 존재” 테스트는 v2 정상 무작도 계약과 충돌해 제거하고, 두 점/현재 무관 추세선 억제 테스트로 교체했다. market-data 375건(6 skipped), agent-orchestration 259건을 통과했다.
