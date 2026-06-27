# GOPS Design Concept

이 문서는 앞으로 우리 코드와 팀원 코드가 같은 범위를 수정한 뒤 Codex로 병합할 때 참고할 설계 판단 기록이다.

나중에 병합할 때는 우리 쪽 `DesignConcept.md`, 팀원 쪽 `DesignConcept.md`, 실제 코드 diff를 함께 비교한다. 단순히 한쪽 코드를 우선하지 않고, 무엇을 위해 어떤 선택을 했는지 확인한 뒤 GOPS 전체 목표에 가장 맞는 구조를 선택한다.

## 운영 원칙

- 구현 변경을 할 때마다 이 문서도 함께 갱신한다.
- 기능 목록보다 설계 의도를 기록한다.
- "무엇을 바꿨는지", "왜 바꿨는지", "어떻게 바꿨는지", "다음 병합 때 주의할 점"을 남긴다.
- 팀원 코드와 같은 범위를 수정했다면 충돌 가능성과 선택 기준을 명시한다.
- 임시 구현, scaffold, placeholder는 최종 설계처럼 적지 않는다.
- 테스트 통과 여부뿐 아니라 병합 판단에 필요한 책임 경계와 데이터 흐름을 남긴다.

## 현재 설계 기준선

### 2026-06-28: Brothers 브랜치에서 kimheejun market data stack 통합

무엇을 바꿨나:

- `kimheejun` 브랜치의 Alpaca 기반 market data stack, Docker/AWS 배포 구조, Redis/ClickHouse/ClickHouse serving 계층을 `Brothers` 브랜치에 병합했다.
- 기존 루트 `frontend/`, `backend/` 구조를 제거하고 Kim 쪽 monorepo 구조를 기준으로 정리했다.
- 우리 프론트엔드와 차트 런타임은 `apps/gops-frontend/`, `apps/chart-engine/`로 이동했다.
- backend API는 `services/07-api-websocket/gops-backend/` 기준으로 정리했다.
- market data provider는 `packages/alfaka/`의 Redis/ClickHouse/Alpaca serving 계층을 통해 연결하도록 맞췄다.
- Docker, infra, local/AWS script는 Kim 쪽 구조를 수용했다.

왜 바꿨나:

- 팀원이 실제 Alpaca data ingestion과 AWS/Docker 배포 구조를 먼저 진행하고 있었기 때문에, chart/frontend가 더 이상 dummy data 중심으로 발전하면 이후 병합 비용이 커진다.
- GOPS 차트는 실제 시장 데이터 contract에 맞춰야 하며, frontend가 임의 candle이나 임의 관심종목을 만들어 보여주면 제품 신뢰성이 깨진다.
- 앞으로 팀원과 같은 범위를 수정하더라도, data ingestion/serving 책임은 backend/provider 쪽에 두고, chart frontend는 정규화된 API/WebSocket contract만 소비하는 편이 병합과 확장에 유리하다.

어떻게 바꿨나:

- 차트 엔진은 `apps/chart-engine/src/`에 두고 React hook과 UI component는 `apps/gops-frontend/src/`에 둔다.
- `apps/gops-frontend`는 `/api/charts/candles`, `/api/charts/symbols`, `/api/market/symbols/search`, chart WebSocket을 통해 data를 받는다.
- frontend-local candle fallback과 hardcoded watchlist fallback은 제거했다.
- 검색, 관심종목, 비교 chart 후보, Agent chart context의 symbol universe는 backend가 내려주는 symbol data를 기준으로 구성한다.
- chart header의 회사명/시장 정보는 backend watchlist metadata를 우선 사용하고, 없을 때만 symbol 자체를 최소 fallback으로 표시한다.
- LLM chart proposal은 frontend state를 직접 수정하지 않고 `ChartCommand` contract를 통해 적용한다.
- mock LLM fallback도 configured Alpaca symbols 기준으로 비교 후보를 고르게 정리했다.

현재 선택한 책임 경계:

- Alpaca 수집, symbol universe, Redis/ClickHouse serving, backfill, market status는 `packages/alfaka/`와 `services/07-api-websocket/gops-backend/` 책임이다.
- Bento Grid, panel catalog, chart interaction, Canvas rendering, chart-local history는 `apps/gops-frontend/`와 `apps/chart-engine/` 책임이다.
- shared chart command/schema는 `shared/chart-contract/`를 기준으로 유지한다.
- AI Agent endpoint는 현재 FastAPI 내부 scaffold지만, OpenAI key와 proposal 생성은 frontend로 노출하지 않는다.

