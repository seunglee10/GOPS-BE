# AI 답변 SSE 지연 측정 (이슈 #43)

측정 스크립트: `scripts/benchmarks/agent_sse_latency.py`

## 공통 조건
- 환경: 로컬 Docker (Kafka·Redis·ClickHouse·백엔드·분석 워커·전달 게이트웨이), 실제 OpenAI `gpt-5.2` 호출
- 근거 데이터: 로컬에 적재한 SEC 공시 (AAPL, MSFT, NVDA)
- 질문: 유형 3개(수익성·재무 구조·현금흐름) × 종목 3개 = 9개, 각 5회 = 45회
- 캐시: 요청마다 `gops:agent:analysis:*`, `gops:agent:financial-final-answer:*`를 비워 미적중 경로만 측정
- 시각 기준: 클라이언트가 요청을 보낸 시각부터 SSE 이벤트가 도착한 시각 (`time.perf_counter`)
- "첫 글자" = `delta` 이벤트가 있으면 첫 delta, 없으면 본문 요약이 들어 있는 첫 `report`

## baseline (구현 전, 측정 스크립트 커밋 2a6b942e)
| 조건 | 합성 타임아웃 | 폴백 | 첫 글자 평균/중앙값/최대 (ms) | 완성 평균/중앙값/최대 (ms) |
|---|---|---|---|---|
| before-5s | 5초 (운영 설정) | 25/45 | 4592 / 5395 / 6146 (n=40) | 4243 / 5392 / 6146 (n=45) |
| before-120s | 120초 | 0/45 | 12469 / 15929 / 21210 (n=40) | 11243 / 14621 / 21210 (n=45) |

- 첫 글자 n=40인 것은 9개 질문 중 1개(NVDA 현금흐름)의 5회에는 요약 본문이 없어서다.
- before-5s에서 "합성 LLM 답"만 모은 값은 JSON의 `*_llmAnswerOnly`에 있다.
