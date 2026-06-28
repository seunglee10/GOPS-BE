# GOPS Design Concept Log

조현호와 김희준이 같은 코드베이스를 함께 수정할 때, 나중에 Codex가 병합 판단을 할 수 있도록 남기는 짧은 설계 판단 로그다.

## 작성 형식

### YYYY-MM-DD: 제목
- 변경:
- 판단:
- 유지할 계약:
- 검증:

로그는 길게 설명하지 않되, 미래의 병합자가 "무엇을 살려야 하는지"와 "왜 그렇게 판단했는지"를 알 수 있을 만큼 구체적으로 남긴다.

## Logs

### 2026-06-28: Brothers <- kimheejun 병합 기준선
- 변경:
  - `Brothers`의 로컬 작업을 먼저 커밋으로 고정하고, `backup/Brothers-before-kimheejun-merge` 브랜치로 병합 전 상태를 보존한 뒤 `origin/kimheejun`을 병합했다.
  - 프론트엔드 시각/상호작용은 조현호 쪽 구현을 기준으로 유지했다. 여기에는 Bento Grid, panel catalog, chart header, Watch List star toggle, custom ticker dropdown, chart panel의 명시적 Ask Agent reference 흐름이 포함된다.
  - 시장 데이터와 백필 흐름은 김희준 쪽 안정화 구현을 적극 반영했다. 여기에는 24시간 초기 snapshot, 1년 stored range, interval-aware candle loading, Redis/ClickHouse/S3 backfill 및 serving path, OpenAI credential 경계가 포함된다.
  - 모바일 전용 동작은 현재 제품 범위가 아니므로 병합 기준선에 포함하지 않았다.
- 판단:
  - UI 충돌은 기능 계약을 깨지 않는 한 현재 GOPS 프론트엔드 경험을 우선한다. 이유는 지금 사용자 검증과 차트/패널 UX가 이 화면 구조를 중심으로 진행되고 있기 때문이다.
  - market-data 충돌은 Redis/ClickHouse/S3 serving contract와 명시적 backfill 흐름을 우선한다. 이유는 차트가 더 이상 dummy shape가 아니라 실제 수집/저장/서빙 파이프라인과 맞아야 하기 때문이다.
  - GET `/api/charts/candles`는 snapshot/range 조회를 담당하고, 백필은 명시적 backfill 경로를 통해 다룬다. 암묵적 side effect가 섞이면 화면에서 "조회"와 "수집 요청"의 책임이 흐려진다.
  - frontend-local fake candle fallback은 되살리지 않는다. 데이터가 없을 때 사실처럼 보이는 차트를 그리는 것보다 loading/empty/backfill 상태를 정확히 드러내는 편이 신뢰성에 맞다.
- 유지할 계약:
  - `ALPACA_UNIVERSE`는 검색/검증 가능한 후보군이다.
  - `ALPACA_SYMBOLS`는 기본 수집 대상이자 초기 Watch List seed다.
  - REST `/api/charts/candles`는 historical snapshot/range loading을 담당한다.
  - WebSocket은 live update, reconnect gap-fill/control, live delta behavior를 담당한다.
  - Chart API는 serving data를 Redis와 ClickHouse에서 읽는다.
  - ClickHouse `chart_candles`는 serving projection이고, S3는 durable storage 및 replay/rematerialization 근거다.
  - OpenAI credential은 frontend bundle에 들어가면 안 된다.
- 검증:
  - `git diff --check`
  - `docker compose config --quiet`
  - `npm run test:chart --prefix apps/gops-frontend`
  - `npm run build --prefix apps/gops-frontend`
  - `.venv/bin/python -m compileall packages services/07-api-websocket/gops-backend/app tests`
  - `.venv/bin/python -m unittest discover -s services/07-api-websocket/gops-backend/tests`
  - `env PYTHONPATH=packages:services/07-api-websocket/gops-backend .venv/bin/python -m unittest discover -s tests`
  - 브라우저 `127.0.0.1:5174`에서 chart canvas가 white screen 없이 로드되고, 화면에 `sample-dev`/`synthetic`/`mock` 문구가 노출되지 않음을 확인했다.
  - 로컬 API `127.0.0.1:8000`의 `/api/charts/candles`, `/api/charts/symbols` 응답에 `isSynthetic` 필드가 없음을 확인했다.

