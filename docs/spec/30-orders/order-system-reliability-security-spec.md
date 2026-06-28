# 주문 시스템 신뢰성 및 보안 기능명세서

## 1. 개요

주문 시스템은 사용자의 주문 요청을 안전하게 접수하고, Kafka를 통해 비동기 처리한 뒤, 한국투자증권 KIS API로 주문을 제출한다.

MVP에서는 KIS 모의투자 주문을 실제로 수행할 수 있게 한다. 실전 주문은 후속 단계로 분리하고, 실전 전환 전 kill switch, rate limit, 권한 검증, 대사 절차를 다시 검증한다.

시스템은 다음 상황에서도 주문 중복 제출과 주문 유실을 방지해야 한다.

- 사용자의 중복 클릭
- 프론트엔드/API 재시도
- API 서버 장애
- Kafka 메시지 재처리
- Consumer 장애
- KIS 주문 API timeout
- 체결 결과 지연
- 내부 DB와 KIS 상태 불일치

최종적으로 모든 주문 상태는 `Idempotency-Key`, Kafka 이벤트, append-only 원장, KIS 체결내역 대사를 통해 일관된 상태로 수렴해야 한다.

---

## 2. 핵심 설계 원칙

### 2.1 서버 멱등성 보장

프론트엔드 버튼 비활성화는 보조 수단이다.

주문 중복 방지는 반드시 서버에서 보장해야 한다.

모든 주문 요청은 `Idempotency-Key`를 포함해야 한다.

서버는 다음 기준으로 요청을 처리한다.

| 조건 | 처리 |
| --- | --- |
| 같은 `Idempotency-Key` + 같은 주문 body | 기존 주문 결과 반환 |
| 같은 `Idempotency-Key` + 다른 주문 body | `409 Conflict` 반환 |
| 새로운 `Idempotency-Key` | 신규 주문 접수 |

주문 body hash를 저장하고, DB unique constraint를 통해 중복 주문 생성을 차단한다.

---

### 2.2 주문은 즉시 체결 결과를 반환하지 않는다

API 응답은 최종 체결 결과가 아니라 주문 접수 상태를 반환한다.

예시 응답:

```json
{
  "order_id": "ord_123",
  "status": "RECEIVED"
}
```

사용자는 이후 `order_id` 기준으로 주문 상태를 조회하거나, WebSocket을 통해 상태 변경을 전달받는다.

---

### 2.3 KIS 주문 API timeout은 실패가 아니다

KIS 주문 API timeout은 “주문 실패”가 아니라 “주문 접수 여부 불명” 상태다.

따라서 timeout 또는 connection reset 발생 시 같은 주문을 즉시 재POST하면 안 된다.

중복 실전 주문이 발생할 수 있기 때문이다.

이 경우 주문 상태를 `SUBMIT_FAILED_UNKNOWN`으로 저장하고, KIS 주문체결내역 조회 또는 체결 WebSocket을 통해 실제 주문 접수 여부를 대사한다.

---

### 2.4 Kafka는 at-least-once 처리를 전제로 한다

Kafka 메시지는 중복 처리될 수 있다고 가정한다.

따라서 consumer는 다음 정책을 따른다.

- `enable.auto.commit=false`
- DB commit 성공 후 Kafka offset commit
- `event_id`, `order_id`, `client_order_id`, `execution_id` unique constraint 적용
- 같은 메시지가 여러 번 처리되어도 DB 상태가 중복 반영되지 않도록 설계

---

### 2.5 주문 상태는 append-only 원장으로 관리한다

`orders` 테이블은 조회용 최신 상태를 저장한다.

실제 상태 변경 이력은 `order_events`에 append-only 방식으로 저장한다.

주문 상태 변경, KIS 제출 결과, 체결 결과, 대사 결과는 모두 이벤트로 남겨야 한다.

---

## 3. 주문 상태 정의

