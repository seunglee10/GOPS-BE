# GOPS Tick Replay Simulator

저장된 실제 Alpaca trade/quote를 원본 타임스탬프와 고정 수집 순서대로 재생한다.
전쟁 시나리오, 합성 뉴스, 가격 랜덤화, 현재 시각으로의 타임스탬프 변경은 사용하지 않는다.

## 고정 데이터셋

- ID: `sp500-top20-plus-amd-mu-20260715-kst-v2`
- 시간: KST `[2026-07-15 00:00, 2026-07-16 00:00)`
- UTC: `[2026-07-14 15:00, 2026-07-15 15:00)`
- 시총 기준일: `2026-06-30`
- 기존 20개 기업에 AMD·Micron을 더한 22개 기업·23개 티커: `NVDA, MSFT, AAPL, AMZN, META, AVGO, GOOGL, GOOG, BRK.B, TSLA, LLY, JPM, WMT, V, ORCL, MA, NFLX, XOM, COST, HD, JNJ, AMD, MU`
- feed: `SIP 15:00–00:00 UTC`, `BOATS 00:00–08:00 UTC`, `SIP 08:00–15:00 UTC`

원본 gzip JSONL은
`s3://$S3_BUCKET/simulator/replay/v2/dataset=sp500-top20-plus-amd-mu-20260715-kst/`에 두고,
ClickHouse의 TTL 없는 `simulation_replay_events`와
`simulation_replay_candles_1m`에 별도로 적재한다. 실시간 `trade_ticks`와
`quote_ticks`에는 넣지 않는다.

```sh
PYTHONPATH=systems/simulator .venv/bin/python -m gops_simul.tools.import_alpaca \
  --fixed-dataset \
  --clickhouse-url "$CLICKHOUSE_URL"
```

importer는 모든 페이지를 끝까지 수집하고 반개구간 필터, 파일 SHA-256·크기,
종목별 trade/quote 수, S3 metadata, ClickHouse 건수를 검증한다. 하나라도 맞지 않으면
manifest와 ClickHouse 상태를 `FAILED`로 남기며 시뮬레이터는 실행되지 않는다.

## 재생과 공통 paper 원장

일반 AWS app 배포가 simulator Pod와 backend 연결을 함께 준비한다. 새 Pod의 기본 상태는
`LIVE/idle`이다.
화면 상단의 플레이 버튼을 누르면 `start` action이 가상시각 `2026-07-15 00:00 KST`의
새 `runId`를 만들고 같은 응답에서 `running`으로 전환한다. 사용자 계좌는
Postgres의 영구 paper 원장을 계속 사용한다. 기본 배속은 `1×`이며 `1·5·20·60×`를
실행 중 바꿀 수 있다. 처리량이 부족하면 가상시계가 늦어질 뿐 틱은 버리지 않는다.
ClickHouse 청크 조회와 이벤트 처리는 HTTP 이벤트 루프 밖의 작업 스레드에서 수행하고,
status와 health는 마지막 완료 스냅샷을 즉시 반환한다. 따라서 큰 청크를 처리하는 동안에도
Kubernetes probe와 웹의 SIM 상태 폴링이 차단되지 않는다.
재생 pump는 ClickHouse 정렬키 `(dataset_id, sequence)`로 다음 청크를 미리 읽고 가상시각을
넘는 첫 이벤트부터 메모리에 보류한다. 같은 가상시각을 기다리는 동안 `event_time` 조건으로
전체 파티션을 반복 스캔하지 않는다. 현재 호가는 처리 완료 뒤 불변 snapshot으로 발행하므로
계좌 평가가 replay pump의 controller lock을 기다리지 않는다.

시장가는 실행 중 현재 ask(매수) 또는 bid(매도), 지정가는 조건 충족 시 실제 ask/bid로
정수 수량 전량 체결된다. 공매도·부분체결·수수료·추가 슬리피지는 없다. 미체결 매수
현금과 매도 수량은 paper 원장에서 예약한다. 주문·체결·멱등키·가격조건은
`execution_mode=simulation`과 `runId`로 Postgres에 저장된다. 재시작 또는 LIVE 전환은
해당 run의 미체결 예약과 미발동 조건만 취소하고 체결된 현금·포지션·성과는 유지한다.

## API

백엔드는 다음 경로를 `/api/simulator/*`로 프록시한다.

```text
GET  /api/control/status
PUT  /api/control/mode       {"mode":"live"|"simulation"}
POST /api/control/action     {"action":"start"|"pause"|"resume"|"restart"}
PUT  /api/control/speed      {"speed":1|5|20|60}
GET  /api/control/candles
GET  /api/control/symbols
GET  /api/control/indices
GET  /api/control/quotes?symbols=AAPL,MSFT
GET  /api/control/order-flow?symbol=...
GET  /api/control/execution-events?runId=...&afterSequence=...&limit=...
```

Simulator는 계좌·주문·조건 control API를 제공하지 않는다. `simulation-paper-matcher`가
execution event를 순서대로 페이지 조회하고 공통 Postgres 원장을 갱신한다. Matcher는 현재
run의 미체결 주문·활성 가격조건 종목만 처리한다. 활성 종목이 없으면 quote를 순회하지 않고
simulator의 처리 완료 sequence까지 checkpoint를 전진시킨다. 활성 종목이 있으면 최대
1,000 raw event씩 읽고 25개 선택 quote마다 checkpoint와 heartbeat를 갱신한다.

