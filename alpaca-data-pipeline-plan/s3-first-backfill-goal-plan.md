# 20-Symbol Lazy Backfill And HTS-Style Chart Plan

이 문서는 Goal 모드에서 그대로 실행할 시장데이터/차트 리빌드 계획이다.
구현 로그가 아니라 현재 합의된 기능 계약과 검증 순서만 담는다.

## 1. Objective

20개 데모 종목에 대해 S3, ClickHouse, Redis, API, chart runtime을 깨끗한 canonical 기준으로 다시 맞춘다.

AWS 배포에서도 로컬과 같은 market-data 계약을 사용한다. 즉 로컬에서 검증한 ClickHouse serving data, S3 canonical parquet/manifest, Redis chart/live/backfill keyspace, Kafka chart topic contract가 AWS/EKS 실제 환경에서도 같은 의미와 범위를 가져야 한다. S3는 같은 저장소를 사용한다고 가정하되, 배포 전 object/manifest audit로 확인한다.

최종 구조:

```text
Alpaca historical API
  -> canonical S3 parquet + compact manifest
  -> ClickHouse chart serving
  -> /api/charts/candles
  -> chart runtime/browser

Alpaca live stream
  -> Kafka/Python processor
  -> Redis realtime/latest/provisional
  -> ClickHouse closed canonical bars
  -> API/WebSocket/browser
```

핵심 목표:

- S3에 duplicate logical chunk가 생기지 않는다.
- ClickHouse는 canonical S3에서 재구축할 수 있다.
- Redis는 realtime/latest/recent cache로만 쓴다.
- ClickHouse에 없으면 S3를 먼저 보고, S3에도 없을 때만 Alpaca를 호출한다.
- 6년치 데이터를 미리 전부 받지 않고, 차트 왼쪽 이동 시 bounded page 단위로 필요한 과거만 채운다.
- Toss/HTS처럼 sparse extended-hours/overnight 구간은 차트 표시 계층에서 자연스럽게 이어 보이게 한다.
- 기존 S3/ClickHouse/Redis에 남아 있던 legacy 데이터는 새 serving 계약에 섞지 않는다.

## 2. Scope

포함:

- `systems/market-data`
- chart 관련 API server route/service
- `apps/chart-engine`
- frontend의 chart data loading, Watch List, Hot Ranking 연결부
- Docker Compose, k8s, env, ClickHouse schema 중 시장데이터 계약에 필요한 부분
- 시장데이터/차트 테스트와 browser smoke

제외:

- agent orchestration
- order/KIS flow
- 비차트 패널의 제품 구조
- 팀원이 진행 중인 frontend layout/design 전면 교체
- fake market candle 생성

팀 병합 시 UI/agent 구조는 팀원 브랜치를 우선하고, 이 문서의 시장데이터 계약만 기능 단위로 옮긴다.

## 3. Fixed Decisions

### Universe

정확히 20개만 사용한다.

```text
AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, BRK.B, JPM, UNH,
V, XOM, MA, AVGO, PG, COST, HD, JNJ, NFLX, AMD
```

기본 Watch List seed는 첫 사용자용 10개다.

```text
AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, BRK.B, JPM, UNH
```

Hot Ranking:

- 같은 20개 안에서 거래대금 Top10.
- current-session `1m` 데이터가 부족하면 latest daily-session 거래대금으로 부족분을 채운다.
- frontend에는 Watch List와 유사한 패널 형식으로 보여준다.

검색, 구독, initial load, backfill, Watch List, Hot Ranking은 이 20개 밖으로 조용히 확장되면 안 된다.

### Canonical Historical Data

- historical canonical version: `v2`
- historical price adjustment: `split`
- Alpaca historical fetch: `adjustment=split`
- chart serving: `canonical_version='v2'` and `price_adjustment='split'` only

`price_adjustment='split'`은 주식분할을 반영한 과거 OHLCV를 뜻한다. 라이브 현재가/체결가와 과거 split-adjusted row를 같은 canonical row처럼 섞으면 안 된다.

Canonical source intervals:

- `1m`
- `1D`

Derived intervals:

- `5m`, `10m`: canonical `1m`에서 query-time 파생
- `1W`, `1M`: canonical `1D`에서 query-time 파생

Legacy/raw/unknown-adjustment row는 chart API에 반환하지 않는다.

### Historical Lazy Target

- `1D`: 최대 6년 lazy target
- `1m`: 최대 6년 lazy target
- `5m`, `10m`: `1m` source coverage를 따라 최대 6년까지 lazy 확장
- `1W`, `1M`: `1D` source coverage를 따라 최대 6년까지 lazy 확장