| 상태 | 의미 |
| --- | --- |
| `RECEIVED` | API가 주문 요청을 접수함 |
| `PUBLISHED` | 주문 명령이 Kafka에 발행됨 |
| `REJECTED` | 기본 검증 실패로 거부됨 |
| `RISK_REJECTED` | 주문 가능 금액, 수량, 권한 등 risk 검증 실패 |
| `SUBMITTING` | KIS Adapter가 한투 주문 API 호출 중 |
| `SUBMITTED` | KIS 주문 API가 정상 응답함 |
| `SUBMIT_FAILED_UNKNOWN` | KIS timeout 또는 connection reset으로 주문 접수 여부 불명 |
| `PARTIALLY_FILLED` | 일부 체결됨 |
| `FILLED` | 전량 체결됨 |
| `CANCELED` | 주문 취소됨 |
| `RECONCILIATION_REQUIRED` | 내부 상태와 KIS 상태가 불일치하여 확인 필요 |
| `FAILED` | 재시도 불가능한 명확한 실패 |

---

## 4. 주문 처리 흐름

### 4.1 사용자 요청 단계

사용자가 주문 버튼을 여러 번 누르거나 네트워크 문제로 재시도하더라도 같은 주문이 중복 제출되면 안 된다.

요구사항:

- 모든 주문 요청은 `Idempotency-Key`를 포함해야 한다.
- 서버는 `Idempotency-Key`와 주문 body hash를 함께 저장해야 한다.
- 같은 key와 같은 body로 재요청하면 기존 `order_id`와 현재 상태를 반환해야 한다.
- 같은 key로 다른 주문 내용을 요청하면 `409 Conflict`를 반환해야 한다.
- 프론트엔드는 주문 요청 후 버튼을 비활성화할 수 있지만, 이는 보조 수단으로만 사용한다.

---

### 4.2 Backend API 단계

API 서버는 한투에 직접 주문을 보내지 않는다.

주문을 접수한 뒤 내부 DB에 상태를 저장하고 Kafka에 주문 명령을 발행한다.

요구사항:

- 주문 접수 시 `orders`와 `order_events`에 `RECEIVED` 상태를 저장한다.
- Kafka 발행 성공 시 `PUBLISHED` 상태를 저장한다.
- Kafka 발행 전 API 서버가 죽어도 주문이 유실되지 않아야 한다.
- Kafka 발행 후 응답 전 API 서버가 죽어도 재요청 시 기존 주문 상태를 반환해야 한다.
- API 응답은 최종 체결 결과가 아니라 `order_id`와 현재 상태를 반환해야 한다.

권장 설계:

- Outbox 패턴 사용
- `orders` 최신 상태 테이블
- `order_events` append-only 이벤트 테이블
- `outbox_events` Kafka 발행 대상 이벤트 테이블

---

### 4.3 Kafka 단계

Kafka는 주문 명령의 durable queue/event log 역할을 한다.

요구사항:

- producer는 `acks=all`을 사용한다.
- idempotent producer 설정을 사용한다.
- 메시지 key는 `account_alias:symbol` 형식으로 설정한다.
- consumer는 auto commit을 끄고, 처리 성공 후 offset을 commit한다.
- 잘못된 메시지는 조용히 버리지 않고 DLQ로 전송한다.

---

### 4.4 순서 보장

모든 주문의 전역 순서를 보장할 필요는 없다.

중요한 범위는 같은 계좌의 같은 종목에 대한 주문 순서다.

예를 들어 같은 계좌/종목에서 다음 명령 순서가 바뀌면 위험하다.

- 매수 후 취소
- 정정 후 취소
- 매도 후 정정
- 주문 후 체결 반영

요구사항:

- Kafka message key는 `account_alias:symbol`로 고정한다.
- 같은 계좌/종목의 주문, 취소, 정정 명령은 같은 partition에 들어가야 한다.
- 같은 partition은 single consumer가 순서대로 처리해야 한다.

---

### 4.5 Trading Service / Risk 단계

