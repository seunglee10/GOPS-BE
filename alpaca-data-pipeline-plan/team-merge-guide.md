# Team Merge Guide For Market Data Stabilization

팀원들이 에이전트와 프론트엔드 작업을 현재 코드보다 많이 진행한 상태를 전제로 한다. 이 문서의 목적은 그 작업을 보존하면서, 이번 Goal에서 안정화한 시장데이터 기능 계약만 정확히 병합하는 것이다.

## 병합 원칙

- 에이전트, 레이아웃, 패널 구성, 시각 디자인, 사용자 흐름은 팀원 브랜치를 우선한다.
- 이번 브랜치의 프론트엔드 파일을 통째로 덮어쓰지 않는다.
- 시장데이터 backend/platform/shared contract는 부분 적용하면 깨지기 쉬우므로 하나의 기능 단위로 병합한다.
- 충돌이 생기면 UI 구조는 팀원 쪽을 살리고, 데이터 로딩/상태/Backfill/Hot/Watch 계약만 필요한 hunk로 옮긴다.
- fake market candle 생성은 병합하지 않는다.

## 먼저 보존해야 하는 기능 단위

### Market Data Core

다음 영역은 이번 안정화의 핵심이다. 팀원 브랜치와 충돌하더라도 기능 계약을 우선 보존한다.

- `systems/market-data/shared/alfaka/alpaca/`
  - feed profile, websocket collector, subscription request config.
- `systems/market-data/shared/alfaka/common/`
  - raw/processed message schema, feed/session metadata.
- `systems/market-data/shared/alfaka/streaming/`
  - Python processor transform, Redis live candle/latest state, heartbeat.
- `systems/market-data/shared/alfaka/storage/`
  - ClickHouse loader, S3 materializer, raw/processed manifest handling.
- `systems/market-data/shared/alfaka/serving/`
  - ClickHouse provider, symbol registry, hot ranking, DTO contract.
- `systems/market-data/shared/alfaka/backfill/`
  - Redis Streams queue, bounded backfill, GapFill, replay/correction, queue metrics.
- `systems/market-data/jobs/`
  - initial-load, coverage-repair, smoke/operational entrypoints.
- `systems/market-data/config/`
  - `gops20` universe request config for the current 20-symbol rebuild.
- `systems/market-data/tests/`
  - hardening, realtime boundary, GapFill, materialize, queue tests.

### API Server

다음 API behavior는 프론트엔드와 운영 검증의 기준이다.

- candle response metadata:
  - `dataStatus`
  - `repairStatus`
  - `coverage.renderable`
  - `targetRangeFrom`
  - `storedCandleCount`
  - `hasMoreBefore`
  - `feedProfile`
  - `marketSession`
- backfill route/status/queue metrics.
- `GET /api/charts/hot-symbols`.
- `GET /api/charts/watchlist`.
- `PUT /api/charts/watchlist`.
- previous close/change percent calculation.
- 20-symbol `gops20` symbol search and filtering for the current rebuild.

### Platform And Runtime

다음은 로컬과 AWS 가정 배포 계약에 직접 영향을 준다.

- `infra/clickhouse/initdb/01-market-data.sql`
- `docker-compose.yml`
- `infra/k8s/base`
- `infra/k8s/overlays/aws`
- `.env.example`
- `docs/ENVIRONMENT.md`
- `systems/market-data/README.md`
- `scripts/local/check-live-path.py`

기존 ClickHouse volume을 쓰는 환경은 feed/session-aware `ORDER BY` 적용 여부를 확인한다. 새 schema가 필요한 경우 table rebuild 계획을 별도로 둔다.

## 프론트엔드 병합 기준

팀원 프론트엔드가 더 최신이면 그 구조를 유지한다. 이번 작업에서 필요한 부분은 아래 데이터 계약이다.

### Chart Engine

다음은 비교 후 필요한 타입/런타임 로직을 포팅한다.

- `apps/chart-engine/src/types.ts`
- `apps/chart-engine/src/marketDataAdapter.ts`
- `apps/chart-engine/src/runtime.ts`
- `apps/chart-engine/src/backfill.ts`
- `apps/chart-engine/src/intervals.ts`
- `apps/chart-engine/src/time.ts`

보존할 동작:

- partial but renderable snapshot은 차트를 표시한다.
- non-renderable real gap은 repair/backfill 상태를 유지한다.
- drag-left pagination은 bounded older window를 요청한다.
- backfill success 이후 같은 candles API path로 refetch한다.
- duplicate/provisional candle merge는 timestamp와 interval source 계약을 따른다.
- `feedProfile`과 `marketSession` metadata를 버리지 않는다.

### GOPS Frontend

