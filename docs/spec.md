# KIS 주문 처리 기능 스펙

작성일: 2026-06-25 KST

이 문서는 국내/해외 주식 주문을 하나의 Kafka 기반 파이프라인으로 처리하기 위한 기능 스펙이다. 현재 저장소는 Python 패키지 `kis_trader`로 KIS 주문 preview, Kafka 주문 command 발행, 해외 주문/체결내역 조회(`ccnl`)를 제공한다. 다음 단계의 목표는 Kafka 명령을 소비하는 KIS Broker Adapter, PostgreSQL 영속성, 주문/체결내역 대사를 붙여 서버 장애에도 주문 상태가 유실되지 않게 만드는 것이다.

관련 문서:

- [구체적인 마일스톤](./milestone.md)
- [주문처리 아키텍처](./architecture.md)

## 목표

- 사용자가 주문 버튼을 누른 뒤 backend가 주문 명령을 Kafka에 기록하고, KIS Broker Adapter가 실제 KIS API 호출을 전담한다.
- 국내/해외 주문은 공통 command envelope을 사용하고, KIS endpoint와 payload 차이는 `market`별 adapter 변환에서 처리한다.
- KIS 제출 결과, KIS 주문/체결내역 조회 결과, 내부 주문 상태 전이를 PostgreSQL에 append-only로 기록한다.
- Kafka consumer 재시작, DB commit 후 offset commit 실패, KIS timeout, KIS 명시적 거부를 상태 모델 안에서 복구 가능하게 처리한다.
- 실전 주문에서는 timeout 이후 같은 주문을 즉시 재POST하지 않고 `SUBMIT_FAILED_UNKNOWN` 상태로 두고 대사한다.

## 비목표

- 내부 Matching Engine으로 실전 체결을 결정하지 않는다.
- frontend UI 구현은 이 스펙의 직접 범위가 아니다.
- PostgreSQL은 로컬 Compose 개발 환경에 포함되어 있으며, 운영 수준 HA/backup 구성은 별도 단계로 진행한다.

## 주문 상태 모델

| State | 의미 |
| --- | --- |
| `RECEIVED` | backend가 사용자 주문 의도를 접수했고 내부 주문 ID를 만든 상태 |
| `VALIDATED` | 권한, 입력값, 주문 가능 조건, idempotency 검사를 통과한 상태 |
| `SUBMITTING` | KIS Broker Adapter가 KIS 제출을 시도 중인 상태 |
| `SUBMITTED` | KIS가 주문 접수를 명시적으로 성공 처리한 상태 |
| `REJECTED` | 내부 검증 또는 KIS 응답으로 주문이 최종 거부된 상태 |
| `SUBMIT_FAILED_UNKNOWN` | KIS POST 결과를 알 수 없는 상태. 재POST 금지, 대사 필요 |
| `PARTIALLY_FILLED` | 일부 수량이 체결된 상태 |
| `FILLED` | 전체 수량이 체결된 상태 |
| `CANCELED` | 주문이 취소된 상태 |
| `RECONCILIATION_REQUIRED` | 내부 상태와 KIS 조회 결과가 충돌해 수동 확인이 필요한 상태 |

허용 전이:

```text
RECEIVED -> VALIDATED
RECEIVED -> REJECTED
VALIDATED -> SUBMITTING
SUBMITTING -> SUBMITTED
SUBMITTING -> REJECTED
SUBMITTING -> SUBMIT_FAILED_UNKNOWN
SUBMITTED -> PARTIALLY_FILLED
SUBMITTED -> FILLED
SUBMITTED -> CANCELED
PARTIALLY_FILLED -> FILLED
PARTIALLY_FILLED -> CANCELED
SUBMIT_FAILED_UNKNOWN -> SUBMITTED
SUBMIT_FAILED_UNKNOWN -> REJECTED
SUBMIT_FAILED_UNKNOWN -> RECONCILIATION_REQUIRED
```

## Kafka Topics

| Topic | Key | Producer | Consumer | 목적 |
| --- | --- | --- | --- | --- |
| `orders.commands.v1` | `account_alias:symbol` | backend 또는 현재 CLI | KIS Broker Adapter | 주문 제출 명령 |
| `broker.submit-results.v1` | `account_alias:symbol` | KIS Broker Adapter | Persistence Writer | KIS POST 제출 결과 |
| `broker.order-events.v1` | `account_alias:symbol` | KIS Poller/Reconciler | Persistence Writer | KIS 주문/체결내역 조회 결과 |
| `orders.reconciled.v1` | `account_alias:symbol` | Reconciliation Service | backend/read model | 대사 후 확정된 주문 상태 |
| `orders.dlq.v1` | `original_topic:partition:offset` | 모든 consumer | DLQ Processor | 처리 불가 메시지 보관 |

순서 보장 범위는 `account_alias:symbol`이다. 같은 계좌의 같은 종목 주문/취소/정정 순서는 같은 Kafka partition에서 보장한다. 서로 다른 종목 사이의 전역 순서는 보장하지 않는다.

## 공통 메시지 Envelope

모든 Kafka 메시지는 다음 공통 필드를 가진다.

```json
{
  "schema_version": 1,
  "event_type": "order.submit.requested",
  "event_id": "uuid",
  "request_id": "uuid",
  "occurred_at": "2026-06-25T00:00:00.000Z",
  "producer": "kis-trader-cli",
  "env": "demo",
  "account_alias": "demo-account",
  "payload": {}
}
```

금지 필드:

- KIS appkey
- KIS appsecret
- access token
- 계좌번호 원문
- raw idempotency key

