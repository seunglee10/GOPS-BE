# Market Data Stabilization Functional Contract

이 문서는 Goal 구현 과정의 진행 로그가 아니라, 팀이 유지해야 할 시장데이터 기능 계약입니다. 세부 이력은 `archive/` 문서를 참고합니다.

## 목표 상태

- Alpaca 데이터는 S&P 500 universe를 기준으로 수집하고 제공한다.
- canonical historical source는 `1m` bar와 `1D` daily bar다.
- `5m`, `10m`, `1W`, `1M`은 canonical source에서 파생한다.
- 마지막 candle은 모든 interval에서 provisional/live 상태로 갱신되어야 한다.
- chart request path는 Redis/ClickHouse를 통해 응답한다. 사용자의 차트 조회 중 S3 또는 Alpaca를 동기 호출하지 않는다.
- S3는 durable preload, replay, materialize, 증거 저장소로 사용한다.
- 미국 정규장 종료 상태에서는 live-market 성공을 주장하지 않는다. local replay, stored data, WebSocket trace, browser smoke로 경로를 검증하고 market-hours smoke는 별도 운영 체크로 남긴다.

## 데이터 범위

- `1D`: 최대 3년 historical target을 유지한다.
- `1m`: `2025-04` inclusive cutoff를 유지한다. `2025-03` 이전 `1m` preload를 새로 만들지 않는다.
- 기존 S3/ClickHouse에 이미 존재하는 더 오래된 데이터는 삭제하지 않는다.
- serving query는 target floor를 기준으로 응답 범위와 `hasMoreBefore`를 계산한다.

## Universe And Subscription

- 기본 universe는 S&P 500이다.
- full-universe로 우선 유지할 데이터는 bars, updatedBars, dailyBars, statuses다.
- trades는 tiered subscription이다.
- Hot Ranking은 S&P 500 전체의 거래대금 기준 Top 20이며, 해당 종목은 trade tier 후보가 된다.
- Watch List는 사용자 편집 가능 상태여야 하며, 편집된 종목은 가격과 전일 종가 대비 등락률이 갱신되어야 한다.
- 기본 Watch List는 첫 사용자에게 보여주는 seed일 뿐, 하드코딩된 최종 구독 목록이 아니다.

## Feed And Session

- Alpaca ingest는 명시적인 feed profile을 사용한다: `sip`, `iex`, `boats`.
- raw envelope, stream transform, Redis latest/live state, ClickHouse row, API snapshot, chart runtime data는 `feedProfile`과 `marketSession`을 보존한다.
- historical row에 session metadata가 없으면 serving 단계에서 보정한다.
- daily, weekly, monthly candle은 session fallback을 `regular`로 둔다.
- 기존 ClickHouse volume은 column 추가는 가능하지만 feed/session-aware `ORDER BY`는 자동 변경되지 않는다. 기존 volume을 그대로 쓰는 배포는 table rebuild 여부를 별도로 검토한다.

## API And Frontend Contract

- 유지해야 하는 route:
  - `GET /api/charts/candles`
  - `POST /api/charts/backfill`
  - `GET /api/charts/backfill/status`
  - `GET /api/charts/backfill/queue`
  - `GET /api/charts/symbols`
  - `GET /api/charts/watchlist`
  - `PUT /api/charts/watchlist`
  - `GET /api/charts/hot-symbols`
  - `WS /ws/charts`
- candle response는 다음 의미를 일관되게 유지한다:
  - `dataStatus`
  - `repairStatus`
  - `coverage.renderable`
  - `targetRangeFrom`
  - `storedCandleCount`
  - `hasMoreBefore`
  - `feedProfile`
  - `marketSession`
- previous close/change percent는 전일 종가 기준이다. intraday open fallback으로 대체하지 않는다.
- partial이더라도 renderable이면 차트는 먼저 보여주고, 필요한 경우 bounded range backfill만 요청한다.
- 정규장 내부의 큰 gap은 repair/backfill 대상이다.
- 정규장 밖 sparse bar는 renderable로 본다.

## Backfill And GapFill

- Backfill queue backend는 Redis Streams다.
- job status는 idempotency, attempt, claim, heartbeat, checkpoint, terminal state를 보존한다.
- chart에서 과거 구간이 필요할 때:
  - 먼저 candles API로 older window를 요청한다.
  - repairable이면 bounded range backfill을 enqueue한다.
  - status를 polling한다.
  - success 이후 같은 chart request path로 refetch한다.
- Backfill worker의 source preference는 coverage-first다:
  - ClickHouse coverage
  - processed S3 manifest/object
  - bounded raw/processed S3 partition
  - Alpaca
- GapFill은 누락 bucket만 coalesce해서 요청한다.
- replay/correction job은 S3 기반으로 실행하며 Alpaca-only replay는 거부한다.

## S3 And Materialization

- runtime/preload 기본 포맷은 parquet + compact manifest다.
- S3 manifest와 object row count는 materialize smoke의 기준이다.
- S3에 미리 적재된 데이터는 배포 후 ClickHouse materialize를 통해 활용할 수 있어야 한다.
- API chart request가 직접 S3를 scan하거나 Alpaca를 호출하는 흐름은 금지한다.
- materialize는 idempotent해야 한다. 중복 insert는 serving dedup/latest-row 계약으로 안전해야 한다.

## Local Verification Gate

팀 병합 후 최소 검증은 다음을 통과해야 한다.

```bash
PYTHONPATH=systems/market-data/shared:systems/api-server/pods/api-server/gops-backend .venv/bin/python -m pytest systems/market-data/tests/test_market_data_hardening.py systems/market-data/tests/test_realtime_boundary.py -q
PYTHONPATH=systems/market-data/shared:systems/api-server/pods/api-server/gops-backend .venv/bin/python -m pytest systems/api-server/tests/test_market_data_query.py -q
```

```bash
cd apps/gops-frontend
npm run test:chart
npm run build
```

```bash
docker compose config --quiet
kubectl kustomize infra/k8s/base
kubectl kustomize infra/k8s/overlays/aws
```

브라우저 smoke는 최소한 AAPL, NVDA, TSLA에 대해 `1m`, `5m`, `10m`, `1D`, `1W`, `1M` 전환, drag-left pagination, Watch List, Hot Ranking, console warning/error 확인을 포함한다.

## Out-Of-Band Operational Check

정규장 market-hours proof는 로컬 Goal 완료 조건에 포함하지 않았다. 배포 또는 운영 점검에서 다음을 별도로 확인한다.

- Alpaca account/feed connection limit.
- 실제 market-hours raw ingest arrival.
- Python processor heartbeat freshness.
- Redis latest/live key freshness.
- ClickHouse insert freshness.
- API/WebSocket/browser last-candle update.
