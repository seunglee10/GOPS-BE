# GOPS Merge Report

이 문서는 이번 merge에 포함되는 UI, Agent, 패널, chart-command 정리 내용을
짧게 남기기 위한 보고서다. 과거의 Goal 모드 구현 계획서는 현재 코드에 반영된
상태를 기준으로 보고서 형태로 전환했다.

## 이번 변경의 의도

- 하단 6개 버튼을 실제 진입점으로 만들고 숫자 라벨을 의미 기반 아이콘으로 바꿨다.
- 중앙 하단 입력창을 기본 Agent 입력창으로 정리하고, chart-command 개발 경로는
  임시 토글로 분리했다.
- 뉴스, 주문, 포트폴리오 패널이 빈 패널이 아니라 현재 종목과 연결된 실제 패널로
  동작하도록 했다.
- legacy chart-command 로직을 API 서버에서 공유 agent package로 옮기고,
  `/api/llm/chat`은 임시 compatibility wrapper로 줄였다.
- 차트 확대 시 캔들 사이 간격이 과도하게 벌어지지 않도록 최소 visible candle 수와
  candle width 계산을 조정했다.
- merge 전에 불필요한 Python `__pycache__` 산출물을 제거했다.

## 프론트엔드 변경

### 하단 command bar

- 하단 6개 버튼 역할을 다음처럼 고정했다.
  - 1번: 레이아웃/페이지
  - 2번: 포트폴리오
  - 3번: 관심종목
  - 4번: 알림설정
  - 5번: 로그인/프로필
  - 6번: 설정
- 숫자 텍스트 대신 `lucide-react` 아이콘을 사용한다.
- 버튼이 왼쪽에 있으면 왼쪽 floating panel, 오른쪽에 있으면 오른쪽 floating panel을
  연다.
- 로그인되지 않은 상태에서 auth가 켜져 있으면 5번 버튼은 바로 Google login으로
  보낸다.
- 로그인된 상태에서는 5번 버튼이 계정 floating panel을 열고 로그아웃 버튼을 보여준다.
- 알림은 아직 실시간 stream 완성이 아니라 진입점 수준으로 남겼다.

### 중앙 Agent 입력창

- 중앙 하단 입력창의 기본 경로는 `/api/agents/analyze`이다.
- `/api/agents/analyze` 응답이 queued/report envelope 형태여도 report polling으로
  완료 결과를 받아 chat log에 표시한다.
- 로그인 required 환경에서는 로그인 전 Agent 입력을 프론트에서 막는다.
- `AUTH_ENABLED=false`인 로컬 개발 모드에서는 로그인 없이 Agent 입력을 실험할 수 있다.
- chat log는 입력창 위 화살표 버튼으로 열리는 floating Agent panel에 표시한다.
- 기존 말풍선 버튼은 제거했고, submit 버튼은 전송 아이콘으로 바꿨다.
- 현재 구현은 차트 컨텍스트가 있는 화면에서만 Agent 분석을 보낸다. 홈 화면에서는
  "차트를 선택하면 분석할 수 있습니다." 메시지로 막는다.

### Chart command dev toggle

- chart-command 개발용 토글은 입력창 바로 위에 `Chart` 체크박스 형태로 배치했다.
- auth가 켜진 환경에서는 로그인 후에만 보인다.
- 토글 off: 중앙 입력창이 `/api/agents/analyze`를 호출한다.
- 토글 on: 기존 chart-command 경로를 통해 차트 조작 agent 성능을 확인한다.
- 이 토글은 `ChartCommandAgent`가 main `AgentOrchestrator`에 통합되면 제거 대상이다.

### 패널과 레이아웃

- 초기 chart workspace는 뉴스, 온톨로지, 포트폴리오, 주문, 메인 차트 패널을 포함한다.
- panel insertion 메뉴에서 선택할 수 있는 종류는 실제 렌더러가 있는 패널로 제한했다.
- 패널 사이 `+`를 눌러 열린 추가 메뉴는 메뉴 외부를 클릭하면 닫힌다.
- 새 chart panel은 독립 종목 선택과 swap/close 흐름을 유지한다.
- 뉴스/주문/포트폴리오 패널은 기존 panel surface 안에서 스크롤되도록 정리했다.
- 주문 패널은 별도 dropdown 검색으로 주문 종목을 선택한다.
- 뉴스 패널 내부의 중복 "뉴스" 제목은 제거하고 현재 종목 중심으로 표시한다.
- 상단 ticker nav에는 chart 화면 전용 얇은 검정 가로선을 추가했다. 홈 화면에는 표시하지 않는다.

### 차트 줌