## 주문 Command Payload

현재 `kis_trader.kafka_producer`가 발행하는 command를 기준으로 국내/해외 공통 계약을 유지한다.

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

국내 주문은 같은 envelope 안에서 `market: domestic`을 사용하고 국내 전용 필드를 추가한다.

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

## KIS Broker Adapter 책임

- `orders.commands.v1`을 consume한다.
- 메시지 schema, `market`, `env`, 필수 payload를 검증한다.
- 같은 `request_id` 또는 내부 주문 ID가 이미 제출 처리 중인지 PostgreSQL에서 확인한다.
- 주문 상태를 `SUBMITTING`으로 기록한다.
- `market=overseas`이면 `/uapi/overseas-stock/v1/trading/order`로 변환한다.
- `market=domestic`이면 `/uapi/domestic-stock/v1/trading/order-cash`로 변환한다.
- KIS 응답을 분류하고 `broker_submissions`에 원문 redaction 후 기록한다.
- 제출 결과 이벤트를 outbox에 저장하고 `broker.submit-results.v1`로 발행한다.
- DB transaction이 성공한 뒤 Kafka offset을 commit한다.

## KIS 응답 분류

| 상황 | 내부 상태 | 재시도 |
| --- | --- | --- |
| HTTP 200 + `rt_cd=0` | `SUBMITTED` | 불필요 |
| HTTP 200 + `rt_cd!=0` | `REJECTED` | 금지 |
| HTTP 4xx | `REJECTED` 또는 DLQ | 기본 금지 |
| HTTP 429 | `SUBMITTING` 유지 후 backoff | 제한적 허용 |
| HTTP 5xx | `SUBMITTING` 유지 후 backoff | 제한적 허용 |
| token expired | token refresh 후 1회 재시도 | 허용 |
| timeout/connection reset | `SUBMIT_FAILED_UNKNOWN` | 즉시 재POST 금지 |

timeout은 가장 위험한 케이스다. KIS가 주문을 접수했는지 알 수 없으므로 같은 주문을 다시 POST하면 중복 주문이 될 수 있다. 이 상태는 KIS 주문/체결내역 조회로만 해소한다.

## PostgreSQL 테이블 기준

| Table | 목적 | 핵심 unique key |
| --- | --- | --- |
| `orders` | 주문 최신 상태 projection | `order_id`, `request_id` |
| `order_events` | append-only 주문 상태 이벤트 | `event_id` |
| `broker_submissions` | KIS 제출 시도와 응답 기록 | `submission_id`, `request_id` |
| `outbox_events` | DB commit 후 Kafka 발행 대상 | `event_id` |
| `reconciliation_runs` | KIS 주문/체결내역 대사 실행 이력 | `run_id` |

`orders`는 조회 최적화용 최신 상태이고, 근거 데이터는 `order_events`, `broker_submissions`, `reconciliation_runs`에 남긴다.

## Outbox 처리

KIS Adapter가 DB와 Kafka를 동시에 갱신해야 할 때는 PostgreSQL transaction 안에서 다음을 함께 수행한다.

1. `orders` 최신 상태 갱신
2. `order_events` append
3. `broker_submissions` append
4. `outbox_events` append

그 뒤 outbox publisher가 `outbox_events`를 읽어 Kafka로 발행한다. Kafka 발행 성공 시 `published_at`을 기록한다. 이 구조는 DB commit 후 프로세스가 죽어도 발행할 이벤트를 잃지 않게 한다.

## 주문/체결내역 대사

KIS Poller/Reconciler는 주기적으로 KIS 주문/체결내역 조회를 수행한다.

- 해외 주문은 현재 `KisOverseasClient.order_history()`가 사용하는 `/uapi/overseas-stock/v1/trading/inquire-ccnl` 경로를 기준으로 한다.
- 국내 주문/체결내역 조회는 국내용 KIS endpoint를 별도 adapter 메서드로 추가하는 것을 전제로 한다.
- 조회 결과는 `broker.order-events.v1`로 발행하고 PostgreSQL의 내부 주문 상태와 비교한다.
- `SUBMIT_FAILED_UNKNOWN` 주문이 KIS 조회에서 발견되면 `SUBMITTED`, `PARTIALLY_FILLED`, `FILLED` 중 실제 상태로 보정한다.
- 내부 주문과 KIS 주문이 매칭되지 않거나 수량/가격이 충돌하면 `RECONCILIATION_REQUIRED`로 올린다.

## Consumer Offset 규칙

- `enable.auto.commit=false`를 사용한다.
- 메시지 처리와 PostgreSQL transaction이 성공한 뒤 offset을 commit한다.
- DB commit 성공 후 offset commit 전에 죽으면 같은 메시지가 재처리된다.
- 재처리는 unique key와 상태 전이 검사로 멱등 처리한다.
- 파싱 불가, schema 불일치, 재시도 초과는 `orders.dlq.v1`로 보낸다.

## Acceptance Criteria

- 국내/해외 주문 command가 같은 envelope으로 처리된다.
- KIS 제출 성공/거부/timeout이 각각 `SUBMITTED`, `REJECTED`, `SUBMIT_FAILED_UNKNOWN`으로 기록된다.
- timeout 이후 즉시 같은 주문을 재POST하지 않는다.
- DB commit 후 offset commit 전 프로세스가 죽어도 재처리 시 중복 KIS 제출이 발생하지 않는다.
- KIS 주문/체결내역 조회 결과로 `SUBMIT_FAILED_UNKNOWN` 상태를 해소할 수 있다.
