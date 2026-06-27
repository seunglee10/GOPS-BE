# 주문 신뢰성/보안 상세 스펙

작성일: 2026-06-25 KST
최종 수정: 2026-06-27 KST

이 문서는 [GOPS 통합 명세](./gops-integrated-spec.md)의 주문 시스템 기준을 바탕으로 주문 접수부터 KIS 제출, 체결 대사, Kafka 결과 이벤트, read model, 감사/운영 가드레일까지의 구현 계약을 정의한다. 흐름과 장애 sequence는 [주문 신뢰성/보안 아키텍처](./architecture.md), 구현 순서는 [주문 경로 보안/신뢰성 마일스톤](./security-reliability-milestones.md)을 따른다.

## 1. 범위와 원칙

MVP 주문 범위는 KIS 모의투자 주문을 실제로 수행할 수 있는 수준까지다. 실전 주문은 같은 파이프라인을 사용하되 kill switch, 계좌/사용자/종목별 한도, circuit breaker, 운영 알림, 복구 리허설을 통과한 뒤 별도 전환한다.

주문 경로의 핵심 원칙은 다음과 같다.

- 주문 중복 방지는 frontend 버튼 비활성화가 아니라 backend idempotency, DB unique constraint, Kafka 재처리 멱등성, KIS 주문/체결내역 대사의 조합으로 보장한다.
- Kafka는 주문 command와 결과 이벤트의 서비스 간 event spine이다.
- PostgreSQL은 단일 주문의 transaction guard, append-only 원장, transactional outbox, projection을 담당한다.
- Backend API는 KIS 주문 API를 직접 호출하지 않는다.
- KIS 주문 POST는 KIS Broker Adapter만 수행한다.
- KIS timeout, connection reset, 불명확한 5xx는 실패 확정이 아니므로 같은 주문을 즉시 재POST하지 않는다.
- API 응답은 최종 체결 결과가 아니라 주문 접수 또는 처리 상태다.
- `SUBMITTED`는 KIS 주문 접수 성공이지 체결 완료가 아니다.
- Frontend는 `FILLED` 확정 전까지 `체결 완료`를 표시하지 않는다.

## 2. 식별자와 멱등성

| 식별자 | 생성 주체 | 저장/노출 기준 | 목적 |
| --- | --- | --- | --- |
| `Idempotency-Key` | Frontend | raw 값은 로그, Kafka, 문서 예시에 남기지 않음 | 같은 주문 의도 재시도 묶기 |
| `idempotency_key_hash` | Backend | DB 저장 가능 | raw key 없이 중복 접수 판정 |
| `request_id` | Backend | 로그, trace, Kafka, DB에 포함 | API 호출 단위 추적 |
| `order_id` | Backend | API 응답, WebSocket, DB에 포함 | 사용자 조회 기준 주문 ID |
| `client_order_id` | Backend | KIS 제출 중복 방지와 broker 호환용 | 외부 제출 의도 식별 |
| `event_id` | Producer | Kafka/DB unique key | 이벤트 중복 처리 |
| `account_alias` | Backend | Kafka/log/API에 사용 | 계좌번호 원문 대체 식별자 |

`order_id`, `request_id`, `client_order_id`, `idempotency_key_hash`는 같은 값으로 합치지 않는다. 각 값은 추적, 사용자 조회, 외부 제출 중복 방지, 재시도 판정이라는 책임이 다르다.

Idempotency 처리 규칙:

- 모든 주문 생성 요청은 `Idempotency-Key`를 포함해야 한다.
- 같은 `Idempotency-Key`와 같은 request body hash는 기존 `order_id`와 현재 상태를 반환한다.
- 같은 `Idempotency-Key`와 다른 request body hash는 `409 Conflict`로 거부한다.
- raw `Idempotency-Key`는 수신 직후 hash로 변환하고, 이후 로그와 Kafka에는 hash 또는 derived ID만 남긴다.
- client timeout 후 사용자가 같은 의도로 재시도하면 기존 `order_id`를 재조회해야 하며 새 주문을 만들면 안 된다.

## 3. 상태 모델

통합 스펙의 주문 상태를 그대로 canonical 상태로 사용한다.