6년 target은 "처음부터 모두 preload"한다는 뜻이 아니다. 차트 최초 진입은 visible window만 빠르게 보여주고, 사용자가 과거로 이동할 때마다 small page/gap range 단위로 채운다. `backfillTargetBars`는 최대 탐색 범위이고, `maxRequestBars`는 한 번에 요청/렌더링하는 page cap이다.

Warm preload는 제품 체감과 기준값을 위한 최소 캐시다.

- `1D`: Watch List, Hot Top10, active chart 후보 위주로 먼저 채울 수 있다.
- `1m`: active chart visible/recent page만 우선 채운다.
- full 6-year preload는 기본 목표가 아니며, 운영자가 명시적으로 선택한 배치 작업일 때만 수행한다.
- target floor에 도달하기 전에는 candle 수 제한만으로 `hasMoreBefore=false`를 반환하지 않는다.

## 4. Storage Contracts

### S3

S3는 canonical historical evidence와 ClickHouse rebuild source다.

Rules:

- Prefix roles are fixed:
  - `raw`: append archive for Alpaca/Kafka evidence and replay.
  - `live`: append evidence for today/live processed events, not direct serving truth.
  - `final`: deterministic canonical parquet/manifest used for ClickHouse rebuild.
- 새 rebuild는 fresh prefix 또는 명시적으로 초기화된 prefix에서만 시작한다.
- canonical object identity는 `symbol + interval + rangeStart + rangeEnd + canonical_version + price_adjustment`다.
- 같은 logical chunk는 같은 deterministic key를 사용한다.
- `force=false`는 valid object와 manifest가 있으면 skip한다.
- `force=true`는 명시적 replace/revision 정책을 사용한다.
- wall-clock upload timestamp가 logical identity가 되면 안 된다.
- manifest는 row count, min/max timestamp, symbol, interval, version, adjustment, source evidence를 포함한다.
- S3 existence/manifest validation을 Alpaca fetch보다 먼저 수행한다.
- raw/live/trade S3는 canonical historical source로 직접 섞지 않는다.
- raw/live S3를 serving에 반영하려면 event time 정렬, logical key dedup, canonical final/ClickHouse materialize 단계를 거친다.

### ClickHouse

ClickHouse는 chart serving store다.

Rules:

- canonical `v2 + split`만 chart API serving 대상으로 삼는다.
- S3 canonical parquet에서 재구축 가능해야 한다.
- `(symbol, interval, event_time/date, canonical_version, price_adjustment)` 기준 중복이 API에 노출되면 안 된다.
- historical closed row와 live/provisional row 충돌 시 deterministic priority가 있어야 한다.
- 기존 ClickHouse volume/schema가 새 `ORDER BY`를 반영하지 못하면 table rebuild 필요성을 보고한다.

### Redis

Redis는 realtime/latest/recent cache와 queue state다.

Rules:

- durable historical source가 아니다.
- flush해도 historical chart data를 잃으면 안 된다.
- stale recent/live key가 ClickHouse canonical coverage를 가리면 안 된다.
- Watch List 사용자 편집 상태와 live tier control state는 명확히 구분한다.

## 5. Backfill, Pagination, And HTS-Style Continuity Contract

Backfill source priority:

1. Redis recent/live cache
2. ClickHouse canonical serving
3. S3 processed canonical parquet + manifest
4. S3 raw archive, only if explicitly enabled and convertible
5. Alpaca historical API

Chart request path:

- `/api/charts/candles`는 Redis/ClickHouse만 직접 조회한다.
- 사용자 요청 중 S3를 직접 scan하거나 Alpaca를 동기 호출하지 않는다.
- missing window는 bounded job으로 처리한다.
- regular-session sparse gap은 `coverage.gapRanges`로 내려보내고, UI는 그 작은 range만 backfill한다.
- 화면 진입이나 terminal backfill status만으로 `force=true` full-range backfill을 자동 생성하지 않는다.
- 최초 진입은 visible window만 요청한다. 6년 target 전체를 한 번에 요청하거나 backfill하지 않는다.
- 왼쪽 이동은 `before` cursor와 page limit으로만 과거를 확장한다.
- `maxRequestBars`를 6년 target과 동일하게 키우지 않는다. 큰 target은 product coverage 목표이고, API 요청은 bounded page로 유지한다.
- page/gap job은 ClickHouse 조회 -> S3 materialize -> Alpaca fetch 순서로 처리하고, 결과를 canonical S3와 ClickHouse에 저장한 뒤 같은 API path로 재조회한다.

