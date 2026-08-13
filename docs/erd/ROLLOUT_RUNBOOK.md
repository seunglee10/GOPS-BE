# ERD 확장 배포 Runbook

이 변경은 기존 HTTP 경로·응답과 기존 문자열 식별자를 유지하는 확장 배포다. 구 컬럼 제거와 RLS 활성화는 자동 마이그레이션에 포함하지 않는다.

## 1. 사전 점검

```sh
export DATABASE_URL='postgresql://...'
PYTHONPATH=systems/order/shared \
  python systems/order/jobs/postgres-migrations/main.py
```

Migration Job은 `0020`, `0021`을 적용하고 10,000행 단위 백필, 변환 불가 날짜, 사용자·종목 매핑 누락, 신·구 값 불일치, FK 검증을 순서대로 수행한다. `schema_migrations`에는 파일 SHA-256과 트랜잭션 모드가 기록된다. 적용된 파일의 내용이 바뀌면 배포를 중단한다.

## 2. ClickHouse 확장

```sh
docker compose run --rm clickhouse-migrations
```

운영 EKS에서는 `scripts/aws/run-clickhouse-migrations-job.sh`가 app rollout 전에 실행된다. 새 row는 `instrument_id`를 기록하며 기존 대용량 tick/candle 파티션은 즉시 mutation하지 않는다. 과거 데이터는 shadow table에 파티션 단위로 복사하고 row count, min/max time, key별 checksum을 비교한 뒤 교체한다.

## 3. 관찰 지표

최소 5거래일 동안 다음 값이 0인지 거래일별로 기록한다.

- typed 날짜 변환 실패와 신·구 불일치
- `app_user_id`, `instrument_id` 누락 및 사용자 소유권 불일치
- FK 고아 레코드
- execution 합계와 주문 `filled_qty`·평균가·현금 원장·포지션 불일치
- UUID v2 Redis key copy-on-read 실패
- Outbox lease 만료 후 유실, Inbox 중복 반영
- `*_latest` View 결과의 merge 전후 불일치

ERD drift는 다음 명령으로 검사한다.

```sh
DATABASE_URL="$DATABASE_URL" python scripts/erd/check_erd_drift.py --strict
```

## 4. RLS 전환

먼저 API, worker, migration 접속 역할을 `operations/provision_database_roles.sql`로 분리한다. API 요청 트랜잭션은 `SET LOCAL app.current_user_id`를 설정해야 한다. 다른 사용자 격리 검증 후에만 다음처럼 활성화한다.

```sh
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
  -c "SET gops.rls_activation_confirmed = 'yes'" \
  -f operations/enable_user_rls.sql
```

서비스 역할은 RLS를 우회하고 API 역할은 강제 적용된다. 활성화 전에 동일한 자격으로 사용자 A가 사용자 B의 주문·알림·추천을 조회하지 못하는지 검증한다.

## 5. 계약 단계

아래 조건을 모두 만족하기 전에는 `user_sub`, 모의투자 `user_id`, `symbol`, 문자열 날짜 컬럼을 삭제하지 않는다.

1. 최소 5거래일 지표가 모두 0이다.
2. 이전 애플리케이션 버전으로 읽기 rollback이 필요하지 않다.
3. Kafka 호환 기간 두 버전이 종료됐다.
4. 모든 Repository가 UUID·typed 컬럼 우선 읽기로 전환됐다.
5. 백업 복원 연습과 ERD drift 검사가 통과했다.

`symbol`은 주문 당시 표시값과 감사 스냅샷으로 계속 유지할 수 있다. 계약 단계는 별도 승인된 신규 migration으로 작성하며 기존 `0020`, `0021`을 수정하지 않는다.
