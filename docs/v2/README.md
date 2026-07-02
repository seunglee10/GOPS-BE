# GOPS v2 Team Docs

이 디렉터리는 `docs/v2/gops-v2-architecture.md`를 팀원이 맡은 역할별로 읽기 쉽게 나눈 문서 모음이다.

역할별 문서를 확인하고 수정할 때는 원본 `docs/v2/gops-v2-architecture.md`도 함께 수정한다. 구현 중 판단이 갈리면 다음 순서로 확인한다.

1. 현재 코드
2. `AGENTS.md`
3. `docs/PRODUCT_CONTEXT.md`
4. `docs/STRUCTURE_GUIDE.md`
5. `docs/ARCHITECTURE.md`, `docs/IMAGE_STRATEGY.md`, `docs/ENVIRONMENT.md`
6. `docs/v2/gops-v2-architecture.md`
7. 이 디렉터리의 역할별 문서

## Role Map

| 번호 | 담당 | 핵심 책임 |
| --- | --- | --- |
| 1 | AI / Agent | 근거 기반 멀티 Agent 분석, `EvidenceItem`, Agent event, Agent guardrail |
| 2 | Frontend / UI Chart | React 화면, 차트 렌더링, workspace UI, WebSocket/SSE 이벤트 표시 |
| 3 | Market Data / News / Fundamentals / Storage Pipeline | Alpaca/SEC 데이터 수집, Kafka 처리, Redis/ClickHouse/S3 저장, 차트용 projection |
| 4 | Infra / Platform / CI-CD | AWS, EKS, Kubernetes, Docker image, GitHub Actions, ECR, 배포/rollback |
| 5 | Backend API / Auth / Order / Integration | FastAPI API, Google OAuth2, session, Postgres, order, KIS demo, 시스템 통합 |

## Common Rules

- 각 담당자는 자신에게 나눠진 역할 문서를 확인하고 수정할 때 원본 `docs/v2/gops-v2-architecture.md`도 함께 수정한다.
- 새 runtime, pod, job, image, Kafka topic, S3 prefix, DB table, secret을 추가하면 담당 문서만이 아니라 `docs/STRUCTURE_GUIDE.md`, `docs/IMAGE_STRATEGY.md`, `docs/ENVIRONMENT.md`, `platform/`, `infra/` 영향도 같이 확인한다.
- S3에 저장하는 tick 데이터는 원본 payload가 아니라 가공된 데이터다.
- local runtime에서 가짜 market candle을 만들지 않는다.
- KIS는 v1/v2 모두 `demo`만 허용한다. `KIS_ENV=real`은 사용하지 않는다.
- Agent는 주문을 직접 제출하지 않는다. 사용자가 명시적으로 주문 버튼을 눌렀을 때만 주문이 생성된다.
- 사용자 결제/과금 기능은 v2 범위에 포함하지 않는다.

## Collaboration Boundaries

### AI 와 Data Pipeline

3번 담당자는 뉴스, 시장 데이터, 펀더멘탈 데이터를 수집하고 저장한다. 1번 담당자는 그 결과물인 `NewsEvent`, `EvidenceItem`, chart/fundamental projection을 근거로 Agent 분석을 만든다.

### Frontend 와 Backend

2번 담당자는 화면 상태와 차트 렌더링을 맡는다. 5번 담당자는 REST, WebSocket, SSE API contract를 제공한다. UI에서 필요한 데이터 모양이 바뀌면 API contract 변경으로 먼저 합의한다.

### Data Pipeline 와 Infra

3번 담당자가 pod/job/topic/S3 prefix/image 변경을 제안하면 4번 담당자가 Docker, Kubernetes, GitHub Actions 배포 경로를 반영한다.

### Backend 와 Order

5번 담당자는 API와 주문 흐름을 책임진다. 4번 담당자는 KIS credential, NetworkPolicy, secret 주입, pod 분리 같은 배포 보안 경계를 책임진다.

## Reading Order

처음 합류한 팀원은 다음 순서로 읽는다.

1. 이 파일
2. 자기 역할 문서
3. `docs/v2/gops-v2-architecture.md`의 관련 섹션
4. 관련 system README 또는 platform README
5. 현재 코드

## Role Documents

- `01-ai-agent.md`
- `02-frontend-chart-ui.md`
- `03-market-data-news-fundamentals-storage.md`
- `04-infra-platform-cicd.md`
- `05-backend-auth-order-integration.md`