| 상태 | 의미 | 사용자 표시 | 주요 진입 조건 |
| --- | --- | --- | --- |
| `RECEIVED` | Backend가 주문 요청을 접수하고 DB에 기록함 | 주문 접수됨 | idempotency와 기본 request shape 통과 |
| `PUBLISHED` | 주문 command가 Kafka에 발행됨 | 주문 접수됨 또는 처리 중 | outbox publisher 발행 성공 |
| `REJECTED` | 기본 검증 또는 KIS 명시적 거부 | 주문 거부됨 | schema 오류, KIS 명시적 거부 |
| `RISK_REJECTED` | 주문 가능 금액, 보유 수량, 권한, 한도 검증 실패 | 주문 거부됨 | Trading/Risk authoritative decision |
| `SUBMITTING` | KIS Broker Adapter가 KIS 제출 처리 중 | 제출 중 | Adapter가 command 처리 시작 |
| `SUBMITTED` | KIS 주문 API가 정상 접수 응답을 반환함 | 주문 제출됨 | KIS `rt_cd=0` |
| `SUBMIT_FAILED_UNKNOWN` | KIS 접수 여부를 알 수 없음 | 주문 확인 중 | timeout, connection reset, 불명확한 5xx |
| `PARTIALLY_FILLED` | 일부 수량 체결 | 일부 체결 | KIS 주문/체결내역 대사 |
| `FILLED` | 전량 체결 | 체결 완료 | KIS 체결내역 확정 |
| `CANCELED` | 주문 취소 확정 | 취소됨 | KIS 취소/잔량 상태 확인 |
| `RECONCILIATION_REQUIRED` | 내부 상태와 KIS 상태가 충돌 | 확인 중 또는 확인 필요 | 수량/가격/상태 불일치 |
| `FAILED` | 재시도 불가능한 시스템 실패 | 실패 | 복구 불가능한 내부 실패 |

허용 전이:

```text
RECEIVED -> PUBLISHED
RECEIVED -> REJECTED
PUBLISHED -> SUBMITTING
SUBMITTING -> SUBMITTED
SUBMITTING -> REJECTED
SUBMITTING -> RISK_REJECTED
SUBMITTING -> SUBMIT_FAILED_UNKNOWN
SUBMITTING -> FAILED
SUBMITTED -> PARTIALLY_FILLED
SUBMITTED -> FILLED
SUBMITTED -> CANCELED
PARTIALLY_FILLED -> FILLED
PARTIALLY_FILLED -> CANCELED
SUBMIT_FAILED_UNKNOWN -> SUBMITTED
SUBMIT_FAILED_UNKNOWN -> PARTIALLY_FILLED
SUBMIT_FAILED_UNKNOWN -> FILLED
SUBMIT_FAILED_UNKNOWN -> REJECTED
SUBMIT_FAILED_UNKNOWN -> RECONCILIATION_REQUIRED
RECONCILIATION_REQUIRED -> SUBMITTED
RECONCILIATION_REQUIRED -> PARTIALLY_FILLED
RECONCILIATION_REQUIRED -> FILLED
RECONCILIATION_REQUIRED -> CANCELED
```

`SUBMIT_FAILED_UNKNOWN`과 `RECONCILIATION_REQUIRED`는 사용자가 실패로 단정할 수 없는 상태다. 사용자 문구는 `주문 확인 중` 계열로 유지하고, 운영 알림과 KIS 조회로 해소한다.

## 4. Kafka 계약

주문 관련 topic은 통합 스펙의 canonical topic만 사용한다.

| Topic | Producer | Consumer | Key | Retention 기준 | 목적 |
| --- | --- | --- | --- | --- | --- |
| `orders.commands.v1` | Backend Outbox Publisher | KIS Broker Adapter | `account_alias:symbol` | 90일 | 주문/취소/정정 command |
| `broker.submit-results.v1` | KIS Adapter Outbox Publisher | API/Audit/WebSocket writer, read model projector | `account_alias:symbol` | 90일 | KIS 제출 성공/거부/불명 결과 |
| `broker.order-events.v1` | Broker Event Listener/Reconciler | API/Audit/WebSocket writer, read model projector | `account_alias:symbol` | 90일 | broker 주문/체결 event와 제한된 대사 결과 |
| `orders.dlq.v1` | Adapter/API/consumer | 운영자 재처리 도구 | 원본 message key | 180일 | schema 오류, 권한 불일치, 재시도 초과 |