### 2026-06-28: 문서와 reference 폴더 정리
- 변경:
  - 이전 참고 구현이 들어 있던 `ref/`를 삭제했다. 이 폴더에는 오래된 참고 앱뿐 아니라 `node_modules`, `.venv`, build output, local env 같은 현재 코드베이스와 분리해야 할 산출물이 포함되어 있었다.
  - `docs/`에서는 `docs/spec/`만 남기고 planning/process/architecture 문서를 삭제했다. 남은 spec 문서는 다른 팀원의 원래 계획을 가늠하기 위한 참고 자료로 유지한다.
  - `AGENTS.md`를 현재 2인 협업 기준으로 줄였다. 앞으로 작업 전 현재 코드와 `DesignConcept.md`를 확인하고, 구현 변경 후 이 로그를 갱신하도록 정리했다.
  - `.gitignore`에서 `/docs/`, `/ref/`, `/AGENTS.md`, `/assets/` ignore를 제거했다. `docs/spec/`, `AGENTS.md`, `assets/agent-icons/`가 협업에 필요한 파일로 드러나야 하기 때문이다.
- 판단:
  - 현재 협업의 기준선은 코드, 테스트, runtime contract, 그리고 이 DesignConcept 로그다. 오래된 계획 문서가 계속 남아 있으면 구현 중 Codex가 현재 코드보다 과거 계획을 우선하는 혼란을 만들 수 있다.
  - `docs/spec/`는 삭제하지 않는다. 구현의 절대 제약은 아니지만, 차트/시장데이터/주문/인프라 등 다른 팀원이 기대했던 방향과 현재 구현이 크게 달라질 때 경고 신호로 가치가 있다.
  - `assets/agent-icons/`는 실제 frontend에서 쓰는 리소스이므로 reference artifact가 아니라 구현 자산으로 취급한다.
  - 문서 삭제 자체가 동작을 바꾸면 안 된다. 삭제 대상이 runtime import, build script, test, API contract에 연결되어 있지 않은지 먼저 확인했다.
- 유지할 계약:
  - `docs/spec/`와 구현이 다르면 자동으로 막지 않는다. 대신 차이, 영향, 선택 이유를 사용자에게 보고한다.
  - `ref/`는 삭제된 경로이며, 앞으로 구현 근거로 참조하지 않는다.
  - 구현 변경이 생기면 이 파일에 날짜별 로그를 추가한다.
  - credentials, generated output, local runtime artifact는 계속 stage하지 않는다.
- 검증:
  - `ref/` 삭제 확인.
  - `docs/`에는 `docs/spec/` 파일만 남았는지 확인.
  - 삭제된 planning/reference 경로를 코드가 참조하지 않는지 `rg`로 확인.
  - `AGENTS.md`, `docs/spec/README.md`, `assets/agent-icons/README.md`가 `.gitignore`에 가려지지 않는지 확인.
  - cleanup 이후 표준 frontend/backend 검증 suite를 실행한다.

### 2026-06-28: 차트 데이터 상태와 라이브 스트림 상태 분리 안정화
- 변경:
  - historical candle snapshot/backfill 상태와 WebSocket live stream 상태가 서로를 덮어쓰지 않도록 chart runtime을 정리했다.
  - snapshot 실패가 `streamStatus=error`를 만들지 않게 했고, snapshot 로드도 `streamStatus=live`를 만들지 않게 했다. live/stale/error는 WebSocket control/live event 경로에서만 바뀐다.
  - Agent chart context에 `dataStatus`, `candleCount`, `hasVisibleCandles`를 명시적으로 포함했다. LLM은 차트 분석 가능 여부를 live stream 상태가 아니라 실제 candle availability로 판단해야 한다.
  - backfill eligible 상태에서 빈 데이터가 잠깐 내려오는 경우는 terminal empty가 아니라 preparing 상태로 취급하도록 `isPreparingCandleData` helper를 추가했다.
  - chart panel header의 가격/등락률은 live socket 상태가 아니라 현재 보유 candle 데이터로 계산하도록 바꿨다.