Required behavior:

- ClickHouse에 요청 window가 있으면 즉시 반환한다.
- ClickHouse에 없고 S3에 있으면 S3 -> ClickHouse materialize 후 같은 chart path로 재조회한다.
- S3에도 없고 target range 안이면 Alpaca -> canonical S3 -> ClickHouse 후 재조회한다.
- target floor 전에는 `hasMoreBefore=false`를 반환하지 않는다.
- target floor에 도달한 경우에만 target-boundary state를 반환한다.
- `before` cursor 요청은 boundary candle을 반복하지 않고 더 과거 candle만 반환한다.
- 반환 candle은 시간 오름차순이다.
- left-pan은 bounded page 단위로 동작한다.
- left-pan이나 repair 중에도 기존 renderable chart를 빈 화면으로 만들지 않는다.
- `1m` full-range force backfill을 화면 진입 시 자동 실행하지 않는다.
- backfill 중에는 기존 candle을 유지하고, 완료 후 timestamp/date 기준 dedupe/sort된 candle만 append/replace한다.

HTS-style continuity:

- 저장소 canonical data는 실제 Alpaca closed bar/daily bar만 보존한다.
- display continuity candle은 ClickHouse/S3 canonical row로 저장하지 않는다.
- extended-hours/overnight에서 거래가 없는 minute는 chart display 계층에서 끊겨 보이지 않게 처리한다.
- display-only candle은 이전 close를 이어받고 volume은 `0`으로 둔다.
- display-only candle은 내부적으로 `synthetic/displayOnly`로 취급하고, 공식 `bars`/`updatedBars` 또는 backfill 결과가 도착하면 즉시 교체한다.
- 정규장 내부 sparse gap은 숨기지 않고 `gapRanges`를 만든 뒤 bounded gapfill 대상으로 둔다.
- `coverage.renderable=false`가 최신 live/provisional candle 표시 자체를 막으면 안 된다.

Derived interval behavior:

- `5m/10m`의 missing coverage는 `1m` source를 채운 뒤 파생한다.
- `1W/1M`의 missing coverage는 `1D` source를 채운 뒤 파생한다.
- derived interval도 최초 진입은 visible window만 그리고, 왼쪽 이동 시 source interval page/gap backfill을 먼저 수행한다.

## 6. Live Data Contract

20개 universe 전체:

- `bars`
- `updatedBars`
- `dailyBars`
- `statuses`

Tiered trade subscription:

- active chart
- user Watch List
- Hot Top10

Live rules:

- trades는 current price, tick chart, provisional candle에 사용한다.
- closed `1m`/`1D` bars는 canonical path로 들어간다.
- live/provisional row를 canonical historical `v2 + split` row처럼 저장/서빙하지 않는다.
- Watch List와 Hot Ranking의 현재가, 전일종가 대비 등락률, 거래대금은 stored/live source에서 갱신되어야 한다.
- 정규장 종료 상태에서는 실제 live-market 성공을 주장하지 않는다. controlled replay/local trace로 검증하고 market-hours smoke를 별도 운영 gate로 남긴다.

## 7. Milestones

### M-1. Local To AWS Runtime Parity

Goal:

- 로컬 검증 상태와 AWS/EKS 실제 ClickHouse, S3, Redis, Kafka 차트 데이터 계약이 같은지 확인한다.

Checks:

- AWS ConfigMap, Secret reference, running pod env, `/health/config`가 로컬 market-data 계약과 일치한다.
- `gops20`, Hot Top10, `v2 + split`, parquet S3, canonical-only serving guard가 AWS active runtime에 들어가 있다.
- S3 bucket/prefix는 로컬에서 적재한 canonical parquet/manifest와 같은 저장소를 바라본다.
- AWS ClickHouse는 reset 후 S3 canonical data에서 재구축 가능하다.
- AWS Redis는 chart/live/backfill keyspace만 reset 가능하고 auth/order/agent key를 건드리지 않는다.
- AWS Kafka chart topics와 consumer groups는 같은 message contract를 사용하며 stale lag가 없는지 확인한다.

Runbook:

- `alpaca-data-pipeline-plan/aws-market-data-reset-runbook.md`를 따른다.

### M0. Current-State And Config Audit

Goal:

- 다음 작업 전에 실제 runtime, env, S3 prefix, ClickHouse, Redis 상태를 재측정한다.

Checks:

