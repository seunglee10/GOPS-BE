# GOPS Service Boundaries

이 문서는 GOPS를 AWS 규모로 확장하기 전에 현재 코드에서 먼저 지켜야 할 논리적 서비스 경계를 정의한다.

현재는 local 개발 scaffold이므로 모든 backend 기능이 하나의 FastAPI 앱에서 실행될 수 있다. 다만 코드, 문서, contract는 이후 별도 서비스로 분리될 수 있도록 미리 경계를 둔다.

## 현재 기준선

현재 차트 구현 상태의 이름은 다음으로 고정한다.

```text
Chart Tool Runtime V1 core implementation baseline + validation hardening backlog
```

의미:

- 차트 렌더링, command runtime, backend dummy data, WebSocket, drawing/comparison tool runtime, preview-first proposal, Agent 01 chart chat 경로는 개발용 core baseline으로 수용한다.
- Playwright screenshot regression, multi-chart browser scenario, `/ref/references` behavior comparison, real provider 전환 정책은 validation hardening backlog로 남긴다.
- 문서와 보고서에서는 `V1 검증 완료판`, `검증 완결판`, `M1-M7 엄격 완료`라고 부르지 않는다.
- 모바일 전용 viewport/layout 검증은 현재 기준선의 완료 조건이 아니다. 현재 UI 기준은 desktop Bento Grid workspace와 panel resize 검증이다.

## 서비스 경계

| 경계 | 현재 위치 | 장기 책임 |
| --- | --- | --- |
| Frontend App | `frontend/` | Bento Grid, panel runtime, chart interaction, CSR Canvas rendering, chart-local undo/redo |
| Shared Chart Contract | `shared/chart-contract/` | `ChartCommand`, `ChartProposal`, capability manifest, payload schema, validation contract |
| Backend API/BFF | `backend/app/main.py`, `backend/app/routes/` | frontend-facing REST/WebSocket entrypoint, auth/user context, service routing |
| Market Data Service | `backend/app/services/market_data.py` | candle snapshot, live/corrected candle event, symbol validation, provider adapter |
| AI Agents Service | `backend/app/services/ai_agents.py` | OpenAI 호출, chart proposal/chat 생성, capability 기반 tool selection |
| Chart Runtime | `frontend/src/chart/` | `ChartDocument`, client command apply, render scene, Canvas renderer |

## 핵심 원칙

- 차트 렌더링은 CSR client-owned다.
- LLM은 Canvas를 직접 그리거나 frontend state를 직접 수정하지 않는다.
- 사용자 UI와 LLM Agent는 같은 `ChartCommand` contract를 사용한다.
- LLM 응답은 `ChartProposal` 또는 agent chat response로 들어오며, command validation을 통과해야 적용된다.
- top app bar auto toggle은 chart/layout 분석 UI command에만 적용한다.
- 주문 생성, 취소, 정정, 계좌/잔고/체결 변경 command는 chart/layout auto toggle 범위에 포함하지 않는다.
- 현재 FastAPI의 `/api/llm/*`는 development scaffold다. 장기적으로 AI Agents Service로 분리한다.
- 현재 `/api/llm/chart-proposal`과 `/api/llm/chat`는 Agent 01 chart operator scaffold다. Agent 02~04와 multi-agent orchestration은 UI에 표시될 수 있지만 chart command chat 권한은 비활성이다.
- Agent chat 신호등은 Agent/LLM 상태 전용이다. chart data stream 상태는 chart panel 내부 live/status UI에서 다룬다.
- 현재 5개 dummy symbol과 `source: dummy`, `feed: synthetic-demo`, `isSynthetic: true`, `notice`는 development-only field/policy다.

## Shared Chart Contract

현재 공유 계약 기준 위치:

```text
shared/chart-contract/
```

현재 contract mirror:

- TypeScript runtime/types: `frontend/src/chart/types.ts`, `frontend/src/chart/capabilities.ts`
- Python request/schema mirror: `backend/app/contracts/chart.py`
- Contract reference files: `shared/chart-contract/chart-command.schema.json`, `shared/chart-contract/chart-capabilities.json`

운영 원칙:

- contract 변경은 frontend, backend, AI Agents Service가 동시에 이해할 수 있어야 한다.
- command id, payload key, validation rule은 임의로 변경하지 않는다.
- 새로운 chart tool은 먼저 shared contract에 추가하고, 이후 frontend renderer/runtime과 AI Agents Service 적용을 진행한다.
- shared canonical chart command schema는 runtime이 이해하는 전체 command set을 정의한다.
- backend OpenAI generation schema는 LLM이 직접 생성할 수 있는 안전 subset이다. 예를 들어 preview control, drawing remove/select, comparison remove/update 같은 runtime command가 canonical schema에 있어도 OpenAI strict schema에는 의도적으로 빠질 수 있다.
- real provider 전환 전까지 dummy symbol 제한은 development-only로 문서화한다.

## FastAPI 내부 구조

현재 FastAPI 앱은 배포 단위 하나를 유지하되 내부 경계는 다음처럼 분리한다.

```text
backend/app/
  main.py                 # app assembly only
  core/config.py          # local env/config
  contracts/chart.py      # Python mirror of chart contract
  routes/health.py        # health endpoint
  routes/charts.py        # chart REST API
  routes/llm.py           # AI proposal/chat API scaffold
  routes/streams.py       # chart WebSocket API
  services/market_data.py # dummy market data provider boundary
  services/ai_agents.py   # AI Agents Service scaffold boundary
```

이 구조는 나중에 다음처럼 물리 서비스로 분리할 수 있게 하기 위한 준비다.

```text
Frontend -> API/BFF -> Market Data Service
                  -> AI Agents Service
                  -> Workspace/Chart State Service
```

## AWS 전환 시 예상 배치

구체적인 AWS 배포 설계는 후속 문서에서 확정한다. 현재 경계는 다음 전환을 고려한다.

- React frontend: S3/CloudFront 또는 equivalent static hosting
- API/BFF: ECS/Fargate 또는 equivalent container runtime
- WebSocket fan-out: API Gateway WebSocket, ALB WebSocket, 또는 별도 gateway
- Market data stream: Kafka/Kinesis 계열 stream, Flink 계열 processing, ClickHouse/Redis 계열 serving store
- AI Agents Service: OpenAI key를 Secrets Manager 계열 secret store에서 읽는 별도 service
- Evidence/raw data: S3 계열 object storage

## 기준선 이후 우선순위

현재 다음 우선순위는 Chart Tool Runtime V1 core baseline의 validation hardening이다.

1. `docs/planning/chart-tool-runtime-v1.md` 기준으로 현재 drawing/comparison/preview-first runtime baseline을 유지한다.
2. `docs/planning/chart-tool-runtime-milestones.md` 기준으로 완료된 core milestone과 남은 hardening backlog를 분리해 관리한다.
3. Playwright/browser regression, multi-chart scenario, `/ref/references` behavior comparison으로 validation hardening backlog를 줄인다. 이때 viewport 대상은 우선 desktop이며, 모바일 화면은 별도 후속 범위로 둔다.
4. real provider 전환 정책인 `source/feed/isSynthetic`, reconnect/backfill/replay는 차트 도구 기준선 이후 정리한다.
5. shared chart contract를 생성/검증 가능한 schema package로 고도화한다.
