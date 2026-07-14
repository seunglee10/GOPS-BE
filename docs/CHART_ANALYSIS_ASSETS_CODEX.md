# Chart Geometry Assets — Codex Reference

## 불변 조건

- 해설·질문 UI 통합 작업은 기존 `geometry_assets`의 read-only consumer다. 자산 build,
  FORCE 재생성, migration Job, 전체 universe 등록, Geometry CronJob 조작을 실행하지 않는다.
- 지원 interval은 `1m/5m/10m/1h/4h/1D/1W`뿐이다.
- 새 build 등록은 `1m/1D`만 허용한다. 기존 다른 interval 자산의 저장·조회·표시
  호환은 유지한다.
- 분석 입력은 정규장·분할조정·완료된 canonical candle뿐이다.
- `1W`는 canonical `1D`에서 기존 주봉 방식으로 생성한다.
- 모든 timed anchor는 현재 asset interval의 실제 candle timestamp에 속한다.
- `tradePlan`의 신호 anchor도 실제 완료 봉 timestamp여야 한다. 프런트가 만드는 임시
  `riskRewardBox`의 미래 끝점은 저장하지 않으며 timestamp 없이 logical index로만 투영한다.
- `indicators.cross.status=crossed`이면 프런트는 `previousIndex + fraction`의 실제
  SMA60/120 보간 교차점에 `flagMarker`를 만들고 Geometry 표시 상태를 따른다.
  `timestamp`는 확인 봉으로 보존하고 마커 가격은 asset의 `price`를 사용하며, 교차
  구간이 현재 candle 범위 밖이면 만들지 않는다.
- 지지·저항 `horizontalLine`은 첫·마지막 접촉의 동일 가격 2-anchor와 선택적
  `role/zoneLow/zoneHigh/halfWidthAtr`를 저장한다. 프런트 presentation은 유효한 zone만
  동일 ID의 `horizontalParallelLines` 밴드로 바꾸며 ATR 재계산·레벨 재병합을 하지 않는다.
  metadata가 부족한 기존 자산과 수동 단일-anchor 선은 원래 geometry를 유지한다.
- 작도 계약은 최대 8개다. 지지·저항은 최대 4개이며 최고 점수 패턴 하나는 경계선
  2개와 선택적 깃대 1개를 사용한다.
- 패턴 종류는 세 삼각형, 상승·하락 깃발형/페넌트/직사각형, 상승·하락 쐐기,
  하락 채널 상단 돌파, 상승 채널 하단 이탈이다. 채널 이탈은 `confirmed`만 hard-pass다.
- geometry 계산에는 LLM을 사용하지 않는다.
- `forming`은 `watch`, `confirmed`만 매매 후보이며 자동 주문으로 연결하지 않는다.
- 돌파 buffer는 `0.25 ATR`, 전술 stop은 돌파 경계에서 `1 ATR`, 최소 신규 진입
  손익비는 `2.0`, 기본 포지션 정책은 long-only다.
- chart asset payload/job은 PostgreSQL, candle은 ClickHouse에 저장한다.
- 결측 보충은 Alpaca의 정확한 누락 range만 사용하며 S3·Redis·Kafka를 거치지 않는다.
- 결측 source는 `5m/10m` target에 `1Min`, `1h/4h` target에 `10Min`을 사용하며,
  실시간 `1m` 기반 파생 계약은 바꾸지 않는다.
- 동일 `(symbol, interval, inputDigest, algorithmVersion)`은 no-op이다.
- 수동 request source/priority는 `manual/100`, 정기 request는 `scheduled/10`이며
  priority는 서버가 소유한다. 동일 활성 요청은 `request_fingerprint`로 합친다.
- item claim은 priority 내림차순이며 2회 시도 후 만료된 lease는 실패로 종결한다.
- 실제 완료 봉 120개 미만 또는 provider가 확인하지 못한 interior/tail gap이면 기존
  성공 자산을 덮어쓰지 않는다. 성공한 Alpaca 조회에도 실재 봉이 없는 slot은
  `provider_confirmed_empty`로 기록하고 가짜 봉 없이 분석을 계속한다.

## Coverage 계약

| Interval | Target | Warm-up | Evaluation | Minimum | Cross |
| --- | ---: | ---: | ---: | ---: | ---: |
| `1m`~`1D` | 380 | 120 | 260 | 120 | 121 |
| `1W` | 312 | 120 | 192 | 120 | 121 |

120봉은 SMA60/120 값을 계산할 수 있지만 직전 봉 비교가 없으므로 교차 상태는
`insufficient_previous_bar`다. SMA 기간은 달력 일수가 아니라 선택 interval의 완료
봉 개수다.

## 주요 코드 경계