- chart-engine과 frontend legacy chart 모두 최소 visible candle 수를 `12`에서 `6`으로 낮췄다.
- wheel zoom step을 `12` 기준에서 `3` 기준으로 낮춰 더 세밀하게 확대되도록 했다.
- candle width 상한과 slot 비율을 키워 확대 상태에서 캔들 사이 간격이 덜 벌어지게 했다.
- chart command schema/capability의 `visibleCount.minimum`도 `6`으로 맞췄다.

## 백엔드 변경

### Read-only 뉴스 API

- `GET /api/market/news/latest?symbol=NVDA` 경로를 추가했다.
- API는 Agent 분석을 실행하지 않고 Redis/ClickHouse cached news intelligence를 읽는다.
- 응답 shape는 `symbol`, `source`, `items[]`를 포함한다.
- item은 `symbol`, `symbols`, `title`, `summary`, `url`, `source`, `publishedAt`,
  `impactDirection`을 포함한다.
- Redis에 데이터가 있으면 Redis를 우선 사용하고, 없으면 ClickHouse provider로 fallback한다.

### ChartCommandAgent 이관

- chart-command prompt, schema, context normalization, OpenAI response parsing을
  `systems/agent-orchestration/shared/gops_agents/chart_command/` 아래로 옮겼다.
- API server의 `app/services/ai_agents.py`는 shared `ChartCommandAgent`를 호출하는
  compatibility adapter로 줄였다.
- API contract의 chart-command schema helper는 기존 import 경로 호환을 위해 re-export
  형태로 남겼다.
- `/api/llm/chat`과 `/api/llm/chart-proposal`은 아직 개발용 compatibility surface이다.
- 관련 frontend/backend 폴더 README에 통합 완료 시 제거해야 할 문서와 wrapper를 기록했다.

## 문서 변경

- `systems/agent-orchestration/shared/gops_agents/chart_command/README.md`
  - chart-command agent의 현재 격리 상태, 미래 통합 방향, 통합 후 제거할 항목을 기록했다.
- `apps/gops-frontend/src/agent/README.md`
  - 중앙 Agent 입력은 `/api/agents/analyze`로 수렴해야 하며 `/api/llm/chat`은 임시 경로임을 기록했다.
- `apps/gops-frontend/src/components/README.md`
  - chart component가 backend routing/prompt를 소유하지 않아야 함을 기록했다.
- `systems/api-server/pods/api-server/gops-backend/app/contracts/README.md`
- `systems/api-server/pods/api-server/gops-backend/app/routes/README.md`
- `systems/api-server/pods/api-server/gops-backend/app/services/README.md`
  - `/api/llm/chat` wrapper와 chart-command migration cleanup 기준을 기록했다.

## Merge 전 주의사항

- `.env.example`에 큰 local bootstrap diff가 있다. 이번 UI/Agent/패널 코드 정리와는 직접
  관련이 없으므로, push/stage 전에 이 파일을 포함할지 별도로 판단해야 한다.
- `.env`와 실제 secret은 절대 commit하지 않는다.
- chart-command dev toggle, `/api/llm/chat` wrapper, migration README들은 의도적으로
  남긴 임시 구조다. `ChartCommandAgent`가 `AgentOrchestrator`에 통합되면 함께 제거한다.
- `apps/gops-frontend/tests/chartRuntime.test.ts`에는 일부 source-level smoke check가 있다.
  구조가 크게 바뀌는 merge에서는 이 테스트가 의도보다 민감하게 실패할 수 있다.
- 브라우저 자동 검증은 현재 로컬 URL 접근 정책에 막힐 수 있으므로, push 전 수동으로
  하단 버튼, Agent 입력, chart toggle, panel insertion menu, 뉴스/주문 패널을 한 번 확인한다.

## 검증 결과

다음 검증은 이번 보고서 작성 시점에 통과했다.

- `npm run build --prefix apps/gops-frontend`
- `npm run test:chart --prefix apps/gops-frontend`
- `.venv/bin/python -m unittest systems/api-server/tests/test_market_data_query.py`
- `git diff --check`

## 후속 고려사항

- 서버 측 `/api/agents/*` 인증 강제.
- `ChartCommandAgent`를 `AgentOrchestrator`에 정식 통합하고 dev toggle과 legacy wrapper 제거.
- Agent 결과 표시를 floating panel에서 richer report UI로 확장.
- Agent report SSE/polling UX 개선.
- 알림 stream과 4번 하단 버튼의 실제 설정 UI 연결.
- 뉴스 API의 빈 데이터/외부 provider 장애 상태를 더 사용자 친화적으로 표시.
- 주문 패널의 주문 가능 금액/주문 전송 UX를 실제 인증 및 KIS adapter 상태와 더 촘촘히 연결.
- 모바일 viewport에서 하단 floating panel, Agent panel, 주문 dropdown 겹침 검증.