- `ALPACA_UNIVERSE=gops20`
- active symbol universe is exactly 20
- `HOT_TIER_SIZE=10`
- `S3_PROCESSED_FORMAT=parquet`
- `HISTORICAL_ADJUSTMENT=split`
- `ALLOW_NON_CANONICAL_HISTORICAL_ADJUSTMENT=false`
- `CLICKHOUSE_REQUIRE_CANONICAL_CANDLES=true`
- `S3_REQUIRE_CANONICAL_PROCESSED_CANDLES=true`
- stale `sp500`, `semiconductor-100`, Hot Top20, `2025-04` cutoff, `jsonl` output가 active runtime에 남아 있지 않은지 확인
- BRK.B normalization 확인

Output:

- 현재 S3 object count
- ClickHouse row count by interval/version/adjustment
- Redis key/stream count
- running market-data writer containers/jobs
- `/health/config` redacted result

### M1. Safe Reset

Goal:

- 새 rebuild가 old data와 섞이지 않게 한다.

Steps:

1. market-data writers stop:
   - `alpaca-ingestor`
   - `local-stream-processor`
   - `s3-sink`
   - `raw-s3-archive`
   - `clickhouse-loader`
   - `backfill-worker`
2. orphan writer가 없는지 확인한다.
3. active S3 rebuild prefix가 비었거나 fresh prefix인지 확인한다.
4. ClickHouse chart-serving data를 reset한다.
5. Redis market-data/cache/queue state를 reset한다.
6. reset 후 row/key/stream count를 다시 기록한다.

Safety:

- reset 대상 prefix/table/db를 출력하고 확인한 뒤 실행한다.
- unrelated agent/order/auth data는 건드리지 않는다.

### M2. Canonical Guard And S3 Dedup

Goal:

- S3와 serving에 duplicate/non-canonical data가 들어오지 못하게 한다.

Checks:

- one `1D` chunk dry-run twice
- one `1m` chunk dry-run twice
- second run with `force=false` skips existing valid object/manifest
- invalid/missing manifest is not served silently
- S3 present chunk prevents Alpaca refetch

Tests:

- same chunk twice creates one canonical object
- `force=true` follows documented replace/revision policy
- S3 keys do not use upload-time as logical identity

### M3. Historical Warm Baseline: Daily/Reference Data

Goal:

- Watch List, Hot Top10, active chart 후보의 일봉 기준값을 먼저 채운다.

Why first:

- previous close
- daily/weekly/monthly charts
- Hot fallback baseline
- Watch List percent/change baseline

Checks:

- planned chunk count
- completed/skipped/failed/dead-letter count
- S3 processed object count
- manifest count
- ClickHouse row count
- per-symbol min/max date
- duplicate canonical date count

Do not continue if failed chunks are unresolved.

### M4. Lazy Page Backfill Foundation

Goal:

- 6년치 전체 preload 없이, 모든 interval에서 visible page와 left-pan page backfill이 동작하게 한다.

Rules:

- chart initial request는 visible page만 가져온다.
- left-pan request는 `before` cursor를 반드시 포함한다.
- page size는 interval별 bounded limit 안에서만 사용한다.
- ClickHouse miss는 S3 materialize job을 먼저 만든다.
- S3 miss는 Alpaca gapfill job을 small range로 만든다.
- `1m` 6-year force backfill은 frontend에서 생성할 수 없다.
- completed job은 같은 API window 재조회로 검증한다.

Checks:

- `/api/charts/candles?before=...` returns older candles only
- page boundary candle is not duplicated
- ClickHouse empty + S3 present -> materialize
- S3 missing + target allowed -> Alpaca gapfill
- target floor before 6 years/listing date returns `hasMoreBefore=true`
- target floor reached returns `hasMoreBefore=false`
- no synchronous S3 scan or Alpaca call in the chart request

This is the minimum local demo intraday gate.

### M5. S3 To ClickHouse Materialization

Goal:

- ClickHouse가 비어 있어도 S3 canonical evidence로 복구 가능함을 증명한다.

Checks:

- matching S3 object exists for a small scoped window
- scoped ClickHouse rows are deleted or isolated
- explicit `S3_MATERIALIZE_KEYS` materializes the object
- second materialize is idempotent
- chart API returns only `v2 + split`

Required scenarios:

- ClickHouse empty + S3 present -> materialize, no Alpaca call
- S3 missing + target allowed -> Alpaca fallback job
- materialized twice -> no duplicate API candles

### M6. API, Backfill, And Pagination

Goal:

- 차트가 빠르게 뜨고, 왼쪽 이동으로 최대 6년 target까지 과거가 안정적으로 확장된다.