- 판단:
  - “데이터가 있음”과 “실시간 연결이 정상임”은 다른 계약이다. 두 상태가 섞이면 Agent가 분석 가능한 차트를 두고도 stream error를 근거로 분석 불가처럼 말하거나, 반대로 snapshot만 있는데 live로 오해하는 문제가 생긴다.
  - `No candle data`는 backfill 불가/실패처럼 실제 terminal empty일 때만 보여야 한다. 수집 요청 또는 백필 준비 중에는 loading/preparing으로 보여주는 편이 사용자 신뢰와 상태 모델에 맞다.
  - 패널 헤더의 현재가/등락률은 “지금 화면에 표시 가능한 candle”의 요약이며, live feed health indicator가 아니다.
- 유지할 계약:
  - REST `/api/charts/candles`는 historical snapshot/range loading 상태를 표현한다.
  - WebSocket과 `chart.stream.status`는 live feed health만 표현한다.
  - Agent 01은 명시적으로 참조된 chart context를 받아 분석하며, `dataStatus.candleCount > 0` 또는 `ready/partial`이면 stream이 stale/error여도 차트 자체는 분석 가능하다고 본다.
  - `No candle data is available...` 문구는 preparing/backfill 진행 상태에서는 노출하지 않는다.
- 검증:
  - chart runtime test에 snapshot/status 분리, preparing 상태, Agent context candle count 회귀 테스트를 추가했다.
  - backend Agent market context test에 data readiness와 live feed status 분리 검증을 추가했다.
  - 브라우저에서 초기 선택 패널 0개, NVDA/AMD/ASML/MU 차트 렌더링, Watch List 7개 seed, Ask Agent reference placeholder, MU 전환 중 no-candle flicker 없음, console error 없음을 확인했다.

### 2026-06-28: OpenAI 실제 전송 검증과 stream 상태 노출 최소화
- 변경:
  - 실제 OpenAI `/v1/responses` 전송을 backend `/api/llm/chat`, `/api/llm/chart-proposal` 경로로 검증했다.
  - 일반 차트 분석 요청에서는 `streamStatus`를 OpenAI 입력 context에서 제거하도록 했다. 실시간 연결 상태를 묻는 요청일 때만 live feed status를 전달한다.
  - Agent market context도 `streamStatus`가 제공된 경우에만 `liveFeedStatus`를 포함하도록 바꿨다.
- 판단:
  - “프롬프트로 언급하지 말라”는 지시만으로는 모델이 `streamStatus=error`를 답변에 반영할 수 있었다. 일반 차트 분석에 불필요한 live 상태는 애초에 보내지 않는 편이 안정적이다.
  - 차트 분석의 기본 근거는 visible candle과 `dataStatus`이며, live feed health는 별도의 운영/실시간 상태 질문에서만 다룬다.
- 유지할 계약:
  - OpenAI API key는 backend에서만 읽고 frontend bundle이나 응답에 노출하지 않는다.
  - Agent 01은 chart-only command만 반환하며 layout/trading/account/order command는 반환하지 않는다.
  - 일반 “차트를 분석해줘” 요청은 `streamStatus=error`가 있어도 candle 데이터가 있으면 차트 분석으로 진행한다.
- 검증:
  - `GOPS_USE_MOCK_LLM=False`, `OPENAI_API_KEY` 존재, `OPENAI_MODEL=gpt-5.2`를 값 노출 없이 확인했다.
  - `/api/llm/chat` 실제 호출 200 OK, command 3개 반환, `stream/error` 언급 없음 확인.
  - `/api/llm/chart-proposal` 실제 호출 200 OK, proposal command 4개 반환 확인.
  - backend unittest에 일반 분석 요청의 `streamStatus` 제거와 live status 요청 구분 테스트를 추가했다.