모든 Kafka 메시지는 envelope을 사용한다.

```json
{
  "schema_version": 1,
  "event_type": "order.submit.requested",
  "event_id": "evt_01",
  "request_id": "req_01",
  "order_id": "ord_01",
  "client_order_id": "coid_01",
  "account_alias": "demo-account",
  "occurred_at": "2026-06-25T00:00:00.000Z",
  "producer": "backend-api",
  "env": "demo",
  "source": "order-api",
  "payload": {}
}
```

금지 필드:

- KIS appkey
- KIS appsecret
- access token
- 계좌번호 원문
- raw idempotency key

주문 command payload는 국내/해외 공통 파이프라인을 유지하고 KIS payload 차이는 adapter 변환에서 처리한다.

```json
{
  "market": "overseas",
  "symbol": "AAPL",
  "side": "buy",
  "qty": "1",
  "price": "145.00",
  "exchange": "NASD",
  "order_division": "00"
}
```

```json
{
  "market": "domestic",
  "symbol": "005930",
  "side": "buy",
  "qty": "1",
  "price": "70000",
  "exchange": "KRX",
  "order_division": "00",
  "sell_type": "",
  "condition_price": ""
}
```

Consumer 규칙:

- `enable.auto.commit=false`를 사용한다.
- 메시지 처리와 PostgreSQL transaction이 성공한 뒤 offset을 commit한다.
- DB commit 후 offset commit 전에 프로세스가 죽으면 같은 메시지가 재전달될 수 있다.
- 재전달은 `event_id`, `request_id`, `client_order_id`, 상태 전이 검사로 멱등 처리한다.
- schema 불일치, 파싱 불가, 재시도 초과는 `orders.dlq.v1`로 보낸다.

## 5. PostgreSQL 원장, Projection, Outbox

PostgreSQL은 Kafka를 대체하는 서비스 간 이벤트 로그가 아니다. PostgreSQL은 단일 주문의 transaction boundary, idempotency 판정, append-only 원장, transactional outbox, 최신 projection을 담당한다. Transactional outbox는 Kafka-first와 충돌하지 않는다. API/KIS Adapter가 DB commit과 Kafka publish를 하나의 원자 작업처럼 다룰 수 없기 때문에, outbox row를 transaction에 포함하고 별도 publisher가 Kafka topic으로 내보내는 방식으로 Kafka event spine의 유실을 막는다.

| Table | 역할 | 핵심 제약 |
| --- | --- | --- |
| `orders` | 주문 최신 상태 projection | `order_id`, idempotency 기준 unique |
| `order_events` | append-only 상태 변경 원장 | `event_id` unique |
| `broker_submissions` | KIS 제출 시도와 redacted 응답 | `submission_id`, `request_id`, `client_order_id` |
| `executions` | 체결 결과 projection | `execution_id` unique |
| `outbox_events` | Kafka 발행 대상 이벤트 | `event_id` unique, `published_at` |
| `dlq_events` | 실패 메시지와 재처리 이력 | 원본 topic/partition/offset |
| `reconciliation_runs` | 대사 실행 이력 | `run_id` unique |

Backend 주문 접수 transaction은 최소한 다음을 함께 commit한다.

1. `orders`에 `RECEIVED` 저장 또는 기존 주문 조회
2. `order_events`에 접수 이벤트 append
3. `outbox_events`에 `orders.commands.v1` 발행 대상 저장

KIS Adapter 제출 결과 transaction은 최소한 다음을 함께 commit한다.

1. `orders` 최신 상태 갱신
2. `order_events` append
3. `broker_submissions` append
4. `outbox_events`에 `broker.submit-results.v1` 발행 대상 저장

Broker Event Listener/Reconciler transaction은 최소한 다음을 함께 commit한다.

