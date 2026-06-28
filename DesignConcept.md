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