### 2026-06-28: Chart interval/backfill/aggregation V1
- 변경:
  - 루트에 `ChartIntervalBackfillPlan.md`를 추가하고, chart interval canonical 값을 `1m`, `5m`, `10m`, `1D`, `1W`, `1M`으로 고정했다.
  - `1d` 같은 legacy 입력은 API/UI/command boundary에서 `1D`로 정규화하되, document/API response/shared command에는 canonical 값만 남기도록 했다.
  - 초기 차트 로드는 24시간 환산이 아니라 interval별 visible bar count로 바꿨다. 기본값은 `1m/5m/10m=390`, `1D=250`, `1W=260`, `1M=120`이다.
  - 백필 목표 범위는 intraday group(`1m/5m/10m`) 1년, higher timeframe group(`1D/1W/1M`) 5년으로 분리했다.
  - `5m/10m`은 stored `1m`, `1W/1M`은 stored `1D`에서 query-time aggregation으로 제공한다. aggregation 결과에는 `ma5/ma20/ma60` flat field를 다시 붙인다.
  - direct Alpaca backfill은 `1m`, `1D`만 수행한다. 파생 interval backfill 요청은 직접 Alpaca를 호출하지 않고 source interval(`5m/10m -> 1m`, `1W/1M -> 1D`) 백필 요청으로 연결한다.
- 판단:
  - 화면을 채우는 visible bar count와 저장/수집 목표 범위는 다른 책임이다. 특히 `1M`은 화면 기본값이 120개지만 5년 범위에서는 약 60개만 가능하므로 request upper bound와 coverage target을 분리했다.
  - V1 query-time aggregation은 MVP 안정화용이다. 장기적으로는 `chart_candles`에 모든 interval candle을 materialized 저장하는 방향을 유지한다.
  - 이동평균선 미표시는 renderer보다 데이터 경로 문제로 본다. 저장/직접 조회 row뿐 아니라 query-time aggregation row에도 API 응답 직전 MA flat field가 존재해야 한다.
- 유지할 계약:
  - 프론트는 fake candle을 만들지 않는다. 데이터가 없으면 loading/empty/backfill 상태를 보여준다.
  - REST `/api/charts/candles`는 모든 canonical interval을 같은 shape로 반환한다.
  - WebSocket interval validation도 canonical interval과 legacy boundary normalization을 따른다.
  - `1m`과 `1M`은 의미가 다르므로 LLM fallback parser는 대소문자 충돌을 조심해서 처리한다.
- 검증:
  - `git diff --check`
  - `npm run test:chart --prefix apps/gops-frontend`
  - `npm run build --prefix apps/gops-frontend`
  - `.venv/bin/python -m compileall packages services/07-api-websocket/gops-backend/app tests`
  - `.venv/bin/python -m unittest discover -s services/07-api-websocket/gops-backend/tests`
  - `env PYTHONPATH=packages:services/07-api-websocket/gops-backend .venv/bin/python -m unittest discover -s tests`
  - 브라우저 `127.0.0.1:5174`에서 timeframe select가 `1m/5m/10m/1D/1W/1M`을 표시하고, `1D/1W/1M` 전환 시 white screen 없이 동작함을 확인했다.
  - 로컬 API `127.0.0.1:8000`에서 `interval=1d` 요청이 `interval=1D`로 반환되고, `1W` snapshot과 `1M` derived backfill status가 canonical response를 반환함을 확인했다.
  - screenshot pixel check에서 chart canvas nonblank와 MA 색상 계열 픽셀을 확인했다.

