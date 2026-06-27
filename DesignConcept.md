# GOPS Design Concept

이 문서는 조현호 쪽 `Brothers` 브랜치와 김희준 쪽 `kimheejun` 브랜치를 Codex로 병합하거나 다시 비교할 때 참고하는 설계 판단 기록이다.

코드를 수정할 때는 기능 목록만 적지 말고, 무엇을 위해 어떤 책임 경계를 선택했는지 남긴다. 이후 병합에서는 이 문서와 실제 코드 diff를 함께 보고 GOPS 전체 목표에 맞는 구현을 선택한다.

## 운영 원칙

- 변경할 때마다 이 문서도 함께 갱신한다.
- 임시 scaffold, placeholder, local-only artifact는 최종 설계처럼 기록하지 않는다.
- 팀원 코드와 같은 범위를 수정했다면 충돌 가능성과 선택 기준을 명시한다.
- 기능 외 프론트엔드 디자인 충돌은 `Brothers` 쪽 GOPS UI를 우선한다.
- 시장 데이터 ingestion, Redis/ClickHouse/S3 serving, backfill, AWS/Docker 배포 경계는 김희준 쪽 market-data stack의 의도를 우선 반영한다.

## 현재 책임 경계

- Alpaca 수집, symbol universe, Redis/ClickHouse serving, S3 archive/materialization, backfill, market status는 `packages/alfaka/`와 `services/07-api-websocket/gops-backend/` 책임이다.
- Bento Grid, panel catalog, chart interaction, Canvas rendering, chart-local history, Watch List UX는 `apps/gops-frontend/`와 `apps/chart-engine/` 책임이다.
- frontend는 candle을 임의 생성하지 않는다. chart data는 REST snapshot/range API와 WebSocket live/control event를 통해 받는다.
- LLM chart proposal은 frontend state를 직접 수정하지 않고 `ChartCommand` contract를 통해 적용한다.
- OpenAI key는 frontend에 노출하지 않는다. 현재는 FastAPI 내부 scaffold에서 읽지만, credential은 env/secret 경로로 관리한다.

## Canonical Market Data Paths

Live path:

```text
Alpaca/Kafka raw
  -> Flink/local processor
  -> Kafka processed topics
  -> Redis hot/recent cache
  -> ClickHouse chart_candles serving projection
  -> S3 processed/final/live durable artifacts
```

Historical/backfill path:

```text
Alpaca Historical REST
  -> S3 raw archive
  -> shared bar-to-candle transform/schema
  -> S3 processed/final
  -> ClickHouse chart_candles materialization
```

Recovery/rematerialization path:

```text
S3 processed/final
  -> S3 replay/materialization job
  -> ClickHouse chart_candles
```

## Decision Log

### 2026-06-28: Brothers baseline before kimheejun merge

무엇을 바꿨나:

- `ALPACA_UNIVERSE`와 `ALPACA_SYMBOLS`를 분리했다.
- `ALPACA_UNIVERSE`는 검색/검증 후보군, `ALPACA_SYMBOLS`는 기본 수집 및 Watch List seed로 정의했다.
- 현재 정식 universe는 `semiconductor-100`, 기본 seed는 `NVDA,AMD,AVGO,TSM,ASML,AMAT,MU`다.
- `/api/charts/symbols`는 seed Watch List를, `/api/market/symbols/search`는 universe search를 담당한다.
- WebSocket `HEARTBEAT`, `MARKET_STATUS_UPDATE`, `VOLUME_PROFILE_BINS_UPDATE`, `ERROR`를 candle event와 분리했다.
- chart snapshot이 `dataStatus=empty`, `canBackfill=true`를 반환하면 chart panel이 명시적으로 `/api/charts/backfill`을 요청한다.
- Watch List 추가/삭제는 오른쪽 Watch List 내부 버튼이 아니라 chart panel header의 별 버튼으로 수행한다.
- ticker 검색은 native datalist 대신 GOPS UI에 맞춘 custom dropdown으로 제어한다.

왜 바꿨나:

- universe 전체 자동 구독은 Alpaca/Kafka/Redis/ClickHouse 부하를 키우므로 금지해야 한다.
- Watch List seed와 검색 universe는 UX와 운영 비용이 다르다.
- heartbeat를 candle parsing 실패로 처리하면 chart stream이 거짓 error가 되고 LLM 답변도 오염된다.
- multi-chart 환경에서 Watch List 편집 대상은 선택된 panel이 아니라 별 버튼이 눌린 chart symbol이어야 명확하다.

병합 때 주의할 점:

- frontend-local candle fallback이나 hardcoded fake watchlist fallback을 되살리지 않는다.
- `ALPACA_SYMBOLS=semiconductor-100`처럼 universe 이름을 seed symbol list로 쓰는 것은 허용하지 않는다.
- `No candle data is available...`는 backfill 불가능/실패 terminal 상태에서만 보여준다.
- 검색 submit은 버튼 클릭과 Enter 제출이 같은 symbol resolution 경로를 지나야 한다.

### 2026-06-28: kimheejun market-data stabilization merge

무엇을 선택했나:

- 김희준 쪽의 24시간 기본 snapshot, 1년 stored range, interval-aware candle limit, range loading, moving average attachment, Redis/ClickHouse/S3 backfill 경로를 반영한다.
- 초기 차트는 최신 24시간 window를 요청하고, 사용자가 zoom out 또는 과거 구간 pan을 할 때 REST range loading으로 이전 candle을 확장한다.
- `1m` candle을 canonical serving 단위로 보고, `5m`/`10m`은 ClickHouse `1m`에서 aggregation해 serving할 수 있게 유지한다.
- REST `/api/charts/candles`는 snapshot/range loading을 담당하고, WebSocket은 live update/control event를 담당한다.
- Watch List 가격은 Redis latest/recent를 우선하고, 없으면 ClickHouse latest `1m` candle로 보완한다.
- OpenAI credential lookup은 process env 또는 repo-root `.env`, Docker/Kubernetes secret 경로를 따른다.
- 모바일 전용 stacking layout은 현재 프로젝트 결정과 맞지 않아 채택하지 않는다.

왜 선택했나:

- 주식 차트는 dummy shape가 아니라 실제 serving projection과 backfill 상태를 기준으로 검증해야 한다.
- 1년 데이터를 저장하더라도 첫 화면에 전부 압축해 보여주면 분석성이 떨어진다.
- historical data를 WebSocket bulk replay로 처리하면 live channel 책임이 흐려진다.
- Watch List의 `-- +0.00%` 같은 fake flat state보다 `No data`가 신뢰성에 맞다.

병합 때 주의할 점:

- GET `/api/charts/candles`에 암묵적 backfill side effect를 넣지 않는다.
- S3 raw 및 processed/final artifact를 backfill durable path에서 제거하지 않는다.
- moving average는 Redis/ClickHouse/backfill 어느 경로에서 온 snapshot이든 API-level로 사용 가능해야 한다.
- OpenAI key를 코드나 frontend bundle에 넣지 않는다.
- 모바일 대응은 별도 결정 전까지 구현 기준선이나 검증 완료 기준으로 삼지 않는다.

### 2026-06-28: Brothers and kimheejun merge policy

무엇을 바꿨나:

- 병합 전 `Brothers`의 미커밋 변경을 안전 커밋으로 고정했다.
- 병합 전 기준선을 `backup/Brothers-before-kimheejun-merge` 브랜치로 보존한다.
- `kimheejun`의 market-data 안정화 변경을 `Brothers`에 병합하되, push는 하지 않는다.

선택 기준:

- 디자인, panel interaction, chart header, Watch List star toggle, custom ticker dropdown은 `Brothers` UI를 우선한다.
- market-data storage/serving/backfill/API contract는 김희준 구현을 우선 반영한다.
- 둘 다 같은 문제를 해결한 경우, 더 명확한 책임 경계와 더 작은 coupling을 가진 구현을 선택한다.
- 병합 후 이 문서에 선택 이유와 남은 검증 항목을 남긴다.

## Verification Baseline

병합 안정화 시 우선 실행할 검증:

```sh
git diff --check
docker compose config --quiet
npm run build --prefix apps/gops-frontend
npm run test:chart --prefix apps/gops-frontend
.venv/bin/python -m compileall packages services/07-api-websocket/gops-backend/app tests
.venv/bin/python -m unittest discover -s services/07-api-websocket/gops-backend/tests
env PYTHONPATH=packages:services/07-api-websocket/gops-backend .venv/bin/python -m unittest discover -s tests
```

Runtime smoke:

- `/api/charts/symbols`가 `ALPACA_SYMBOLS` seed를 반환한다.
- `/api/market/symbols/search`가 `ALPACA_UNIVERSE` 후보를 반환한다.
- NVDA 외 seed symbol도 backfill 후 candle snapshot을 표시한다.
- LLM 답변에서 거짓 `stream error` 언급이 재발하지 않는다.
- Watch List star toggle, ticker dropdown, panel catalog, Ask Agent flow가 작동한다.
