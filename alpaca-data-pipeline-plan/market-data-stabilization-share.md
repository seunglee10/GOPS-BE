# Market Data Canonical Rebuild Functional Contract

이 문서는 Goal 구현 로그가 아니라 팀이 유지해야 할 시장데이터 기능 계약입니다. 이번 리빌드는 20개 종목 데모 범위에서 canonical S3, ClickHouse serving, Redis realtime cache, chart pagination을 안정화하는 작업입니다.

## Target Scope

- 대상 종목은 정확히 20개다:
  `AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, BRK.B, JPM, UNH, V, XOM, MA, AVGO, PG, COST, HD, JNJ, NFLX, AMD`
- 기본 Watch List seed는 10개다:
  `AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, BRK.B, JPM, UNH`
- Hot Ranking은 같은 20개 안에서 거래대금 Top10이다.
- 검색/드롭다운/구독/initial load/backfill/hot/watch 기본 설정은 이 20개 universe를 벗어나면 안 된다.
- 팀원의 프론트엔드/에이전트 구조는 보존하고, 시장데이터 계약에 필요한 API/차트 로직만 병합한다.

## Canonical Data

- Historical serving은 `canonical_version=v2` + `price_adjustment=split`만 허용한다.
- Alpaca historical 요청은 `adjustment=split` 기준이다.
- Canonical source interval은 `1m`과 `1D`다.
- `5m/10m`은 `1m`에서 파생한다.
- `1W/1M`은 `1D`에서 파생한다.
- Legacy/raw/unknown adjustment row는 chart API에 반환하지 않는다.
- Fake market candles는 생성하지 않는다.

## Data Range

- `1D`: 3년 historical target.
- `1m`: 3년 historical target.
- 시간이 부족하면 `1m`은 최근 3개월, 최근 1년, 전체 3년 순서로 확장한다.
- Target floor에 도달한 경우에만 `hasMoreBefore=false` 또는 target-boundary 상태가 허용된다.
- 최소 데모 gate는 `1D` 3년 + `1m` 최근 3개월이다.
- full rebuild gate는 `1m` 3년까지 완료해야 한다.

## Rebuild Boundary

- 새 리빌드는 fresh S3 prefix 또는 명시적으로 초기화된 prefix에서만 시작한다.
- ClickHouse chart-serving data와 Redis market-data/cache/queue state는 리빌드 범위 안에서 reset한다.
- 기존 S3/ClickHouse/Redis legacy data는 새 serving 계약에 섞지 않는다.
- reset은 market-data/chart 범위에 한정하고 agent/order/auth data는 건드리지 않는다.

## S3 Contract

- S3는 canonical historical preload, evidence, replay, materialization source다.
- Canonical S3 object는 logical chunk 기준 deterministic key를 사용한다.
- 같은 symbol/interval/range/canonical/adjustment chunk를 다시 실행해도 duplicate object가 생기면 안 된다.
- `force=false`는 기존 valid manifest/object를 재사용한다.
- `force=true`는 명확한 revision 또는 replace 정책을 따른다.
- Manifest는 row count, min/max timestamp, symbol, interval, adjustment, canonical version, source evidence를 포함한다.
- S3에 있는 데이터를 무시하고 Alpaca를 먼저 호출하면 안 된다.
- Raw/live/trade S3 data는 canonical historical source로 섞지 않는다.

## Serving And Backfill

- Chart request path는 Redis/ClickHouse만 조회한다.
- API가 사용자 요청 중 S3를 직접 scan하거나 Alpaca를 동기 호출하면 안 된다.
- ClickHouse에 요청 window가 없으면 bounded backfill/materialization job이 처리한다.
- Backfill source order:
  1. Redis recent/live
  2. ClickHouse canonical serving
  3. S3 processed canonical parquet + manifest
  4. S3 raw archive, 명시적으로 허용된 경우
  5. Alpaca historical API
- S3 present + ClickHouse empty 상황에서는 S3 -> ClickHouse materialize가 우선이다.
- S3 missing + target allowed 상황에서만 Alpaca fallback을 사용하고, 결과는 canonical S3 evidence로 남긴다.
- Left-pan pagination은 `before` cursor를 사용하고, 기존 candle 왼쪽에 append하며 timestamp/date 중복을 제거한다.
- Backfill 중에도 renderable chart는 빈 화면으로 바뀌면 안 된다.

## Redis And Realtime

- Redis는 realtime/latest/recent cache다. durable historical source가 아니다.
- 20개 전체에 대해 `bars`, `updatedBars`, `dailyBars`, `statuses`를 처리한다.
- `trades`는 active chart, user watchlist, Hot Top10 tier 안에서만 구독한다.
- Trades는 current price, provisional/live candle, tick chart 계열에 사용한다.
- Closed canonical `1m`/`1D` row와 provisional/live row가 같은 timestamp에서 보일 때 serving 결과는 deterministic해야 한다.

## API And Frontend Contract

유지해야 하는 route:

- `GET /api/charts/candles`
- `POST /api/charts/backfill`
- `GET /api/charts/backfill/status`
- `GET /api/charts/backfill/queue`
- `GET /api/charts/symbols`
- `GET /api/charts/watchlist`
- `PUT /api/charts/watchlist`
- `GET /api/charts/hot-symbols`
- `WS /ws/charts`

Candle response metadata must remain consistent:

- `dataStatus`
- `repairStatus`
- `coverage.renderable`
- `targetRangeFrom`
- `storedCandleCount`
- `hasMoreBefore`
- `feedProfile`
- `marketSession`

Previous close/change percent는 전일 종가 기준이다. Intraday open fallback으로 대체하지 않는다.

## Local Verification Gate

```bash
PYTHONPATH=systems/market-data/shared:systems/api-server/pods/api-server/gops-backend .venv/bin/python -m pytest systems/market-data/tests/test_market_data_hardening.py -q
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

Browser smoke:

- AAPL, MSFT, NVDA, TSLA, UNH, BRK.B.
- `1m`, `5m`, `10m`, `1D`, `1W`, `1M`.
- Drag-left pagination.
- Watch List default 10.
- Hot Ranking Top10.
- Console error/warning check.

## Operational Note

정규장 market-hours proof는 로컬 Goal 완료 조건과 분리한다. 배포 또는 운영 점검에서는 Alpaca live connection, Redis freshness, ClickHouse insert freshness, API/WebSocket last-candle update를 별도로 확인한다.