### 2026-06-28: Derived interval source backfill correction
- 변경:
  - `5m/10m/1W/1M` backfill 요청이 `unavailable`로 끝나지 않고, 내부적으로 source interval 요청을 생성하도록 수정했다.
  - `5m/10m`은 `1m`, `1W/1M`은 `1D` 요청을 만들고, API response에는 사용자가 요청한 `interval`과 실제 수집 기준인 `sourceInterval`을 함께 반환한다.
  - snapshot metadata와 frontend data status도 `sourceInterval`을 보존하게 했다. 따라서 `1W`나 `1M`을 먼저 눌러도 source인 `1D` 상태를 보고 백필/준비/완료 상태를 판단한다.
  - terminal status 처리 시 frontend의 in-flight backfill key를 정리해, 성공 이후에도 preparing 상태에 갇히는 경로를 막았다.
  - 자동 backfill 범위는 `.env`의 `BACKFILL_DEFAULT_LOOKBACK_HOURS`에 흔들리지 않고 interval 정책을 따른다. 명시적으로 `start/end`를 보내거나 내부에서 `lookback_hours`를 직접 넘기는 경우만 별도 범위를 허용한다.
- 판단:
  - 사용자는 `1W`나 `1M`을 열기 위해 먼저 `1D`를 알아서 눌러야 하면 안 된다. UI interval은 사용자의 분석 의도이고, 시스템은 그 interval을 만들기 위한 source interval을 스스로 알아야 한다.
  - `.env`의 개발용 lookback override가 자동 백필 정책을 덮어쓰면 1년/5년 계약이 조용히 깨진다. 수집 범위 정책은 코드 계약으로 고정하고, 임시 범위 축소는 명시 요청으로만 허용한다.
  - 파생 interval candle은 V1에서 query-time aggregation이지만, API와 frontend는 source interval 세부사항을 숨기지 않고 진단 가능한 metadata로 보존한다.
- 유지할 계약:
  - Alpaca 직접 호출 대상은 여전히 `1m`, `1D`뿐이다.
  - 프론트는 없는 candle을 생성하지 않는다. source backfill이 끝난 뒤 aggregation 결과가 있을 때만 차트를 그린다.
  - REST snapshot과 명시적 backfill 요청은 분리한다. 단, 사용자가 derived interval을 요청하면 backfill API가 필요한 source interval 요청으로 연결한다.
  - 장기적으로는 모든 interval candle을 materialized 저장하는 방향을 유지한다.
- 검증:
  - API에서 `MU 1M` 요청이 `sourceInterval=1D`, 5년 range로 성공하고 월봉 snapshot에 `ma5`가 포함됨을 확인했다.
  - API에서 `AVGO 5m` 요청이 `sourceInterval=1m`, 1년 range로 성공하고 5분봉 snapshot에 `ma5/ma20`이 포함됨을 확인했다.
  - 브라우저에서 `MU -> 1M`, `AVGO -> 10m`을 먼저 선택해도 `No candle data`나 frontend render error 없이 canvas가 유지됨을 확인했다.

### 2026-06-28: No generated market data or mock LLM baseline
- 변경:
  - `sample-dev` historical backfill과 sample raw bar generator를 제거했다. Backfill runner는 실제 Alpaca historical API에서 받은 row만 S3/ClickHouse로 흘린다.
  - Candle snapshot, WebSocket event, frontend chart runtime에서 `isSynthetic` 계약을 제거했다. 데이터 출처는 실제 `source/feed`와 empty/backfill status로만 표현한다.
  - `GOPS_USE_MOCK_LLM` 분기와 Agent 01 fallback response를 제거했다. OpenAI key가 없거나 호출이 실패하면 가짜 command/proposal을 만들지 않고 503/502로 실패한다.
  - local smoke backfill script와 README/env 예시도 실제 Alpaca historical path 기준으로 정리했다.