병합 때 주의할 점:

- 팀원 코드가 frontend에 fixed symbol list나 demo candle fallback을 되살리면, 실제 provider contract를 우선해야 한다.
- 팀원 코드가 market data API shape를 바꿨다면 frontend adapter를 맞추되, Canvas/chart runtime이 provider 원본 포맷에 직접 의존하지 않게 유지한다.
- Docker/AWS 구조는 Kim 쪽 방향을 존중하되, chart-engine과 gops-frontend 분리는 유지하는 편이 좋다.
- `docs/`는 현재 `.gitignore`에서 local planning/reference artifact로 무시된다. 팀 공유가 필요한 설계 판단은 이 파일이나 tracked README에 남긴다.

검증:

- `npm run build` in `apps/gops-frontend`
- `npm run test:chart` in `apps/gops-frontend`
- `.venv/bin/python -m compileall packages services/07-api-websocket/gops-backend/app tests`
- `.venv/bin/python -m unittest discover -s services/07-api-websocket/gops-backend/tests`
- `env PYTHONPATH=packages:services/07-api-websocket/gops-backend .venv/bin/python -m unittest discover -s tests`
- `git diff --check`
- `docker compose config --quiet`

### 2026-06-28: ALPACA_UNIVERSE와 ALPACA_SYMBOLS 책임 분리

무엇을 바꿨나:

- `ALPACA_UNIVERSE`를 검색/검증 가능한 후보군으로 정의했다.
- `ALPACA_SYMBOLS`를 항상 수집할 Alpaca subscription 목록이자 frontend Watch List 기본 seed로 정의했다.
- 현재 정식 universe는 `semiconductor-100` 하나로 두고, 기본 seed는 `NVDA,AMD,AVGO,TSM,ASML,AMAT,MU` 7개로 두었다.
- `/api/charts/symbols`는 `ALPACA_SYMBOLS` seed를 내려주고, `/api/market/symbols/search`는 `ALPACA_UNIVERSE` 후보군을 검색하도록 분리했다.

왜 바꿨나:

- universe 전체를 기본 구독하면 Alpaca/Kafka/Redis/ClickHouse 부하가 커지고, 사용자가 의도하지 않은 수집이 발생한다.
- 반대로 watch list 7개만 검색 후보로 쓰면 사용자가 universe 안의 다른 반도체 종목을 찾거나 backfill 요청하는 흐름이 막힌다.
- 검색 후보군, 기본 수집 대상, 사용자 watch list는 비슷해 보이지만 운영 비용과 UX 책임이 다르다.

어떻게 바꿨나:

- `config/market-data-request.json`에 `defaultSeedSymbols`를 추가해 `defaultSymbols` universe와 seed를 분리했다.
- Alpaca 구독 helper는 `ALPACA_SYMBOLS`가 비어 있거나 universe 이름이면 실패하며, universe 전체를 자동 구독하지 않는다.
- `ALFAKA_REQUEST_CONFIG=config/market-data-request.json` 같은 상대경로는 현재 작업 디렉터리가 아니라 repo root 기준 config도 찾도록 보강했다.
- Symbol Registry fallback은 `ALPACA_SYMBOLS`가 아니라 `ALPACA_UNIVERSE`를 기준으로 검색/상세 후보를 만든다.
- Redis/ClickHouse에 symbol metadata가 아직 없어도 default seed와 주요 검색 후보는 config의 `symbolMetadata`로 회사명/거래소 fallback을 제공한다.
- frontend는 watch list seed가 처음 로드될 때 사용자가 아직 종목을 고르지 않았다면 첫 seed symbol을 active chart에도 적용한다.
- frontend는 기존처럼 `/api/charts/symbols`를 초기 Watch List로 쓰고, 검색 후보는 `/api/market/symbols/search`를 통해 backend universe를 따른다.
- frontend symbol search submit은 React state만 믿지 않고 form/input의 실제 입력값을 읽는다. `datalist`가 붙은 입력에서 Enter가 후보 선택처럼 소비될 수 있으므로, Enter keydown도 명시적인 검색 의도로 처리한다.
- Watch List row는 `data-symbol`과 명확한 `aria-label`을 둔다. 이후 browser regression이 텍스트 순서나 회사명 formatting에 덜 의존하게 하기 위함이다.
- Panel Catalog item은 `data-panel-catalog-type`과 명확한 `aria-label`을 둔다. drag/drop 검증은 패널 제목 텍스트가 아니라 panel type contract를 기준으로 해야 한다.