Trading Service는 주문을 KIS로 보내기 전에 주문 가능 여부를 검증한다.

검증 항목:

- 계좌 소유권
- 사용자 권한
- 실전/모의투자 환경
- 주문 가능 금액
- 보유 수량
- 주문 수량
- 가격 단위
- 시장 시간
- 종목별 주문 제한
- 시장가 주문 허용 여부

요구사항:

- risk 검증 실패 주문은 KIS로 보내지 않는다.
- 실패 사유를 명확히 저장한다.
- 실전 주문 권한과 모의투자 권한은 분리한다.
- consumer 재시작 또는 Kafka 재처리 상황에서도 같은 주문이 중복 제출되지 않아야 한다.

---

### 4.6 KIS Adapter 단계

KIS 주문 API 호출은 KIS Adapter 한 곳에서만 수행한다.

요구사항:

- KIS appkey, appsecret, access token은 Adapter에서만 접근한다.
- 프론트엔드, API 응답, Kafka payload, 로그에 secret이 노출되면 안 된다.
- Kafka payload에는 `account_alias`, `idempotency_key_hash`만 포함한다.
- 전체 계좌번호는 Kafka와 로그에 남기지 않는다.
- KIS timeout 또는 connection reset 발생 시 즉시 재POST하지 않는다.
- timeout 발생 시 상태를 `SUBMIT_FAILED_UNKNOWN`으로 저장한다.
- 이후 KIS 주문체결내역 조회 또는 체결 WebSocket으로 실제 주문 상태를 확인한다.
- KIS rate limit, token 만료, 5xx 응답에 대한 retry/backoff 정책을 둔다.

---

### 4.7 PostgreSQL 저장 단계

PostgreSQL은 주문의 내부 원장을 관리한다.

주요 테이블:

| 테이블 | 역할 |
| --- | --- |
| `orders` | 주문의 최신 상태 조회 |
| `order_events` | 주문 상태 변경 append-only 원장 |
| `broker_submissions` | KIS 주문 API 제출 이력 |
| `executions` | 체결 결과 저장 |
| `outbox_events` | Kafka 발행 대상 이벤트 |
| `dlq_events` | 실패 메시지 및 운영자 재처리 대상 |

필수 제약조건:

- `orders.client_order_id` unique
- `order_events.event_id` unique
- `broker_submissions.client_order_id` unique
- `broker_submissions.request_id` unique
- `executions.execution_id` unique
- `outbox_events.event_id` unique

요구사항:

- DB commit 후 offset commit 전 consumer가 죽어도 재처리 시 중복 저장되지 않아야 한다.
- 주문 상태 변경은 `order_events`에 누적 저장한다.
- `orders`는 최신 상태 조회 최적화를 위한 projection으로 사용한다.

---

### 4.8 체결 결과 수집 및 대사

KIS 주문 API 응답과 실제 체결 결과는 다를 수 있다.

따라서 주문 API 응답만으로 최종 상태를 확정하면 안 된다.

요구사항:

- 체결 결과는 KIS 주문체결내역 polling 또는 체결 WebSocket으로 별도 수집한다.
- 내부 주문 상태와 KIS 체결내역을 주기적으로 대사한다.
- KIS에는 있는데 내부 DB에 없는 주문은 보정 이벤트로 복구한다.
- 내부에는 있는데 KIS에는 없는 주문은 `RECONCILIATION_REQUIRED` 상태로 올린다.
- 대사 결과도 `order_events`에 append-only로 저장한다.

---

### 4.9 DLQ 처리

스키마 오류, 권한 불일치, 알 수 없는 KIS 응답처럼 자동 처리가 어려운 메시지는 DLQ로 보낸다.

요구사항:

- DLQ topic은 `orders.dlq.v1`로 운영한다.
- DLQ에는 원본 topic, partition, offset, key, error type, error message를 저장한다.
- DLQ 메시지는 운영자가 원인 확인 후 재처리할 수 있어야 한다.
- DLQ 수는 모니터링 지표로 관리한다.
- DLQ가 증가하면 알림을 발생시킨다.