지수 패널은 SIM에서 LIVE Yahoo 캐시를 사용하지 않는다. `GET /api/control/indices`가
재생 시작 시각 `2026-07-15 00:00 KST` 직전까지 관측된 15개 지수·시장지표의 불변
스냅샷을 반환한다. 따라서 같은 데이터셋의 모든 run이 같은 값을 표시하고 미래 값이
재생 화면에 섞이지 않는다.

SIM 차트는 재생 시작 전 정상 과거 캔들과 현재 가상시각까지의 replay 캔들만 합친다.
Bid/Ask 차트와 Order Flow 패널은 `simulation_replay_events`의 quote/trade를 종목별로
cursor까지만 읽어 `orderflow-estimated-v2` minute profile을 만든다. 정규장 체결만
집계하고 장외에는 직전 정규장 profile을 유지하되 현재 bid/ask는 계속 replay cursor의
호가를 사용한다. 최대 8종목 projection만 메모리에 두며 eviction 또는 Pod 복구 뒤에는
불변 replay 원본과 저장된 cursor로 다시 계산한다. 이 데이터는 LIVE Redis/Kafka나
`trade_ticks`·`quote_ticks`에 기록하지 않는다.

일봉은 UTC 자정이 아니라 `America/New_York` 시장 날짜의 자정을 canonical timestamp로
사용한다. 같은 시장 날짜의 과거 완성 봉과 replay 진행 봉은 하나로 합치고 replay 봉이
우선한다. 현재 시장 날짜의 일봉은 `isClosed=false`이며 다음 뉴욕 시장 날짜가 시작된
뒤에만 이전 일봉을 완료 상태로 바꾼다. 진행 일봉 OHLCV는 replay controller가 처리한
모든 trade에서 실행별로 누적·복원하며, 화면 갱신마다 ClickHouse 원본 틱을 다시 집계하지
않는다. `ready` 상태에서는 replay 시작 시각과 겹치는 과거 완성 일봉도 노출하지 않는다.
SIM 뉴스 패널은 live Redis 캐시 대신 ClickHouse만 읽는다. 최신 기사 경로는
`published_at`과 `localized_at`이 모두 현재 가상시각 이하인 저장 기사만 반환하고,
일별 요약 경로는 `generated_at <= virtualTime`인 스냅샷만 반환한다. 그 밖에
point-in-time 조회를 보장하지 못하는 뉴스 watchlist·추천·기업정보·AI 분석은
`simulation_data_unavailable`을 반환한다.

SIM 가상계좌 평가는 모든 보유종목을 한 번의 batch 요청으로 읽은 replay bid/ask만 사용한다.
replay 호가가 없으면 LIVE Redis, 최신 캔들, demo 가격으로 대체하지 않는다. 최초 호가
미도착은 `409 simulation_quote_not_ready`, simulator timeout은
`504 simulation_quote_timeout`, 연결 장애는 `503 simulation_service_unavailable`로
구분하며 모두 재시도 가능한 transient 상태다.
배당·52주 통계 같은 LIVE 보강은 실행하지 않으며 계좌 성과 이력은 `virtualTime` 이하의
스냅샷만 포함한다. 기업저널 report/evidence도 cutoff 조회가 구현되기 전까지 차단한다.

## 오프라인 V3 추천 fixture

`tools/recommendation_v3_fixture.py`는 AWS ClickHouse의 2026-07-14 실제 데이터를
read-only로 추출하고 cutoff-safe 추천 결과를 검증하는 별도 오프라인 도구다.
생성 파일은 저장소에 자동 포함되지 않으며 현재 tick replay runtime API에는 연결되지
않는다. fixture가 없을 때 synthetic 추천으로 대체하지 않는다.
검증된 fixed recommendation artifact가 배포된 경우에도 replay cursor가 artifact의
`evidenceAsOf`에 도달하기 전에는 추천 API가 409를 반환한다.

## dev EKS

AWS app overlay는 `Deployment/gops-simulator`를 `replicas: 1`로 유지한다. 일반 배포가
backend의 `GOPS_SIMULATOR_URL`과 simulator rollout을 함께 적용·검증하므로 별도 시작
스크립트가 필요 없다. Pod가 떠 있어도 기본 상태는 `LIVE/idle`이며 화면의 플레이 버튼을
누르기 전에는 run이나 replay가 시작되지 않는다. Alpaca ingestor, market processor,
실시간 Redis/Kafka는 변경하지 않는다.

```sh
AWS_PROFILE=gops-dev scripts/aws/deploy-dev-local.sh
AWS_PROFILE=gops-dev scripts/aws/run-simulator-replay-import.sh  # 최초 1회
```

기존 `READY` 데이터셋이 있으면 import도 다시 실행하지 않는다. 완전히 새 클러스터에서
데이터셋이 아직 없으면 simulator readiness gate가 배포를 실패 처리한다. 이 경우 위 import를
한 번 실행하고 일반 배포를 다시 실행한다. `start-dev-simulator.sh`는 데이터셋·health를
수동 점검하고 `LIVE/idle`로 돌리는 호환 도구일 뿐 replica나 backend 설정을 바꾸지 않는다.
`stop-dev-simulator.sh`도 재생 상태만 LIVE로 정리하며 Pod는 READY 상태로 유지한다.