다음 파일은 팀원 변경과 충돌 가능성이 높다. 통째로 덮어쓰지 말고 기능 hunk만 옮긴다.

- `apps/gops-frontend/src/App.tsx`
- `apps/gops-frontend/src/components/ChartPanel.tsx`
- `apps/gops-frontend/src/components/WatchListPanel.tsx`
- `apps/gops-frontend/src/components/HotRankingPanel.tsx`
- panel/layout/style 관련 파일 전반.

보존할 동작:

- Watch List는 사용자 편집 가능해야 한다.
- 기본 Watch List는 첫 사용자 seed로만 사용한다.
- Watch List row는 전일 종가 대비 등락률과 현재가를 보여준다.
- Hot Ranking은 20개 universe 안의 거래대금 Top10이며 별도 패널 분류로 존재한다.
- Hot Ranking row 선택은 active chart symbol을 바꾼다.
- search dropdown은 현재 `gops20` 20개 universe를 기준으로 검색된다.
- chart loading 중 오래 걸리는 backfill이 renderable chart 표시를 막지 않는다.

팀원 브랜치에 새로운 패널 시스템이나 에이전트 UI가 있다면 그 구조를 살리고, 위 API/runtime 호출부만 연결한다.

## 권장 병합 순서

1. 팀원 브랜치에서 현재 테스트와 빌드 상태를 먼저 기록한다.
2. market-data shared/backend 코드를 기능 단위로 병합한다.
3. ClickHouse schema, compose, k8s, env contract를 병합한다.
4. API server candle/backfill/hot/watch contract를 병합한다.
5. Python tests와 API tests를 먼저 통과시킨다.
6. chart-engine 타입과 runtime contract를 병합한다.
7. gops frontend는 팀원 UI를 유지하면서 ChartPanel, Watch List, Hot Ranking의 데이터 로직만 수동 포팅한다.
8. frontend chart tests와 build를 통과시킨다.
9. local compose/API/browser smoke를 수행한다.

## 충돌 판단표

| 충돌 위치 | 기본 선택 | 이유 |
| --- | --- | --- |
| Agent orchestration/UI | 팀원 브랜치 | 이번 Goal의 핵심 범위가 아님 |
| Panel layout/design | 팀원 브랜치 | 팀원이 더 최신 UI를 진행 중 |
| Chart data loading/runtime | 시장데이터 안정화 계약 | 빈 차트, 중복 봉, backfill 지연 재발 방지 |
| Watch/Hot visual styling | 팀원 브랜치 | 표시 방식은 UI 소유 |
| Watch/Hot price/change/ranking logic | 시장데이터 안정화 계약 | 전일종가 기준, 거래대금 Top10 계약 |
| API response schema | 시장데이터 안정화 계약 | frontend/backend가 함께 의존 |
| S3/ClickHouse/Redis schema | 시장데이터 안정화 계약 | 부분 적용 시 runtime 장애 가능 |
| Env/secrets precedence | 시장데이터 안정화 계약 | AWS/local 충돌 방지 |

## 병합 후 필수 검증

Python:

```bash
PYTHONPATH=systems/market-data/shared:systems/api-server/pods/api-server/gops-backend .venv/bin/python -m pytest systems/market-data/tests/test_market_data_hardening.py systems/market-data/tests/test_realtime_boundary.py -q
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

Local smoke:

```bash
.venv/bin/python scripts/local/check-live-path.py --symbol AAPL
```

Browser smoke:

- AAPL, NVDA, TSLA `1m` chart가 비어 있지 않은지 확인한다.
- `5m`, `10m`, `1D`, `1W`, `1M` 전환이 정상인지 확인한다.
- drag-left 후 candle count가 증가하고 spinner가 멈추는지 확인한다.
- Watch List row의 현재가/등락률이 전일 종가 기준인지 확인한다.
- Hot Ranking Top10이 거래대금 기준으로 표시되고 row 선택이 active chart를 바꾸는지 확인한다.
- browser console warning/error가 없는지 확인한다.

## 병합 중 열어둘 위험

- Alpaca feed/account connection cap 때문에 live path가 배선 문제처럼 보일 수 있다.
- 정규장 종료 시간에는 실제 live candle update가 멈추는 것이 정상일 수 있다.
- 기존 ClickHouse volume은 새 `ORDER BY`를 자동 반영하지 않는다.
- S3 default prefix에 오래된 데이터가 남아 있으면 새 rebuild prefix와 섞이지 않도록 확인해야 한다.
- `1m` preload target은 현재 3년이며, 최근 3개월 -> 최근 1년 -> 전체 3년 순서로 확장한다.
- team frontend가 chart runtime을 크게 바꿨다면, 데이터 merge/de-dup/backfill trigger 테스트를 반드시 추가로 본다.