병합 때 주의할 점:

- 팀원 코드가 `ALPACA_SYMBOLS`를 검색 universe처럼 쓰거나, env 미설정 시 `defaultSymbols` 전체를 자동 구독하는 경로를 되살리면 안 된다.
- `ALPACA_UNIVERSE=semiconductor-100`은 검색/검증 후보군이고, `ALPACA_SYMBOLS=NVDA,AMD,AVGO,TSM,ASML,AMAT,MU`는 기본 수집/watch list seed다.
- 향후 `nasdaq-all` 같은 universe를 추가하더라도 전체 자동 구독은 별도 명시 정책 없이는 금지한다.
- 검색 submit 경로를 바꿀 때는 버튼 클릭과 Enter 제출이 같은 symbol resolution 경로를 지나야 한다.

검증:

- `ALPACA_UNIVERSE=semiconductor-100`, `ALPACA_SYMBOLS=NVDA,AMD,AVGO,TSM,ASML,AMAT,MU` 기준 unit test를 추가한다.
- `ALPACA_SYMBOLS=semiconductor-100`은 ticker validation에서 실패해야 한다.
- `rg "ALPACA_UNIVERSE|ALPACA_SYMBOLS"`로 env 사용 경로가 새 책임 분리를 따르는지 정적 확인한다.

### 2026-06-28: 차트 데이터 empty 상태와 WebSocket control event 처리

무엇을 바꿨나:

- WebSocket `HEARTBEAT`, `MARKET_STATUS_UPDATE`, `VOLUME_PROFILE_BINS_UPDATE`, `ERROR`를 candle event와 분리했다.
- `HEARTBEAT`는 stream error가 아니라 연결 유지 신호로 처리한다.
- chart snapshot이 `dataStatus=empty`, `canBackfill=true`를 반환하면 chart panel이 `/api/charts/backfill`을 명시적으로 요청한다.
- backfill이 성공하면 같은 symbol/timeframe snapshot을 다시 불러와 차트를 그린다.

왜 바꿨나:

- 이전에는 heartbeat payload를 candle event로 파싱하다 실패해서 `streamStatus=error`가 되었고, LLM이 계속 “차트 스트림 상태가 error”라고 답했다.
- `ALPACA_SYMBOLS`는 항상 구독할 seed이지 historical candle을 이미 보유한다는 뜻은 아니다. 차트를 그리려면 empty snapshot에서 backfill 요청으로 데이터 준비 흐름이 이어져야 한다.
- GET `/api/charts/candles`에 side effect를 넣지 않고, chart panel이 `canBackfill` contract를 보고 POST `/api/charts/backfill`을 호출하게 했다.

어떻게 바꿨나:

- frontend WebSocket message handler가 control payload를 먼저 판별한 뒤 candle event만 `normalizeCandleEvent`로 보낸다.
- chart runtime에 `chart.data.status` action을 추가해 terminal backfill 상태를 표현할 수 있게 했다.
- `apps/chart-engine/src/backfill.ts`에 backfill 요청 조건과 status payload normalization을 분리했다.
- chart panel은 symbol/timeframe별 중복 backfill 요청을 막고, queued/running은 내부 polling으로 따라가며, succeeded 시 snapshot reload를 수행한다.
- chart panel의 on-demand backfill은 frontend가 임의로 정한 ticker 전체가 아니라 backend watch list/search를 통해 확인된 eligible symbol에만 허용한다. 초기 레이아웃의 오래된 기본값이나 universe 밖 ticker가 Alpaca 요청을 만들면 안 된다.
- chart-engine의 기본 chart symbol은 현재 `ALPACA_SYMBOLS` sample seed의 첫 종목인 `NVDA`로 통일한다. `AAPL` 같은 이전 scaffold 기본값이 seed 로딩 전에 snapshot/WebSocket 요청을 만들면 market-data universe와 화면 기준선이 어긋난다.
- backfill 가능한 empty snapshot은 사용자에게 "No candle data"로 보여주지 않고 chart 준비 상태로 표시한다. empty는 최종 결론이 아니라 데이터를 준비하기 전의 중간 상태일 수 있기 때문이다.

병합 때 주의할 점:

- heartbeat/control event를 candle parsing 실패로 처리하는 경로를 되살리면 안 된다.
- `/api/charts/candles` GET이 암묵적으로 backfill을 생성하게 만들지 않는다. 데이터 조회와 백필 요청은 분리한다.
- seed symbol 전체를 무조건 한 번에 backfill하는 방식은 Alpaca/API 비용과 부하 정책이 정해지기 전까지 피한다. 현재 기준은 chart demand 기반 on-demand backfill이다.
- on-demand backfill도 backend universe/search contract 밖의 symbol에는 실행하지 않는다. 사용자 입력 검증, 검색 후보, 백필 가능 대상은 같은 backend symbol registry를 기준으로 맞춘다.
- chart 기본값을 바꿀 때는 `DEFAULT_CHART_SYMBOL`, `.env.example`의 `ALPACA_SYMBOLS` 첫 종목, 초기 watch list seed 적용 흐름을 함께 확인한다.
- `No candle data is available for this symbol and interval.`은 backfill이 불가능하거나 실패한 terminal 상태에만 사용자에게 노출한다.

검증:

- heartbeat는 `isRealtimeControlPayload`에서 true이고, candle event normalize 대상이 아니다.
- empty/canBackfill/not_requested 상태에서만 frontend가 backfill 요청을 시작한다.
- browser에서 backfill 후 NVDA candle chart가 실제로 렌더링되고, stream error 문구 없이 Agent가 visible candle context를 읽어야 한다.

### 2026-06-28: Watch List 편집 책임을 차트 패널 별 버튼으로 이동

무엇을 바꿨나:

- 오른쪽 Watch List 패널은 항목 선택과 표시만 담당한다.
- Watch List 추가/삭제는 차트 패널 제목란의 별 버튼으로만 수행한다.
- Watch List에 포함된 차트 심볼은 검정색 채움 별, 포함되지 않은 심볼은 외곽선 별로 표시한다.
- 검색창의 native `datalist` 의존을 제거하고, 커스텀 dropdown으로 검색 가능한 ticker 목록을 보여준다.
- `/api/market/symbols/search?q=&limit=100`은 현재 universe 후보를 반환하도록 바꿨다.

왜 바꿨나:

- Watch List 패널 내부의 추가/삭제 버튼과 차트 패널의 현재 심볼 상태가 따로 놀면 multi-chart 환경에서 어떤 차트를 추가/삭제하는지 모호해진다.
- Watch List membership은 사용자가 보고 있는 차트 심볼에 대한 명시적 즐겨찾기 상태로 보는 편이 자연스럽다.
- 브라우저 native datalist는 표시와 상호작용이 제어하기 어려워, GOPS UI 기준에 맞는 ticker dropdown을 직접 관리해야 한다.

어떻게 바꿨나:

- `watchlistSymbols`는 App의 단일 상태로 유지한다.
- 차트 패널 헤더의 별 버튼은 해당 chart document의 현재 symbol을 기준으로 `watchlistSymbols` membership을 토글한다.
- 추가 시에는 backend search/watchlist로 확보한 metadata를 우선 사용하고, 없으면 symbol fallback metadata를 사용한다.
- 검색 dropdown은 현재 입력값이 있으면 symbol 문자열에 입력값을 포함하는 후보만 보여준다.

병합 때 주의할 점:

- 오른쪽 Watch List에 별도 add/remove 버튼을 되살리지 않는다.
- 여러 chart panel이 있을 때 watchlist 토글 대상은 active selection이 아니라 별 버튼이 눌린 chart panel의 symbol이다.
- 빈 검색어의 search API는 universe dropdown 용도이므로 seed watch list와 혼동하지 않는다.

검증:

- `npm run build --prefix apps/gops-frontend`
- `npm run test:chart --prefix apps/gops-frontend`
- `env PYTHONPATH=packages:services/07-api-websocket/gops-backend .venv/bin/python -m unittest discover -s tests -p 'test_market_data_hardening.py'`
- `env PYTHONPATH=packages:services/07-api-websocket/gops-backend .venv/bin/python -m unittest services/07-api-websocket/gops-backend/tests/test_market_data_query.py`

## 앞으로 갱신할 때 쓸 형식

### YYYY-MM-DD: 변경 제목

무엇을 바꿨나:

- 

왜 바꿨나:

- 

어떻게 바꿨나:

- 

병합 때 주의할 점:

- 

검증:

- 