Checks:

- `/api/charts/candles` sorted ascending
- `before` cursor returns older candles only
- boundary candle is not duplicated
- `hasMoreBefore=false` only at target floor
- metadata is consistent:
  - `dataStatus`
  - `repairStatus`
  - `coverage.renderable`
  - `targetRangeFrom`
  - `storedCandleCount`
  - `hasMoreBefore`
  - `feedProfile`
  - `marketSession`
- partial but renderable chart remains visible during backfill
- real non-renderable gap creates bounded repair/backfill state
- single live candle does not make the chart falsely ready
- sparse extended-hours/overnight candles remain displayable
- display-only continuity candles are not persisted as canonical data
- official closed candles replace display-only/provisional candles at the same timestamp

Representative symbols:

- AAPL
- MSFT
- NVDA
- TSLA
- UNH
- BRK.B
- AMD

Intervals:

- `1m`
- `5m`
- `10m`
- `1D`
- `1W`
- `1M`

### M7. Live Path And Day-Market Readiness

Goal:

- 실시간 데이터가 들어오는 경로를 closed-market에서도 추적 가능하게 만든다.

Checks:

- Alpaca credential source is explicit and redacted in `/health/config`
- live ingestor subscription plan is 20-symbol safe
- trade tier is active/watch/hot only
- Kafka raw topics receive controlled replay or market-hours events
- Python processor updates Redis latest/live keys
- closed bars reach processed Kafka/ClickHouse canonical path
- API/WebSocket delivers last-candle/latest changes
- Watch List and Hot Ranking update current price/change/traded value

If market is closed:

- run controlled replay/local trace
- record market-hours smoke as open operational item

### M8. Browser Verification

Goal:

- 실제 브라우저에서 Toss/HTS처럼 끊기지 않는 차트 제품처럼 동작하는지 확인한다.

Browser checks:

- open AAPL, MSFT, NVDA, TSLA, UNH, BRK.B, AMD
- switch `1m`, `5m`, `10m`, `1D`, `1W`, `1M`
- drag left multiple times
- older candles append before target floor
- no duplicate candle
- no time reversal
- no scale collapse
- overnight/extended sparse 1m candles do not visually break the chart
- regular-session missing ranges trigger bounded gapfill instead of display-only hiding
- no single-candle fake-ready chart
- no infinite spinner
- renderable chart is not replaced by empty backfill message
- Watch List default 10 renders
- Watch List is user-editable
- Hot Ranking Top10 renders
- Hot row click changes active chart
- search dropdown finds only the 20-symbol universe
- browser console has no chart/backfill errors

### M9. Optional Warm Cache Expansion

Goal:

- 운영자가 선택한 경우에만 frequently viewed symbols/ranges를 미리 데워 chart latency를 줄인다.

Rules:

- dry-run first
- enqueue bounded batches
- do not advance with unresolved failures
- record remaining chunks if stopped
- S3/ClickHouse에 이미 있는 logical chunk는 skip한다.
- warm cache는 lazy 6-year target의 보조 수단이지 필수 gate가 아니다.

### M10. Optional Full Historical Preload

Goal:

- 운영자가 비용/시간/rate limit을 승인한 경우에만 full historical preload를 수행한다.

Rules:

- same validation as M4/M9
- monitor Alpaca rate limits, S3 object counts, ClickHouse row counts, duplicate counts, queue lag
- stop and report exact remaining chunks if rate/cost/time risk becomes too high
- this is not required for normal chart browsing because lazy backfill can reach the 6-year target on demand

### M11. Team Handoff

Goal:

- 팀원이 frontend/agent 작업을 병합할 수 있게 기능 계약을 남긴다.

Checks:

- `alpaca-data-pipeline-plan/market-data-stabilization-share.md` matches this plan
- `alpaca-data-pipeline-plan/team-merge-guide.md` identifies market-data logic vs team-owned UI
- docs do not ask team to replay old S&P500, Hot Top20, or `2025-04` scoped plan
- final report lists:
  - completed gate
  - exact loaded ranges
  - exact unfinished ranges
  - tests passed
  - browser checks passed
  - open market-hours checks

## 8. Required Tests

Python:

```bash
PYTHONPATH=systems/market-data/shared:systems/api-server/pods/api-server/gops-backend .venv/bin/python -m pytest systems/market-data/tests/test_market_data_hardening.py -q
PYTHONPATH=systems/market-data/shared:systems/api-server/pods/api-server/gops-backend .venv/bin/python -m pytest systems/api-server/tests/test_market_data_query.py -q
```

