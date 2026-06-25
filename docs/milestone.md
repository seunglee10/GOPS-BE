# KIS 주문 처리 구체적인 마일스톤

작성일: 2026-06-25 KST

이 문서는 [기능 스펙](./spec.md)을 실제 구현 가능한 단위로 나눈다. 구현 대상은 국내/해외 주문 공통 Kafka 파이프라인, KIS Broker Adapter, PostgreSQL 영속성, KIS 주문/체결내역 대사다. 전체 흐름은 [주문처리 아키텍처](./architecture.md)를 따른다.

## 현재 구현된 실행 명령

| 명령 | 목적 |
| --- | --- |
| `kis-overseas emit-sample-command` | KIS 호출 없이 Kafka 주문 command 샘플 발행 |
| `kis-overseas db-init` | PostgreSQL 주문/이벤트/outbox/대사 테이블 생성 |
| `kis-overseas broker-adapter` | `orders.commands.v1` consume 후 KIS 주문 제출 및 DB/outbox 기록. `--fake-kis`, `--crash-before-process`, `--crash-after-process`로 안전 smoke 가능 |
| `kis-overseas outbox-publish` | `outbox_events`의 미발행 이벤트를 Kafka로 발행 |
| `kis-overseas dlq-reprocess` | DLQ 이벤트의 원본 command를 patch 후 다시 Kafka command로 발행 |
| `kis-overseas poll-order-events` | KIS 주문/체결내역 조회 후 `SUBMIT_FAILED_UNKNOWN` 주문 대사. `--fake-kis`로 안전 smoke 가능 |
| `kis-overseas ops-metrics` | 주문 상태, outbox, DLQ, 대사 불일치, Kafka lag 지표 조회 |

## M1. 주문 Command 계약 확정

### 목표

현재 CLI가 발행하는 `orders.commands.v1` 메시지를 국내/해외 공통 계약으로 고정한다. 이후 backend API가 붙어도 같은 topic과 payload를 사용하게 한다.

### 구현 범위

- `schema_version=1` envelope을 공식 계약으로 문서화한다.
- Kafka key를 `account_alias:symbol`로 고정한다.
- `payload.market`은 `domestic` 또는 `overseas`만 허용한다.
- 국내/해외 공통 필드: `symbol`, `side`, `qty`, `price`, `exchange`, `order_division`.
- 국내 전용 필드: `sell_type`, `condition_price`.
- 계좌번호 원문, secret, token은 command에 넣지 않는다.

### 완료 기준

- 현재 `kis_trader.kafka_producer`가 만드는 메시지가 스펙과 충돌하지 않는다.
- 국내/해외 주문 샘플 JSON이 문서와 테스트 fixture로 동일하게 쓰일 수 있다.
- 잘못된 `market`, 누락된 필드, numeric string이 아닌 `qty`/`price`를 거부하는 기준이 있다.

### 테스트 시나리오

- 해외 주문 command sample이 schema 검증을 통과한다.
- 국내 주문 command sample이 schema 검증을 통과한다.
- `account_alias:symbol` key가 같은 계좌/종목 주문을 같은 partition 후보로 묶는다.
- secret 또는 계좌번호 원문이 payload에 있으면 실패한다.

## M2. KIS Broker Adapter Consumer

### 목표

`orders.commands.v1`을 consume해 KIS 주문 API를 호출하는 단일 책임 서비스를 만든다. API 서버나 CLI는 KIS에 직접 주문을 제출하지 않는다.

### 구현 범위

- consumer group: `kis-broker-adapter`.
- `enable.auto.commit=false`.
- message validation 후 `orders` 상태를 `SUBMITTING`으로 전이한다.
- `market=overseas`는 해외 주문 API body로 변환한다.
- `market=domestic`은 국내 현금주문 API body로 변환한다.
- token expired는 token refresh 후 1회만 재시도한다.
- timeout/connection reset은 `SUBMIT_FAILED_UNKNOWN`으로 기록하고 즉시 재POST하지 않는다.

### 완료 기준

- KIS 제출 책임이 adapter로 격리된다.
- 같은 `request_id` 재처리 시 이미 제출된 주문이면 KIS POST를 반복하지 않는다.
- KIS 응답 원문은 redaction 후 `broker_submissions`에 남는다.

### 테스트 시나리오

- consumer가 주문 command를 읽고 해외 KIS 주문 client를 호출한다.
- 국내 주문 command는 국내 KIS 주문 client를 호출한다.
- KIS 명시적 거부는 `REJECTED`가 된다.
- KIS timeout은 `SUBMIT_FAILED_UNKNOWN`이 되고 재POST하지 않는다.
- DB commit 전 kill이면 offset이 commit되지 않아 메시지가 재처리된다.

