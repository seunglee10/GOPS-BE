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

- `1D`: 최대 6년 lazy target.
- `1m`: 최대 6년 lazy target.
- `5m/10m`은 `1m` source coverage를 따라 최대 6년까지 확장한다.
- `1W/1M`은 `1D` source coverage를 따라 최대 6년까지 확장한다.
- 6년 target은 최초 진입 때 전체 preload를 수행한다는 뜻이 아니다.
- 최초 차트 요청은 visible window만 반환하고, 왼쪽 이동 시 bounded page/gap backfill로 필요한 만큼만 채운다.
- Target floor에 도달한 경우에만 `hasMoreBefore=false` 또는 target-boundary 상태가 허용된다.
- Warm preload는 Watch List, Hot Top10, active chart 후보의 체감 속도를 위한 선택적 최적화다.
- Full 6-year preload는 운영자가 명시적으로 선택한 배치 작업일 때만 수행한다.

## Rebuild Boundary

- 새 리빌드는 fresh S3 prefix 또는 명시적으로 초기화된 prefix에서만 시작한다.
- ClickHouse chart-serving data와 Redis market-data/cache/queue state는 리빌드 범위 안에서 reset한다.
- 기존 S3/ClickHouse/Redis legacy data는 새 serving 계약에 섞지 않는다.
- reset은 market-data/chart 범위에 한정하고 agent/order/auth data는 건드리지 않는다.

## S3 Contract

- S3는 canonical historical preload, evidence, replay, materialization source다.
- Raw archive, live processed sink, final canonical data는 서로 다른 역할을 가진다.
  - `raw`: Alpaca/Kafka 원본 증거와 replay source.
  - `live`: append-style 오늘/live 증거. object 순서와 중복을 chart serving 기준으로 믿지 않는다.
  - `final`: deterministic canonical parquet/manifest. ClickHouse rebuild source.
- Canonical S3 object는 logical chunk 기준 deterministic key를 사용한다.
- 같은 symbol/interval/range/canonical/adjustment chunk를 다시 실행해도 duplicate object가 생기면 안 된다.
- `force=false`는 기존 valid manifest/object를 재사용한다.
- `force=true`는 명확한 revision 또는 replace 정책을 따른다.
- Manifest는 row count, min/max timestamp, symbol, interval, adjustment, canonical version, source evidence를 포함한다.
- S3에 있는 데이터를 무시하고 Alpaca를 먼저 호출하면 안 된다.
- Raw/live/trade S3 data는 canonical historical source로 직접 섞지 않는다.
- S3 live/raw data를 serving에 쓰려면 먼저 event time 정렬, logical key dedup, canonical final/ClickHouse materialize 단계를 거친다.

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
- Sparse regular-session gap은 `coverage.gapRanges`로 내려보내고, UI/worker는 해당 작은 범위만 gapfill한다.
- 화면 진입만으로 `force=true` full backfill을 만들면 안 된다.
- Left-pan pagination은 `before` cursor를 사용하고, 기존 candle 왼쪽에 append하며 timestamp/date 중복을 제거한다.
- Backfill 중에도 renderable chart는 빈 화면으로 바뀌면 안 된다.
- Backfill 완료 후에는 canonical priority로 dedupe/sort된 candle만 append/replace한다.
- `1m`, `5m`, `10m`, `1D`, `1W`, `1M` 모두 같은 lazy browsing 계약을 따른다.

## HTS-Style Chart Continuity

- 저장소 canonical data는 실제 Alpaca `bars`, `updatedBars`, `dailyBars`만 보존한다.
- 거래가 없는 extended-hours/overnight minute를 ClickHouse/S3에 가짜 canonical candle로 저장하지 않는다.
- 차트 표시 계층은 sparse extended-hours/overnight 구간을 끊긴 오류처럼 보이지 않게 처리한다.
- 표시용 continuity candle은 이전 close를 이어받고 volume은 `0`이며, 내부적으로 `displayOnly` 또는 `synthetic`으로 취급한다.
- 공식 closed bar 또는 backfill 결과가 같은 timestamp에 도착하면 표시용 candle은 즉시 교체된다.
- 정규장 내부 누락은 display-only로 숨기지 않고 `gapRanges`를 만든 뒤 S3-first/Alpaca-last gapfill 대상으로 둔다.
- `coverage.renderable=false`가 최신 live/provisional candle 표시 자체를 막으면 안 된다.

## Redis And Realtime

- Redis는 realtime/latest/recent cache다. durable historical source가 아니다.
- 20개 전체에 대해 `bars`, `updatedBars`, `dailyBars`, `statuses`를 처리한다.
- `trades`는 active chart, user watchlist, Hot Top10 tier 안에서만 구독한다.
- Trades는 current price, provisional/live candle, tick chart 계열에 사용한다.
- Closed canonical `1m`/`1D` row와 provisional/live row가 같은 timestamp에서 보일 때 serving 결과는 deterministic해야 한다.
- Redis live/provisional, ClickHouse historical/final, gapfill result를 합칠 때는 timestamp 기준으로 정렬하고 같은 timestamp는 canonical priority로 하나만 남긴다.

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
- `coverage.gapRanges`

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