Frontend:

```bash
cd apps/gops-frontend
npm run test:chart
npm run build
```

Runtime manifests:

```bash
docker compose config --quiet
kubectl kustomize infra/k8s/base
kubectl kustomize infra/k8s/overlays/aws
```

API smoke:

```text
GET /health/config
GET /api/charts/candles?symbol=AAPL&interval=1m&limit=120
GET /api/charts/candles?symbol=NVDA&interval=1m&limit=120
GET /api/charts/candles?symbol=BRK.B&interval=1D&limit=120
GET /api/charts/hot-symbols?limit=10
GET /api/charts/watchlist
GET /api/charts/symbols?query=brk
```

## 9. Known Risks To Keep Open

- Alpaca rate limits may slow or stop large optional historical preload jobs.
- Lazy 6-year backfill may expose provider rate limits if many users drag deep history at the same time.
- Display-only continuity can hide natural no-trade minutes visually, so it must be clearly separated from canonical data and excluded from storage/analytics.
- S3 object exists but manifest is stale or incompatible.
- ClickHouse table schema/ORDER BY may not match new canonical/feed/session requirements on existing volumes.
- Redis stale latest/live keys may hide canonical ClickHouse coverage.
- `BRK.B` normalization may differ across UI, API, S3, ClickHouse, and Redis.
- KST display, UTC storage, and New York session boundaries may make candles appear shifted.
- Split-adjusted history and unadjusted live prices must not be merged as the same canonical row.
- Concurrent backfill workers can race unless S3 keying and ClickHouse insert semantics are idempotent.
- Market closed local verification cannot prove actual live freshness.
- Team frontend changes may have replaced chart runtime assumptions; port data-contract hunk by hunk.

## 10. Prior Local Evidence

Recorded on 2026-07-01 KST for the previous local demo gate. Re-measure before making completion claims. This evidence proves the earlier 20-symbol canonical rebuild state, but it is not by itself proof of the new lazy 6-year browsing gate.

- Active S3 rebuild prefixes:
  - `S3_FINAL_PREFIX=market-data/rebuild-20260701/final`
  - `S3_MANIFEST_PREFIX=market-data/rebuild-20260701/manifest`
  - `S3_RAW_PREFIX=market-data/rebuild-20260701/raw/alpaca`
  - `S3_LIVE_PREFIX=market-data/rebuild-20260701/live`
- `/health/config` reports canonical `split`, `parquet`, ClickHouse/S3 canonical guards enabled, and no warnings.
- ClickHouse canonical serving rows:
  - `1D / v2 / split`: `15020` rows, `20` symbols, `2023-07-03T04:00:00Z` to `2026-06-30T04:00:00Z`
  - `1m / v2 / split`: `818475` rows, `20` symbols, `2026-04-01T08:00:00Z` to `2026-06-30T15:38:00Z`
  - total chart rows: `833495`
  - outside the 20-symbol universe: `0`
  - duplicate canonical `(symbol, interval, event_time)`: `0`
  - non-canonical rows in `chart_candles`: `0`
  - exact symbols for both `1D` and `1m`: `AAPL, AMD, AMZN, AVGO, BRK.B, COST, GOOGL, HD, JNJ, JPM, MA, META, MSFT, NFLX, NVDA, PG, TSLA, UNH, V, XOM`
- S3 object counts under the rebuild prefix:
  - `final/candles/interval=1D`: `60`
  - `final/candles/interval=1m`: `380`
  - `manifest`: `880`
- S3 canonical final object audit:
  - total final candle objects: `440`
  - parsed final candle objects: `440`
  - unparsed final candle objects: `0`
  - outside symbols: `0`
  - bad metadata: `0`
  - duplicate logical chunks: `0`
  - per-symbol counts: `1D=3`, `1m=19`
- S3 manifest audit:
  - manifest count: `880`
  - candle manifests: `440`
  - raw manifests: `440`
  - bad canonical candle manifests: `0`
  - missing object keys: `0`
  - zero-row candle manifests: `0`
  - outside symbols: `0`
- Redis audit:
  - `DBSIZE=935` after preload/API/browser activity
  - key prefix summary: `backfill=921`, `active=14`
  - no stale marker keys for `sp500`, `semiconductor`, `ADBE`, old semiconductor symbols, or old `2025-04` scoped plan
  - backfill stream has `pending=0`, `lag=0`, `XLEN=440`, and dead-letter length `0`
  - backfill stream symbol distribution is exactly `22` entries per 20-symbol universe member
  - backfill status/lock/latest keys are exactly `46` keys per 20-symbol universe member
  - active chart symbols are inside the 20-symbol universe
