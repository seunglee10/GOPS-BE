# GOPS Tick Replay Simulator

저장된 실제 Alpaca trade/quote를 원본 타임스탬프와 고정 수집 순서대로 재생한다.
전쟁 시나리오, 합성 뉴스, 가격 랜덤화, 현재 시각으로의 타임스탬프 변경은 사용하지 않는다.

## 고정 데이터셋

- ID: `sp500-top20-20260715-kst-v1`
- 시간: KST `[2026-07-15 00:00, 2026-07-16 00:00)`
- UTC: `[2026-07-14 15:00, 2026-07-15 15:00)`
- 시총 기준일: `2026-06-30`
- 20개 기업·21개 티커: `NVDA, MSFT, AAPL, AMZN, META, AVGO, GOOGL, GOOG, BRK.B, TSLA, LLY, JPM, WMT, V, ORCL, MA, NFLX, XOM, COST, HD, JNJ`
- feed: `SIP 15:00–00:00 UTC`, `BOATS 00:00–08:00 UTC`, `SIP 08:00–15:00 UTC`

원본 gzip JSONL은
`s3://$S3_BUCKET/simulator/replay/v1/dataset=sp500-top20-20260715-kst/`에 두고,
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

시작 스크립트는 simulator와 backend 연결만 준비하고 전역 상태를 `LIVE/idle`에 둔다.
화면 상단의 플레이 버튼을 누르면 `start` action이 가상시각 `2026-07-15 00:00 KST`의
새 `runId`를 만들고 같은 응답에서 `running`으로 전환한다. 사용자 계좌는
Postgres의 영구 paper 원장을 계속 사용한다. 기본 배속은 `1×`이며 `1·5·20·60×`를
실행 중 바꿀 수 있다. 처리량이 부족하면 가상시계가 늦어질 뿐 틱은 버리지 않는다.
ClickHouse 청크 조회와 이벤트 처리는 HTTP 이벤트 루프 밖의 작업 스레드에서 수행하고,
status와 health는 마지막 완료 스냅샷을 즉시 반환한다. 따라서 큰 청크를 처리하는 동안에도
Kubernetes probe와 웹의 SIM 상태 폴링이 차단되지 않는다.

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
GET  /api/control/execution-events?runId=...&afterSequence=...&limit=...
```

Simulator는 계좌·주문·조건 control API를 제공하지 않는다. `simulation-paper-matcher`가
execution event를 순서대로 페이지 조회하고 공통 Postgres 원장을 갱신한다.

SIM 차트는 재생 시작 전 정상 과거 캔들과 현재 가상시각까지의 replay 캔들만 합친다.
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

## 오프라인 V3 추천 fixture

`tools/recommendation_v3_fixture.py`는 AWS ClickHouse의 2026-07-14 실제 데이터를
read-only로 추출하고 cutoff-safe 추천 결과를 검증하는 별도 오프라인 도구다.
생성 파일은 저장소에 자동 포함되지 않으며 현재 tick replay runtime API에는 연결되지
않는다. fixture가 없을 때 synthetic 추천으로 대체하지 않는다.

## dev EKS

`Deployment/gops-simulator`는 평소 `replicas: 0`이다. 시작 스크립트는 ClickHouse
스키마와 `READY` 데이터셋을 확인한 뒤 simulator Pod와 backend 연결만 준비한다.
스크립트 완료 시에는 `LIVE/idle`이며 화면의 플레이 버튼을 누르기 전에는 run이나 replay가 시작되지 않는다.
Alpaca ingestor, market processor, 실시간 Redis/Kafka는 변경하지 않는다.

```sh
LOCAL_REF=dev FORCE_SERVICES=frontend,backend,simulator \
  AWS_PROFILE=gops-dev scripts/aws/deploy-dev-local.sh
AWS_PROFILE=gops-dev scripts/aws/run-simulator-replay-import.sh  # 최초 1회
AWS_PROFILE=gops-dev scripts/aws/start-dev-simulator.sh
AWS_PROFILE=gops-dev scripts/aws/stop-dev-simulator.sh
```