- 판단:
  - 시장 데이터와 LLM 제안은 사용자 판단에 직접 영향을 준다. 개발 편의를 위해 그럴듯한 candle이나 command를 생성하면 실제 데이터 공백을 숨기고 차트 신뢰성을 깨뜨린다.
  - 테스트 fixture는 런타임 API로 노출되지 않는 정적 입력으로만 허용한다. 제품 경로에서는 Redis/ClickHouse/S3/Alpaca/OpenAI에서 오지 않은 데이터를 렌더링하거나 제안하지 않는다.
  - 데이터가 없을 때는 빈 화면과 짧은 diagnostic message가 맞다. 잘못 그려진 차트보다 솔직한 empty/preparing/error 상태가 더 안전하다.
- 유지할 계약:
  - REST `/api/charts/candles`와 WebSocket event는 실제 source/feed만 전달한다.
  - Backfill은 `1m`, `1D` source interval에 대해 실제 Alpaca historical data만 수집한다.
  - OpenAI credential은 backend에서만 읽고, 미설정 시 Agent command를 만들지 않는다.
  - UI panel placeholder는 시장 데이터가 아니므로 이번 기준선의 fake data 제거 범위에 포함하지 않는다.
- 검증:
  - `rg`로 `isSynthetic`, `synthetic`, `sample-dev`, `build_sample`, `GOPS_USE_MOCK_LLM`, `fallback_agent`, `fallback_chart` 런타임/테스트/문서 잔재가 없음을 확인한다.
  - `npm run test:chart --prefix apps/gops-frontend`
  - `npm run build --prefix apps/gops-frontend`
  - `.venv/bin/python -m compileall packages services/07-api-websocket/gops-backend/app tests`
  - `.venv/bin/python -m unittest discover -s services/07-api-websocket/gops-backend/tests`
  - `env PYTHONPATH=packages:services/07-api-websocket/gops-backend .venv/bin/python -m unittest discover -s tests`

### 2026-06-28: Chart data trust hardening
- 변경:
  - 루트에 `ChartDataTrustHardeningPlan.md`를 추가해 no-dummy 이후 검증해야 할 chart data 신뢰성 이슈를 정리했다.
  - `GET /api/charts/candles` 응답에 상세 `coverage` 진단 객체를 추가했다. 기존 `dataStatus/backfillStatus/canBackfill/message`는 호환을 위해 유지한다.
  - Backfill `succeeded`를 곧바로 chart `ready`로 보던 낙관 판정을 제거했다. 실제 저장 coverage가 target range/count를 만족해야 ready가 된다.
  - runtime에서 Kafka에 sample market event를 publish하던 local smoke script와 sample producer를 제거했다.
  - 미국 주식/ETF serving path에서 주말 candle을 배제하도록 Redis/ClickHouse snapshot 경로와 ClickHouse coverage query를 보강했다.
- 판단:
  - 백필 작업의 성공 여부와 사용자가 볼 수 있는 candle coverage는 다른 책임이다. Alpaca가 no bars를 반환하거나 일부 구간만 저장된 경우에도 job은 끝날 수 있으므로, 차트 ready 판정은 저장소 coverage 기준이어야 한다.
  - 과거 local volume에 남은 개발 데이터는 현재 code path에서 안전하게 식별할 수 없다. 자동 삭제나 추정 필터보다 명시적인 local reset/check 절차가 안전하다.
  - 다만 GOPS의 현재 universe는 미국 주식/ETF이므로 주말 candle은 정상 시장 데이터로 보지 않는 방어선이 타당하다. 이는 local 잔존 데이터뿐 아니라 future ingestion 오류를 막는 serving invariant다.
  - 테스트 fixture는 허용하지만, Redis/ClickHouse/S3/Kafka runtime으로 흘러가는 generated market data producer는 금지한다.
- 유지할 계약:
  - REST snapshot은 장기 backfill을 숨은 side effect로 시작하지 않는다. 프론트가 필요할 때 `/api/charts/backfill`을 명시 호출한다.
  - `5m/10m` coverage는 source `1m`, `1W/1M` coverage는 source `1D` 저장 상태를 기준으로 판단한다.
  - 프론트는 terminal empty일 때만 데이터 없음 메시지를 보여주고, queued/running 상태는 loading/preparing으로 유지한다.