- Local `backfill-worker` has been recreated with explicit `gops20`, rebuild S3 prefixes, `HISTORICAL_ADJUSTMENT=split`, `S3_PROCESSED_FORMAT=parquet`, and `S3_REQUIRE_CANONICAL_PROCESSED_CANDLES=true`.
- Phase 3 dry-run for `1m` recent 1 year (`2025-07-01T00:00:00Z` to `2026-07-01T00:00:00Z`) estimates:
  - `1460` chunks
  - `73` chunks per symbol
  - about `4,838,400` rows
  - `1460` raw objects
  - `1460` processed objects
  - `2920` manifest entries
  - `maxEnqueue=100`, `maxBacklog=1000`, `force=false`
- Verification after compose worker env hardening:
  - `git diff --check`: passed
  - `docker compose config --quiet`: passed
  - `systems/market-data/tests/test_market_data_hardening.py`: `150 passed, 8 subtests passed`
  - `systems/api-server/tests/test_market_data_query.py`: `34 passed`
  - `npm run test:chart`: passed
  - `npm run build`: passed
  - `kubectl kustomize infra/k8s/base`: passed
  - `kubectl kustomize infra/k8s/overlays/aws`: passed
  - API smoke: AAPL/NVDA `1m`, BRK.B `1D`, Watch List, Hot Ranking, and BRK.B symbol search passed
- Browser smoke:
  - initial NVDA `1m` chart rendered candles, volume, and moving averages with no empty-chart message and no console warnings/errors
  - NVDA interval switching passed for `5m`, `10m`, `1D`, `1W`, `1M`, and back to `1m`
  - left-pan controls kept the chart renderable with no target-boundary or coverage error
  - Hot Ranking Top10 was visible, and selecting the TSLA row changed the active chart to TSLA
  - search dropdown for `BRK` showed `BRK.B / Berkshire Hathaway Inc. Class B`
  - search dropdown did not show out-of-universe `ADBE`

Do not enqueue Phase 3 or Phase 4 automatically without operator confirmation because they are large Alpaca/S3 jobs.

## 11. Prior Demo Gate Audit

This audit belongs to the previous local demo objective. Keep it as evidence, but do not use it to claim the lazy 6-year product gate.

Objective completion criteria from `/Users/heejunkim/.codex/attachments/90c4d2c4-8adb-4177-b18b-55cf7b7df224/goal-objective.md`:

| Requirement | Current evidence | Status |
| --- | --- | --- |
| ClickHouse `chart_candles` clean reset 완료 | Active serving table contains `833495` rows, exactly the 20-symbol universe, `0` outside rows, `0` non-canonical rows, and `0` duplicate timestamp keys. This proves no legacy chart rows remain in the serving table after the rebuild. | Proven locally |
| Redis clean reset 완료 | Redis is no longer empty because preload/API/browser activity repopulated runtime state, but the current keyspace contains only current rebuild `backfill` and `active` state, no legacy/stale markers, no dead-letter jobs, and only 20-symbol universe members in stream/status keys. This proves no stale Redis state is contaminating the clean rebuild. | Proven locally |
| 20개 universe 단일 설정 완료 | compose/k8s/env/API tests show `gops20`; Hot/Watch/Search stay inside the 20-symbol universe. | Proven locally |
| S3 canonical dedup 구현 완료 | S3 final object audit parsed all `440` canonical candle objects, found `0` duplicate logical chunks, `0` bad metadata, `0` outside symbols, and deterministic `interval/symbol/range/adjustment/canonical` keys. Manifest audit found `0` bad candle manifests. | Proven locally |
| `1D` 3년 20개 적재 완료 | ClickHouse `1D/v2/split` has `15020` rows across `20` symbols, `2023-07-03T04:00:00Z` to `2026-06-30T04:00:00Z`; S3 `1D` object count is `60`. | Proven locally |
| `1m` 최근 3개월 20개 적재 완료 | ClickHouse `1m/v2/split` has `818475` rows across `20` symbols, `2026-04-01T08:00:00Z` to `2026-06-30T15:38:00Z`; S3 `1m` object count is `380`. | Proven locally |
| API는 `v2/split`만 반환 | ClickHouse non-canonical row count is `0`; API smoke for AAPL/NVDA/BRK.B is renderable; tests cover canonical filters. | Proven locally |
| 브라우저 차트 정상 확인 | Browser smoke passed for rendering, interval switching, left-pan, Hot Ranking, and BRK.B search. | Proven locally |
| 재현 가능한 짧은 실행 문서 작성 | This plan contains the runbook, current evidence, tests, and Goal Mode prompt. | Proven |