---

### 4.10 프론트 상태 표시

사용자는 주문 직후 “체결 완료”가 아니라 현재 진행 상태를 봐야 한다.

표시 상태 예시:

| 사용자 표시 문구 | 내부 상태 |
| --- | --- |
| 주문 접수됨 | `RECEIVED` |
| 주문 제출 중 | `SUBMITTING` |
| 주문 확인 중 | `SUBMIT_FAILED_UNKNOWN` |
| 일부 체결됨 | `PARTIALLY_FILLED` |
| 체결 완료 | `FILLED` |
| 주문 거부됨 | `REJECTED`, `RISK_REJECTED` |
| 확인 필요 | `RECONCILIATION_REQUIRED` |

요구사항:

- 프론트는 `order_id` 기준으로 상태를 조회한다.
- WebSocket으로 주문 상태 변경을 전달받는다.
- timeout 상황을 단순 실패로 표시하지 않는다.
- 사용자가 실패로 오해하고 같은 주문을 다시 넣지 않도록 상태 문구를 분리한다.

---

## 5. 보안 요구사항

### 5.1 계좌 접근 제어

사용자는 본인에게 허용된 계좌만 조회하거나 주문할 수 있다.

요구사항:

- Gateway 또는 API 단계에서 account ownership을 검증한다.
- 사용자 role을 검증한다.
- trading environment를 검증한다.
- 주문 가능 권한을 검증한다.

권한 예시:

| 권한 | 설명 |
| --- | --- |
| `user` | 조회만 가능 |
| `trader` | 조회, 모의투자 주문, 허용된 실전 주문 가능 |
| `admin` | 운영 및 관리 권한 |

실전 주문은 `trader` role만으로 허용하지 않고 계좌별/사용자별 trading permission, kill switch, rate limit을 추가로 통과해야 한다.

---

### 5.2 민감정보 보호

민감정보는 Kafka, 로그, 프론트엔드에 노출되면 안 된다.

노출 금지 항목:

- KIS appkey
- KIS appsecret
- KIS access token
- 전체 계좌번호
- raw idempotency key
- 주민번호, 전화번호 등 개인식별정보

요구사항:

- Kafka payload에는 `account_alias`만 사용한다.
- `Idempotency-Key`는 hash로 저장한다.
- 로그 redaction을 적용한다.
- API 응답에 secret 또는 전체 계좌번호를 포함하지 않는다.
- KIS secret은 KIS Adapter만 접근한다.

---

## 6. 운영 안전장치

실전 주문은 장애나 버그 발생 시 금전 손실로 이어질 수 있다.

따라서 다음 안전장치를 제공해야 한다.

### 6.1 Kill Switch

- 실전 주문 kill switch를 제공한다.
- kill switch ON 상태에서는 KIS 주문 API를 호출하지 않는다.
- 차단된 주문은 명확한 거부 상태로 저장한다.

### 6.2 Circuit Breaker

- 계좌별 circuit breaker를 제공한다.
- KIS timeout, 5xx, rate limit, 대사 불일치가 급증하면 주문 제출을 차단한다.
- 차단 상태는 운영자 확인 후 해제한다.

### 6.3 주문 제한

- 사용자별 분당 주문 수 제한
- 계좌별 분당 주문 수 제한
- 일별 주문 금액 제한
- 종목별 주문 수량 제한
- 시장가 주문 제한
- 실전 주문 전 추가 확인 정책

### 6.4 모니터링 지표

다음 지표를 모니터링한다.

- Kafka consumer lag
- 주문 API timeout 수
- KIS 제출 실패 수
- `SUBMIT_FAILED_UNKNOWN` 주문 수
- DLQ 메시지 수
- 대사 불일치 수
- kill switch 발동 여부
- 주문 처리 latency
- 체결 결과 반영 latency

---