1. `orders` 최신 상태 보정 또는 `RECONCILIATION_REQUIRED` 기록
2. `order_events` append
3. `executions` append 또는 update
4. `reconciliation_runs` append
5. `outbox_events`에 `broker.order-events.v1` 발행 대상 저장

Outbox publisher는 `outbox_events`를 읽어 Kafka에 발행하고 성공한 row에 `published_at`을 기록한다. publisher가 죽어도 미발행 row가 남아 재발행 가능해야 한다. Kafka에 발행된 뒤에는 해당 topic이 서비스 간 공식 계약이며, DB outbox는 발행 보증과 재발행 추적을 위한 내부 구현 세부사항이다.

## 6. KIS Adapter와 Retry 정책

KIS Broker Adapter 책임:

- `orders.commands.v1`을 consume한다.
- envelope, schema version, `market`, `env`, payload 필수 필드를 검증한다.
- `request_id` 또는 `client_order_id`가 이미 제출 처리되었는지 DB에서 확인한다.
- Trading/Risk decision과 kill switch 상태를 KIS POST 직전에 확인한다.
- 주문 상태를 `SUBMITTING`으로 기록한다.
- `market=overseas`는 해외 주문 API payload로 변환한다.
- `market=domestic`은 국내 주문 API payload로 변환한다.
- KIS 응답 원문은 redaction 후 `broker_submissions`에 저장한다.
- DB transaction 성공 후 Kafka offset을 commit한다.

KIS 응답 분류:

| 상황 | 내부 상태 | 재시도 |
| --- | --- | --- |
| HTTP 200 + `rt_cd=0` | `SUBMITTED` | 불필요 |
| HTTP 200 + `rt_cd!=0` | `REJECTED` | 금지 |
| HTTP 4xx | `REJECTED` 또는 `FAILED` | 기본 금지 |
| HTTP 429 | `SUBMITTING` 유지 후 제한적 backoff | 주문 미접수 확신 시만 허용 |
| HTTP 5xx 중 미접수 확정 가능 | `SUBMITTING` 유지 후 제한적 backoff | retry budget 안에서 허용 |
| 불명확한 5xx | `SUBMIT_FAILED_UNKNOWN` | 즉시 재POST 금지 |
| token expired | token refresh 후 1회 재시도 | 허용 |
| timeout/connection reset | `SUBMIT_FAILED_UNKNOWN` | 즉시 재POST 금지 |

주문 API retry budget과 조회 API retry budget은 분리한다. KIS 조회 API 실패가 주문 POST 재시도로 이어지면 안 된다.

## 7. Broker Event와 상태 수렴

Broker Event Listener/Reconciler는 broker가 제공하는 주문/체결 event를 primary path로 반영한다. KIS 주문/체결내역 조회는 event 누락, timeout, open/unknown 상태 보정을 위한 bounded fallback이다.

- 해외 주문은 KIS 해외 주문/체결내역 조회를 사용한다.
- 국내 주문은 국내 주문/체결내역 조회 adapter를 별도 구현한다.
- broker event와 제한된 조회 결과는 `broker.order-events.v1`로 발행한다.
- `SUBMIT_FAILED_UNKNOWN` 주문이 KIS 조회에서 발견되면 실제 상태에 맞게 `SUBMITTED`, `PARTIALLY_FILLED`, `FILLED`, `CANCELED`로 보정한다.
- KIS에는 있는데 내부에 없는 주문은 운영 알림 대상으로 올린다.
- 내부 주문과 KIS 조회 결과의 수량, 가격, 상태가 충돌하면 `RECONCILIATION_REQUIRED`로 올린다.
- 같은 KIS 결과를 반복 조회해도 `execution_id`, broker order reference, 상태 전이 검사로 중복 이벤트를 막는다.

Bounded reconciliation 규칙:

- 조회 대상은 `SUBMIT_FAILED_UNKNOWN`, `RECONCILIATION_REQUIRED`, open order, 장기 미종결 주문으로 제한한다.
- `FILLED`, `CANCELED`, `REJECTED`, `FAILED` 확정 주문은 조회 대상에서 제외한다.
- 가능하면 주문별 조회가 아니라 계좌별 주문/체결내역 window 조회로 묶는다.
- 계좌별, adapter별, 환경별 rate budget을 둔다.
- 반복 실패는 adaptive backoff와 jitter를 적용하고, rate limit 초과가 반복되면 circuit breaker를 연다.
- 조회 API retry budget은 주문 POST retry budget과 분리한다.