The objective file's explicit Completion Criteria match the local demo gate above. Phase 3 and Phase 4 remain documented follow-up expansion steps, not blockers for this Goal's completed demo gate unless the operator explicitly selects the full rebuild gate.

## 12. Acceptance Gates

### Minimum Local Demo Gate

This gate is enough for a stable local demo.

- 20-symbol config active.
- ClickHouse chart data reset for rebuild.
- Redis market-data state reset for rebuild.
- S3 target prefix fresh or explicitly isolated.
- S3 duplicate smoke passes.
- Initial chart render uses bounded visible windows only.
- Representative visible windows can be served from Redis/ClickHouse or filled by bounded S3-first jobs.
- Left-pan lazy backfill works for `1m`, `5m`, `10m`, `1D`, `1W`, and `1M`.
- Extended-hours/overnight sparse candles render continuously without storing display-only candles as canonical data.
- S3 -> ClickHouse materialize smoke passes.
- API smoke passes for representative symbols and intervals.
- Browser smoke passes for rendering, left-pan, Watch List, Hot Ranking, and search.
- Automated tests pass.

### Lazy 6-Year Product Gate

This gate is required before claiming Toss/HTS-style 6-year browsing behavior.

- Minimum Local Demo Gate passed.
- Initial chart render never requests a full 6-year range.
- Left-pan can keep requesting older bounded pages until the 6-year/listing target floor.
- `1m`, `5m`, `10m`, `1D`, `1W`, and `1M` all use source-interval lazy backfill.
- ClickHouse miss uses S3 materialize before Alpaca.
- S3 miss uses Alpaca only for bounded page/gap ranges.
- Extended-hours/overnight sparse candles render continuously without polluting canonical storage.
- Regular-session missing data still triggers bounded gapfill.
- No duplicate canonical S3 logical chunks.
- ClickHouse duplicate timestamp/date checks are zero.
- Backfill uses S3 before Alpaca.
- Alpaca is called only for missing target-range data not present in S3.
- Browser checks pass again after full load.

Close this Goal once the selected gate is proven and recorded. Do not claim full historical preload unless an operator explicitly selects and verifies it.

## 13. Goal Mode Prompt

```md
PLEASE IMPLEMENT THIS PLAN:

Implement `alpaca-data-pipeline-plan/s3-first-backfill-goal-plan.md`.

Scope is market-data/chart only. Preserve team-owned frontend layout, agent, and order code unless a market-data contract requires a small connection change.

Use exactly these 20 symbols:
AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, BRK.B, JPM, UNH, V, XOM, MA, AVGO, PG, COST, HD, JNJ, NFLX, AMD.

Hard requirements:
- Historical chart serving uses only `canonical_version='v2'` and `price_adjustment='split'`.
- Reset ClickHouse chart-serving data and Redis market-data state before rebuild.
- Use a fresh or explicitly initialized S3 rebuild prefix.
- Store canonical S3 parquet chunks with deterministic duplicate-safe keys.
- Check S3 before Alpaca for every historical gap.
- Materialize S3 into ClickHouse before serving.
- Chart API reads Redis/ClickHouse only; it must not synchronously scan S3 or call Alpaca.
- Do not load 6 years of data at chart initial render.
- Support lazy 6-year browsing for `1m`, `5m`, `10m`, `1D`, `1W`, and `1M`.
- Left-pan uses `before` cursor and bounded page/gap jobs.
- ClickHouse miss uses S3 materialize first; only S3 miss uses Alpaca.
- Keep extended-hours/overnight sparse charts visually continuous without storing synthetic candles as canonical data.
- Do not trigger full-range `1m` backfill from chart initial render.
- Keep Watch List default 10 and Hot Ranking Top10 inside the 20-symbol universe.
- Verify day-market/live path with controlled replay if the market is closed.

Proceed milestone by milestone:
1. Re-measure current state.
2. Make the smallest coherent change.
3. Add focused tests.
4. Run relevant tests.
5. Record only market-data/chart evidence and risks.

Stop and report if a destructive action, large Alpaca load, S3 prefix change, or full 1m expansion requires operator choice.

Do not finish until the selected acceptance gate passes with automated tests, S3 dedup smoke, S3->ClickHouse materialize smoke, API smoke, and browser verification.
```