## 7. 테스트 항목

| 테스트 | 방법 | 기대 결과 |
| --- | --- | --- |
| 멱등성 테스트 | 같은 `Idempotency-Key`와 같은 body로 100회 동시 주문 요청 | DB 주문 row 1개, KIS submit 1회 |
| body hash 충돌 테스트 | 같은 key로 symbol, qty, price 중 하나를 변경해 요청 | `409 Conflict`, KIS submit 0회 |
| Kafka publish 전 API 장애 테스트 | 주문 접수 후 Kafka publish 전 API 프로세스 kill | 주문 유실 없음, 재처리 가능 |
| Kafka publish 후 응답 전 API 장애 테스트 | Kafka publish 후 API 응답 전 프로세스 kill | 재요청 시 기존 주문 상태 반환 |
| consumer DB commit 전 장애 테스트 | Kafka consume 후 DB commit 전 consumer kill | offset 미커밋, 재시작 후 재처리 |
| DB commit 후 offset commit 전 장애 테스트 | DB commit 후 offset commit 전 consumer kill | 재처리되어도 DB 중복 없음 |
| Kafka 중복 메시지 테스트 | 같은 Kafka message를 2회 처리 | unique constraint로 중복 반영 없음 |
| 순서 보장 테스트 | 같은 `account_alias:symbol`로 주문 → 취소 전송 | 같은 partition에서 순서 유지 |
| KIS timeout 테스트 | fake KIS client가 timeout 발생 | 재POST 없음, `SUBMIT_FAILED_UNKNOWN` 저장 |
| KIS 대사 테스트 | KIS mock history에만 주문이 존재하도록 구성 | 내부 보정 이벤트 생성 |
| 내부/KIS 불일치 테스트 | 내부에는 주문이 있으나 KIS에는 없도록 구성 | `RECONCILIATION_REQUIRED` 상태 전환 |
| 권한 테스트 | 타 계좌 주문/조회 시도 | 403 또는 정책 거부 |
| 실전/모의 권한 테스트 | 모의 권한 사용자로 실전 주문 요청 | KIS submit 0회, 정책 거부 |
| secret redaction 테스트 | Kafka payload, log, API response 검사 | appsecret, token, 전체 계좌번호 없음 |
| DLQ 테스트 | 깨진 schema 또는 알 수 없는 KIS 응답 투입 | DLQ 저장, 원본 offset 포함 |
| kill switch 테스트 | kill switch ON 후 실전 주문 요청 | KIS POST 0회 |
| rate limit 테스트 | 짧은 시간에 대량 주문 요청 | 제한 초과 주문 차단 |
| 대사 지연 테스트 | 체결 이벤트가 늦게 들어오도록 구성 | 최종 상태가 대사 후 보정됨 |

---

## 8. MVP 구현 우선순위

### P0: 반드시 구현

- `Idempotency-Key` 기반 주문 멱등성
- 주문 body hash 검증
- DB unique constraint
- 주문 상태 저장: `RECEIVED`, `PUBLISHED`, `REJECTED`, `SUBMITTED`, `SUBMIT_FAILED_UNKNOWN`
- Kafka 주문 발행
- consumer auto commit 비활성화
- DB commit 후 offset commit
- `account_alias:symbol` 기반 Kafka partition key
- KIS Adapter 단일화
- KIS timeout 시 재POST 금지
- secret redaction
- 주문 제출 차단 플래그. 실전 주문 전환 전 kill switch로 강화

### P1: 가능하면 구현

- Outbox 패턴
- `order_events` append-only 원장
- KIS 주문체결내역 polling
- DLQ topic
- 권한 role 분리
- 계좌별 rate limit
- 프론트 주문 상태 조회 및 WebSocket 상태 갱신

### P2: 후순위

- 체결 WebSocket 연동
- 자동 reconciliation
- circuit breaker
- 운영자 재처리 화면
- 주문 이상 탐지 알림
- 상세 모니터링 대시보드