## 8. Read Model과 Frontend 상태 반영

Frontend는 `order_id` 기준으로 WebSocket 상태 변경을 수신한다. 새로고침 후에도 같은 `order_id`로 최신 상태를 조회할 수 있어야 한다.

Read model 반영 기준:

- `broker.submit-results.v1`는 제출 성공, 명시적 거부, 제출 여부 불명 상태를 반영한다.
- `broker.order-events.v1`는 일부 체결, 전량 체결, 취소, 대사 불일치를 반영한다.
- Projector가 중단되면 Kafka offset 기준으로 재시작해 누락 없이 따라잡아야 한다.
- WebSocket push는 read model 반영 이후 또는 같은 transactionally safe boundary 이후 수행한다.

사용자 표시 기준:

- `RECEIVED`, `PUBLISHED`: 주문 접수됨 또는 처리 중
- `SUBMITTING`: 제출 중
- `SUBMITTED`: 주문 제출됨
- `SUBMIT_FAILED_UNKNOWN`, `RECONCILIATION_REQUIRED`: 주문 확인 중
- `PARTIALLY_FILLED`: 일부 체결
- `FILLED`: 체결 완료
- `CANCELED`: 취소됨
- `REJECTED`, `RISK_REJECTED`: 주문 거부됨
- `FAILED`: 실패

`SUBMITTED` 화면에서는 체결 수량, 잔량, 평균 체결가를 최종값처럼 표시하지 않는다. 체결 관련 값은 KIS 주문/체결내역 대사 결과가 들어온 뒤 갱신한다.

## 9. 보안과 권한

- KIS secret은 KIS Broker Adapter만 접근한다.
- Backend, Frontend, Chart Engine은 KIS secret을 알면 안 된다.
- API response, Kafka payload, frontend state, 로그에는 KIS appkey, KIS appsecret, access token, 전체 계좌번호, raw idempotency key를 남기지 않는다.
- `account_alias`는 사용자와 운영자가 추적 가능한 안전한 계좌 식별자로 사용한다.
- JWT role은 `user`, `trader`, `admin`으로 둔다.
- `user`는 조회 권한만 가진다.
- `trader`는 조회, 모의투자 주문, 허용된 실전 주문 권한을 가진다.
- `admin`은 운영/관리 권한을 가진다.
- 실전 주문은 `trader` role만으로 허용하지 않고 계좌별/사용자별 trading permission, kill switch, rate limit을 추가로 통과해야 한다.
- DLQ 재처리 권한은 주문 생성 권한과 분리한다.

## 10. Acceptance Criteria

- 같은 주문 요청을 100회 재시도해도 DB 주문 row와 KIS 제출이 중복되지 않는다.
- 같은 idempotency key와 다른 body는 `409 Conflict`로 거부된다.
- KIS 제출 성공, 명시적 거부, timeout이 각각 `SUBMITTED`, `REJECTED`, `SUBMIT_FAILED_UNKNOWN`으로 기록된다.
- timeout 이후 같은 주문을 즉시 재POST하지 않는다.
- DB commit 후 Kafka offset commit 전 프로세스가 죽어도 재처리 시 중복 KIS 제출이 발생하지 않는다.
- `broker.submit-results.v1`, `broker.order-events.v1` replay로 주문 read model을 복구할 수 있다.
- KIS 주문/체결내역 조회 결과로 `SUBMIT_FAILED_UNKNOWN`을 해소할 수 있다.
- Kafka 메시지, DB 예시, 로그 예시에 secret, token, 계좌번호 원문, raw idempotency key가 없다.
- Frontend는 `SUBMITTED`를 `FILLED`처럼 표시하지 않는다.
- `RECONCILIATION_REQUIRED`, DLQ 급증, outbox 미발행 누적, 반복 timeout은 운영 알림으로 이어진다.