- 검증:
  - Backend/Frontend 테스트에 backfill succeeded but insufficient coverage, coverage normalization, preparing-vs-empty 상태를 추가한다.
  - `rg`로 runtime sample/dummy/synthetic producer가 남지 않았는지 확인한다.
  - `git diff --check`, frontend build/test, backend compile/unittest, root unittest를 실행한다.

### 2026-06-28: Interval renderability and sparse source protection
- 변경:
  - chart snapshot coverage에 `renderable`, 최소 source/returned bar 기준, 반환 window span, invalid row count 진단을 추가했다.
  - `ready` 판정은 target range와 target count를 모두 만족할 때만 허용한다. 넓은 기간에 8개 daily row만 흩어진 경우처럼 sparse source는 더 이상 `ready`가 아니다.
  - frontend는 `partial` snapshot을 무조건 chart로 그리지 않고, `coverage.renderable=true`일 때만 render scene을 `ready`로 승격한다.
  - `1D` direct snapshot은 stored daily row를 calendar day 기준으로 묶어 반환하고, `1W/1M`은 validated daily source coverage가 부족하면 chart처럼 보여주지 않는다.
  - ClickHouse loader와 S3 materializer는 주말/비정상 candle row를 serving projection에 적재하지 않는다.
  - 같은 range가 terminal backfill status로 잠겨 있어도 명시적 `force` backfill 요청은 새 repair 요청을 만들 수 있게 했다.
  - canvas 상태 문구는 줄바꿈 처리해 긴 diagnostic message가 패널 밖으로 잘리지 않게 했다.
- 판단:
  - 백필 job 성공, 저장 coverage 완성, 현재 화면에 그릴 수 있는 renderability는 서로 다른 개념이다. 이 셋을 섞으면 sparse daily data가 1D/1W/1M에서 그럴듯한 차트처럼 보인다.
  - 기존 local volume에는 이전 개발 데이터가 섞여 있을 수 있으므로 invalid row count는 진단으로 남긴다. 다만 serving query가 weekday 등 유효 row만 반환한다면, 현재 반환 window가 충분하고 촘촘한 경우 chart 렌더링은 허용한다.
  - `5m/10m`의 query-time aggregation은 source `1m` window가 지나치게 sparse하면 막는다. `1D/1W/1M`은 source `1D`가 최소 기준을 넘기 전까지 분석용 chart로 보지 않는다.
- 유지할 계약:
  - fake candle은 생성하지 않는다. 없는 데이터나 sparse data는 차트 대신 상태 메시지를 보여준다.
  - `5m/10m`은 V1에서 `1m`, `1W/1M`은 `1D` 기반 query-time aggregation이다.
  - REST snapshot은 candle payload와 coverage 진단을 함께 반환하고, frontend는 coverage를 렌더링 판단에 사용한다.
  - local DB cleanup/reset은 별도 승인 없이 자동 수행하지 않는다. 필요한 경우 `force` backfill이나 별도 reset 절차로 명시적으로 repair한다.
- 검증:
  - API matrix에서 `MU/AMD/AVGO 1D/1W/1M`이 `ready`가 아니라 `partial + renderable=false`로 바뀐 것을 확인했다.
  - API matrix에서 `AVGO 5m/10m`의 장기 sparse window가 `returned_window_sparse`로 차단되는 것을 확인했다.
  - 브라우저에서 `NVDA 1D`는 차트가 아니라 “stored 1D candles 없음” 상태를 표시했고, `NVDA 5m`은 실제 캔들/MA/볼륨 차트를 유지했다.
  - 브라우저에서 `MU 1M`은 sparse source 메시지를 표시하고, 다시 `1m`으로 돌아오면 정상 차트가 복구됨을 확인했다.
  - 테스트로 storage boundary invalid candle skip과 force backfill requeue를 고정했다.