- `alfaka.analytics.geometry`: OHLCV evidence, 수평선, SMA/교차와 Geometry 자산 조립
- `alfaka.analytics.pivots` + `alfaka.analytics.patterns`: 방향전환 피벗과 회귀형 패턴 탐지
- `alfaka.analytics.trade_timing`: 확인 상태를 진입·손절·목표·손익비 시나리오로 변환
- `alfaka.analytics.analysis_candles`: 완료 봉과 canonical identity, 기존 주봉 집계
- `alfaka.serving.session_buckets`: 09:30 ET 기준 intraday 버킷과 공통 OHLCV 집계
- `alfaka.analytics.analysis_repair`: ClickHouse audit와 Alpaca-only repair
- `gops_agents.chart_assets.builder`: symbol/interval 단위 조립과 digest no-op
- `gops_agents.chart_assets.storage`: PostgreSQL 최신 geometry 자산
- `gops_agents.chart_assets.job_store`: PostgreSQL queue, 15분 lease, 최대 2회 시도

새 payload의 `assetVersion`은 숫자 개발 단계가 아니라 기존 응답 union을 구분하는
semantic discriminator인 `geometry`다. `algorithmVersion`은 현재
`ohlcv-consensus-pattern-families-v4`이며 분석 의미가 바뀔 때만 변경한다. 범용
`patterns[]`/`primaryPattern`이 없는 기존 geometry row는 프런트가 `primaryTriangle`로
표시 호환하고, 다음 빌드에서 새 계약으로 교체한다. 기존 숫자형 자산 row는 읽기
fallback이나 자동 변환에 사용하지 않는다.

`GET /api/charts/analysis-assets/coverage`의 각 `symbol + interval` 항목은 대표 패턴의
`kind/state/score`만 담은 `primaryPattern` 요약을 포함한다. 저장 payload에 범용
`primaryPattern`이 없으면 `primaryTriangle`을 사용하고, 둘 다 없으면 `null`이다.

Intraday candle input contract는 `regular-session-derived`이며 asset digest에 포함된다.
미국 주식 `5m/10m/1h/4h`는 `bucket_policy=us_equity_regular_session`인 ClickHouse
행만 사용한다. 과거 `clock_aligned` 행과 섞지 않는다.

PostgreSQL 테이블은 `geometry_assets`, `geometry_build_jobs`,
`geometry_build_items`다. 자산 기본 키는 `(symbol, interval)`이고 item claim은
`FOR UPDATE SKIP LOCKED`를 사용한다. 활성 request fingerprint에는 partial unique
index를 사용한다. 기존 설치는 명시적 migration Job을 재실행해
`geometry_assets_drawing_count_check`와 queue index를 갱신해야 한다.

## 검증

```sh
.venv/bin/python -m pytest systems/market-data/tests/analytics/test_geometry_assets.py
.venv/bin/python -m pytest systems/market-data/tests/analytics/test_patterns.py
.venv/bin/python -m pytest systems/market-data/tests/analytics/test_trade_timing.py
.venv/bin/python -m pytest systems/agent-orchestration/tests/test_geometry_asset_contract.py
.venv/bin/python -m pytest systems/api-server/tests/test_chart_assets_routes.py
```

프론트는 `chart-asset:` 근거와 `chart-plan:` 제안을 차트별 `작도`, `제안` 토글로
독립 제어하고, 현재 interval의 자산만 적용하며 SMA60·SMA120 overlay를 함께
활성화한다. 레이어가 없을 때만 해당 토글을 비활성화하고 수동 drawing은 보존한다.
빌드 패널은 `1m/1D`만 제공하고 둘 다 기본 선택한다.

`DrawingStyle.labelPlacement`는 `inline | axis | none`, `zoneSplit`은 boolean이다.
두 값이 없으면 기존 수동 작도의 label/geometry를 보존한다. 시스템 밴드는 중앙 가격
pill 하나, 패턴은 마지막 anchor 가격 pill을 사용한다. `zoneSplit:true` trade plan은
확인 봉부터 진입 점선을 그리고 마지막 완료 봉 다음 슬롯부터 위험·보상 fill과
목표·손절선을 그린다. 미래 끝점은 `last candle index + projectionBars`의 logical index이며
timestamp를 만들지 않는다.

완전한 신규 long/short 후보만 차트 문서별 비영속 `ActiveTradePlan` registry에 저장한다.
`sell_candidate`, `watch`, `no_trade`는 active plan을 만들지 않는다. registry event는
`gops:trade-plan-updated`와 `{ chartDocumentId, plan }` detail을 사용하고 clear는
`plan:null`이다. 이 projection은 해설·spotlight용이며 주문·알림 계약으로 사용하지 않는다.