## M3. PostgreSQL 영속성과 Outbox

### 목표

KIS 제출 시도, 주문 상태 전이, Kafka 후속 이벤트를 PostgreSQL에 원자적으로 기록한다.

### 구현 범위

- `orders`: 주문 최신 상태 projection.
- `order_events`: append-only 상태 변경 이벤트.
- `broker_submissions`: KIS 제출 시도, HTTP status, KIS 응답 코드, redacted response.
- `outbox_events`: `broker.submit-results.v1` 또는 `orders.dlq.v1` 발행 대상.
- `reconciliation_runs`: 대사 작업 실행 이력.
- `request_id`, `event_id`, `submission_id`에 unique constraint를 둔다.

### 완료 기준

- DB transaction 안에서 상태 변경과 outbox insert가 함께 commit된다.
- outbox publisher가 발행 성공 후 `published_at`을 기록한다.
- DB commit 후 offset commit 전에 죽어도 재처리 시 중복 row가 생기지 않는다.

### 테스트 시나리오

- 같은 `event_id` insert는 unique constraint로 차단된다.
- DB commit 후 outbox publisher가 죽어도 미발행 이벤트가 남아 있다.
- offset commit 실패 후 같은 메시지를 재처리해도 KIS POST가 중복 실행되지 않는다.
- redaction 테스트에서 token, appsecret, 계좌번호 원문이 저장되지 않는다.

## M4. KIS 주문/체결내역 Poller와 대사

### 목표

KIS가 가진 외부 주문 상태와 내부 PostgreSQL 상태를 주기적으로 맞춘다. 특히 `SUBMIT_FAILED_UNKNOWN` 상태를 조회 결과로 해소한다.

### 구현 범위

- 해외 주문은 현재 `ccnl` 기능의 `/uapi/overseas-stock/v1/trading/inquire-ccnl` 조회를 사용한다.
- 국내 주문/체결내역 조회 adapter 메서드를 추가하는 것을 전제로 문서와 테스트를 준비한다.
- poller는 조회 결과를 `broker.order-events.v1`로 발행한다.
- reconciler는 KIS 결과와 `orders`, `broker_submissions`를 비교한다.
- 자동 보정 가능한 경우 `orders.reconciled.v1`을 발행한다.
- 매칭 불가 또는 충돌은 `RECONCILIATION_REQUIRED`로 올린다.

### 완료 기준

- `SUBMIT_FAILED_UNKNOWN` 주문이 KIS 조회에서 발견되면 내부 상태가 보정된다.
- KIS에는 있는데 내부에 없는 주문은 운영 알림 대상이 된다.
- 내부에는 있는데 KIS에 없는 주문은 재조회 후에도 없을 때 수동 확인 상태로 남긴다.

### 테스트 시나리오

- timeout 주문이 KIS 조회 결과로 `SUBMITTED`가 된다.
- 일부 체결 수량이 들어오면 `PARTIALLY_FILLED`가 된다.
- 전체 체결 수량이 들어오면 `FILLED`가 된다.
- 수량/가격이 내부 주문과 다르면 `RECONCILIATION_REQUIRED`가 된다.
- poller 재실행 시 같은 KIS 결과가 중복 이벤트로 쌓이지 않는다.

## M5. 장애, 중복, DLQ, 운영 기준

### 목표

서버가 죽거나 외부 API가 흔들려도 주문 데이터가 유실되지 않고 상태가 결정적으로 수렴하는지 검증한다.

### 구현 범위

- KIS Adapter kill/restart 테스트.
- DB commit 전/후 kill 테스트.
- Kafka rebalance 중 재처리 테스트.
- KIS timeout, HTTP 429, HTTP 5xx, 명시적 거부 분류 테스트.
- `orders.dlq.v1` 메시지 포맷과 재처리 기준 정의.
- 운영 metric: consumer lag, KIS timeout count, reconciliation mismatch count, DLQ count.

### 완료 기준

- 같은 주문 요청 100회 재처리에도 실전 KIS POST는 1회 이하로 유지된다.
- timeout 후 재POST 금지 정책이 테스트로 검증된다.
- DLQ 메시지는 원본 topic/partition/offset, error type, retryable 여부를 포함한다.
- 대사 불일치가 1건 이상이면 알림 대상이 된다.

### 테스트 시나리오

- KIS Adapter가 KIS POST 전 죽으면 offset 미커밋으로 재처리된다.
- KIS POST 성공 후 DB 기록 전에 죽은 경우 대사로 복구된다.
- DB commit 성공 후 offset commit 전에 죽으면 재처리되지만 unique key로 중복 저장이 막힌다.
- parsing 불가 메시지는 DLQ로 이동한다.
- DLQ 재처리 성공 시 원본 주문 상태가 정상 전이된다.