### 2026-06-28: Local daily backfill repair and monthly bucket correction
- 변경:
  - 로컬 Docker 런타임을 최신 코드로 rebuild/restart하고, 오염된 `1D/1d/1W/1M` ClickHouse row, Redis backfill/cache key, MinIO daily backfill 산출물을 정리한 뒤 `ALPACA_SYMBOLS` 7개에 대해 `1D force backfill`을 재실행했다.
  - Alpaca dailyBars에서 각 seed symbol이 5년 범위 1253개 row로 materialize됨을 확인했다.
  - `1M` query-time aggregation 결과가 월초가 주말인 달을 공통 weekday filter로 잃지 않도록 serving/storage validation을 보정했다.
  - 5년 daily backfill이 휴일/거래일 차이로 naive target보다 몇 개 적더라도, dense하고 renderable하면 `ready`로 볼 수 있도록 coverage complete 판정에 trading-calendar tolerance를 추가했다.
- 판단:
  - `1D` source가 실제로 충분히 저장되기 전에는 `1W/1M`을 신뢰할 수 없다. 따라서 문제 해결 순서는 daily source repair 후 derived interval 검증이 맞다.
  - 월봉 bucket timestamp는 거래일 timestamp가 아니라 calendar month bucket이므로, 월초가 주말이라는 이유로 제거하면 정상 월봉 일부가 사라진다.
  - coverage target은 품질 기준이어야 하지만, 시장 휴일을 무시한 정확한 calendar count 비교가 정상 dailyBars를 partial로 만드는 것은 과하다.
- 유지할 계약:
  - fake candle은 만들지 않는다. source가 없거나 sparse하면 차트 대신 diagnostic state를 보여준다.
  - `1W/1M`은 V1에서 stored `1D` 기반 query-time aggregation이다.
  - 로컬 저장 데이터 정리는 명시 승인된 repair 작업에서만 수행한다.
- 검증:
  - ClickHouse에서 seed symbol별 `1D` row 1253개, invalid row 0개를 확인했다.
  - API에서 `NVDA 1D/1W/1M`이 실제 Alpaca dailyBars 기반으로 서로 다른 snapshot을 반환하는지 확인했다.
  - 회귀 테스트로 dense completed daily backfill tolerance, monthly weekend bucket retention, monthly storage validation을 추가했다.

### 2026-06-28: Local intraday backfill repair
- 변경:
  - `1m/5m/10m`에서 `Stored 1m candle coverage is too small...` 상태가 발생해 로컬 `1m` serving 계층을 점검했다.
  - seed symbol의 기존 `1m` ClickHouse row, Redis intraday/backfill cache, MinIO 1m raw/final 산출물을 정리하고 `1m force backfill`을 재실행했다.
  - `NVDA/AMD/AVGO/TSM/ASML/AMAT/MU` 모두 Alpaca `1Min` historical backfill이 성공했고, `5m/10m`은 저장된 `1m` 기반 query-time aggregation으로 복구됐다.
- 판단:
  - `5m/10m`의 문제는 별도 데이터 부족이 아니라 source interval인 `1m` coverage 부족의 결과다.
  - local stream으로 쌓인 수십 개 row만으로는 intraday chart를 신뢰할 수 없으므로, seed symbol은 명시적 `1m` backfill 기준선을 갖춰야 한다.
  - 기존 local MinIO/ClickHouse에 남은 sparse/invalid 1m 산출물은 false chart보다 위험하므로 repair 시 명시적으로 정리한다.
- 유지할 계약:
  - fake candle은 생성하지 않는다.
  - `1m`은 Alpaca `1Min` historical/source data, `5m/10m`은 stored `1m` 기반 query-time aggregation이다.
  - local 저장 데이터 정리는 명시적인 repair 작업에서만 수행한다.
- 검증:
  - seed symbol별 `1m` row가 120k~236k 범위로 저장되고 invalid row 0개임을 확인했다.
  - API matrix에서 모든 seed symbol의 `1m/5m/10m`이 `ready`, `coverage_complete`, `renderable=true`임을 확인했다.
  - 브라우저 현재 `10m` 상태에서 coverage warning, no data, frontend error가 없음을 확인했다.
