# Order-Flow / Bid-Ask 안정화 계획 (Codex Goal-Mode 참조 세트)

이 폴더는 Bid/Ask 차트·오더플로우 데이터 경로의 4개 워크스트림 구현 계획이다.
Codex는 각 문서를 독립 Goal로 실행하되, 이 README의 실행 순서와 결합 규칙을 먼저 읽는다.
`AGENTS.md`와 `docs/CHART_DATA_REBUILD_PLAN.md`의 기존 계약(API route, Kafka 토픽, DB
스키마 유지)은 이 계획 전체에서 그대로 유효하다.

## 결정 기록 (2026-07-10, 사용자 확정 — 2차 개정)

| 주제 | 결정 |
| --- | --- |
| Bid/Ask 차트 범위 | **오늘 하루만 그린다.** 과거 세션은 표시하지 않으며, 과거 데이터 확보(백필) 계획은 폐기한다. |
| Bid/Ask 차트 형태 | 당일을 인터벌로 펼치는 인트라데이 footprint: **1m/10m/1h, 기본 10m.** 1D 일별 나열 형태는 폐기. |
| Redis 저장 모델 | 원본·전 기간 누적이 아니라 **계산된 데이터만, 캔들처럼 갱신**: 마감 분 버킷은 불변 블롭 append, 진행 분은 덮어쓰기. |
| EOD 롤업 | **크론잡은 유지**(NVDA 편향 검증의 기준선). 프론트의 daily 호출은 제거, `/api/charts/order-flow/daily`는 deprecated(동작은 유지). |
| NVDA 오더플로우 편향 | 버그로 단정하지 않는다. 검증 체계로 판별 후 수리 여부 결정 (1차 결정 유지). |
| Redis | 과부하 해소가 최우선 목표. 핀 심볼 `NVDA,AMZN,MU,AAPL,GOOGL` 5종목 유지 (1차 결정 유지). |

1차 결정 중 "표시 일수 24→10일 축소"와 "EOD 백필(전략 1)"은 이 개정으로 폐기되었다.

## 문서 구성과 실행 순서

```text
Phase 0  진단 (코드 변경 없음)
  04 §1  Redis commandstats 스냅샷 (부하 baseline)

Phase 1  병렬 구현
  02     오더플로우 Redis 저장 모델 전환 (backend: processor + api provider)
  04     Redis 핫패스 경량화 (backend: quote/health/pub-sub)
  01     Bid/Ask 차트 인트라데이 전환 (frontend — 02와 API 계약이 동일하므로 병렬 가능)

Phase 2  집계 검증
  03     라이브 vs as-of 재계산 비교 진단 → 판정에 따라 후속 수리 여부 결정
```

| 파일 | 내용 |
| --- | --- |
| `01-bidask-intraday-chart.md` | 프론트엔드: bidask를 당일 인트라데이 차트(1m/10m/1h)로 전환, daily 의존 제거 |
| `02-orderflow-redis-storage-model.md` | 백엔드: 분×빈 해시 누적 → "마감 분 블롭 append + 진행 분 덮어쓰기" 모델 전환 |
| `03-aggregation-verification.md` | 진단: NVDA 편향이 라이브 분류 아티팩트인지 시장 특성인지 판별 |
| `04-redis-lean-strategy.md` | 백엔드: quote/trade 핫패스의 Redis 명령 수 감축 (§6은 02로 대체됨) |

## 워크스트림 간 결합 규칙

- **02와 01은 API 계약으로 분리된다.** `GET /api/charts/order-flow/intraday`의 응답
  형태(`minutes[]` + `liveQuote`)와 WS `ORDER_FLOW_BINS_UPDATE` 페이로드는 두 문서
  모두에서 변경 금지. 따라서 백엔드 저장 전환과 프론트 차트 전환은 어느 쪽이 먼저
  배포되어도 동작한다.
- **04와 03은 결합된다.** `live:quote:{symbol}` 쓰기 스로틀을 150ms 이상으로 올리는
  것은 04 §5(핀 심볼 인메모리 NBBO) 적용 후에만 허용. 그 전에는 기본 100ms 이하.
- **03의 결과가 라이브 분류 수리 여부를 정한다.** as-of 재계산과 라이브 bins가
  유의미하게 갈라질 때만 04 §5의 인메모리 NBBO를 as-of 버퍼로 확장한다. EOD 롤업
  크론잡은 이 검증의 기준선이므로 검증 종결 전에는 제거하지 않는다.

## 전 워크스트림 공통 검증

`docs/CHART_DATA_REBUILD_PLAN.md`의 Validation 순서를 따른다:

```text
git diff --check
.venv/bin/python -m unittest discover -s systems/market-data/tests
.venv/bin/python -m unittest discover -s systems/api-server/tests -p 'test_market_data_query.py'
apps/gops-frontend: tsc -b && vite build && node scripts/run-chart-tests.mjs
docker compose config && docker compose build
```

## 건드리지 않는 것 (전 문서 공통 가드레일)

- API route 계약(`/api/charts/order-flow/*` 경로·응답 형태), Kafka 토픽/파티션 계약,
  ClickHouse 스키마. deprecated 처리도 route 제거가 아니라 문서·프론트 미사용 처리다.
- `ORDER_FLOW_PINNED_SYMBOLS` 5종목 구성.
- EOD 롤업 크론잡과 as-of join 분류 로직(`alfaka/orderflow/classification.py`) —
  03의 판정 전에는 수정 금지.
- 오더플로우 패널(`OrderFlowPanel.tsx`)의 기존 UX — 이번 범위는 차트 패널의 bidask
  chartType이다.
- 로컬 런타임에서 가짜 캔들 생성 금지 (AGENTS.md).
