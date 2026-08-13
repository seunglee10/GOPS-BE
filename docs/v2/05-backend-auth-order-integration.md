# 5. Backend API / Auth / Order / Integration

## Mission

Frontend, Agent, market-data pipeline, order system을 연결하는 API gateway와 사용자 중심 workflow를 구현한다.

이 역할은 "나머지"가 아니다. 사용자 요청이 들어오는 진입점, 인증, session, API response contract, 주문 안전장치, 내부 service orchestration을 책임진다.

## Owns

- FastAPI Backend API
- Google OAuth2 login/callback/logout/me
- Redis session
- Postgres user/order/agent metadata
- Chart API
- Agent API
- Order API
- WebSocket endpoint
- SSE endpoint
- API response `meta` contract
- idempotency key
- Order Outbox
- KIS demo order flow
- order event log
- forbidden field validation
- recursive redaction
- KIS demo-only guardrail

FastAPI는 Python으로 HTTP API 서버를 만들 때 사용하는 웹 프레임워크다. route 함수, request validation, response serialization을 빠르게 구성할 수 있다. GOPS에서는 frontend와 worker가 사용하는 REST/WebSocket API gateway로 사용한다.

OAuth2는 외부 계정으로 로그인할 때 많이 쓰는 인증 표준이다. GOPS에서는 Google 로그인에 사용한다. 사용자를 Google consent 화면으로 보낸 뒤 callback에서 `state`를 검증하고 GOPS session을 만든다.

Postgres는 관계형 데이터베이스다. 사용자, 주문, idempotency key, event log처럼 정합성이 중요한 데이터를 저장한다.

## Does Not Own

- React 화면 구현
- Alpaca/SEC 수집 worker
- Kafka platform 운영
- EKS 배포 manifest
- GitHub Actions workflow
- LLM Agent 내부 추론 로직

## Main Paths

- `systems/api-server/`
- `systems/api-server/pods/api-server/app/`
- `systems/api-server/pods/api-server/app/routes/`
- `systems/api-server/pods/api-server/app/auth/`
- `systems/api-server/pods/api-server/app/services/`
- `systems/order/`
- `systems/order/pods/order-outbox/`
- `systems/order/pods/kis-adapter/`
- `systems/order/shared/kis_trader/`
- `systems/order/jobs/postgres-migrations/`
- `systems/order/jobs/reconciler/`

## Source Sections

`docs/v2/gops-v2-architecture.md`에서 먼저 볼 섹션:

- `5.2 Backend API`
- `5.8 Order Outbox`
- `5.9 KIS Demo Adapter`
- `8. Backend API Design`
- `9. Authentication Flow`
- `16. Redis Cache Design`
- `17. Postgres Data Model`
- `21. Order and KIS Guardrails`
- `23.2 Order Reliability`
- `26.1 Auth And Session`
- `26.2 Secret Handling`
- `26.3 Order Security`
- `29.2 Ownership Rules`
- `29.4 Pod And Job Map`

## API Contract

Preserve these routes unless the team explicitly changes the API contract.

Auth:

- `GET /api/auth/google/login`
- `GET /api/auth/google/callback`
- `GET /api/auth/me`
- `POST /api/auth/logout`

Chart:

- `GET /api/charts/candles`
- `POST /api/charts/backfill`
- `GET /api/charts/backfill/status`
- `GET /api/charts/symbols`
- `WS /ws/charts`

Agent:

- `POST /api/agents/analyze`
- `GET /api/agents/reports/{analysis_id}`
- `WS /ws/agent-alerts`

Order:

- `GET /api/order-contract`
- `GET /api/orders/balance`
- `POST /api/orders`
- `GET /api/orders/{order_id}`
- `GET /api/orders/{order_id}/events`
- `WS /ws/orders/{order_id}`

`POST /api/orders`는 반드시 `Idempotency-Key` header를 요구한다.

Idempotency key는 같은 요청이 여러 번 들어와도 주문이 중복 생성되지 않게 하는 키다. 네트워크 재시도나 사용자의 중복 클릭이 있어도 같은 key와 같은 body면 기존 응답을 재사용하고, 같은 key에 다른 body가 오면 conflict로 막는다.

## Common Response Shape

```json
{
  "data": {},
  "meta": {
    "asOf": "2026-07-02T00:00:00.000Z",
    "source": "redis",
    "cacheStatus": "hit",
    "stalenessMs": 120,
    "isStale": false
  }
}
```

`asOf`는 데이터 기준 시각이다. `cacheStatus`는 Redis 같은 cache에서 바로 온 값인지, DB/projection에서 읽은 값인지 표현한다. `stalenessMs`는 데이터가 얼마나 오래됐는지를 millisecond 단위로 나타낸다.

## Auth Rules

- OAuth state는 Redis에 TTL과 함께 저장한다.
- OAuth state는 1회만 사용한다.
- session id는 Redis에 TTL과 함께 저장한다.
- session cookie는 `HttpOnly`, `Secure`, `SameSite=Lax` 또는 `Strict`를 사용한다.
- callback 실패 사유는 일반화된 메시지로만 반환한다.
- 운영에서는 `AUTH_ENABLED=true`가 필요하다.

## Order Rules

- `KIS_ENV=demo`만 허용한다.
- 주문 생성 요청은 user ownership을 검증한다.
- 주문 생성 요청은 idempotency key를 요구한다.
- 주문 가능 현금/보유 수량을 확인한다.
- 주문 생성은 Postgres transaction 안에서 `orders`, `order_events`, `idempotency_requests`, `outbox_events`를 함께 기록한다.
- Agent proposal id가 있어도 주문 생성은 별도 사용자 클릭으로만 가능하다.
- KIS adapter가 실패해도 order event log는 남아야 한다.
- forbidden field는 중첩 dict/list까지 검사한다.
- API response, DB JSON, Kafka payload, log 후보 payload에는 recursive redaction을 적용한다.

## First Implementation Checklist

- API route contract가 기존 route와 충돌하지 않는지 확인한다.
- 인증이 켜진 상태와 꺼진 dev 상태를 둘 다 테스트한다.
- `Idempotency-Key` 누락, replay, body mismatch 테스트를 만든다.
- forbidden field validation 테스트를 만든다.
- order outbox publish 실패 후 재시도 가능성을 테스트한다.
- Agent proposal이 submit을 직접 실행하지 못하는지 확인한다.
- API response `meta`가 frontend에서 쓰기 충분한지 2번 담당자와 확인한다.

## Handoffs

- 1번 AI: Agent run 시작, report 조회, event stream contract를 맞춘다.
- 2번 Frontend: REST response, WebSocket, SSE message shape을 맞춘다.
- 3번 Data Pipeline: chart query facade, backfill status, market-data projection source를 맞춘다.
- 4번 Infra: API image, migration job, order worker image, KIS secret, Google OAuth secret, rollout smoke endpoint를 맞춘다.
