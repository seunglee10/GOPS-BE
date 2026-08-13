# GOPS ERD 관리

전체 ERDCloud Import 원본은 [`../ERDCLOUD_IMPORT.sql`](../ERDCLOUD_IMPORT.sql)이다. 아래 파일은 도메인별 리뷰와 재Import를 위한 축약 뷰다.

| 파일 | 범위 |
|---|---|
| `01-order-paper.sql` | 주문, Outbox/Inbox, 모의계좌, 모의체결 원장 |
| `02-users-recommendations.sql` | 내부 사용자, OAuth 식별자, 추천 실행·아이템 |
| `03-chart-assets.sql` | PostgreSQL `chart_assets.*` 자산·빌드 작업 |
| `04-clickhouse-market-data.sql` | 실제 `market_data.*` 테이블을 `ch_*` 별칭으로 표시한 논리 모델 |

각 테이블의 실무 역할과 원본·집계 책임은 [`TABLE_ROLES.md`](TABLE_ROLES.md)에 정리한다.

관계 표기 규칙:

- `fk_*`: PostgreSQL이 실제로 검증하는 물리 FK
- `fk_logical_*`: ClickHouse 안에서 코드·정렬 키로만 유지하는 논리 관계
- `fk_cross_*`: PostgreSQL `instruments`와 ClickHouse `market_data.*.instrument_id` 사이의 교차 저장소 데이터 흐름. 실제 물리 FK는 없다.

`ch_`는 ERDCloud 표시용 별칭일 뿐이다. 예를 들어 `ch_chart_candles`의 실제 테이블은 `market_data.chart_candles`다. ClickHouse의 `ReplacingMergeTree` 최신 행은 운영 쿼리에서 `*_latest` View를 사용한다.

변경 절차는 `확장 → 이중 쓰기 → 10,000행 배치 백필 → 검증 → 전환 → 5거래일 안정화 → 계약/제거` 순서다. 기존 컬럼 제거와 RLS 강제 활성화는 안정화 기록이 없는 상태에서 실행하지 않는다.

실제 배포·검증·전환 절차는 [`ROLLOUT_RUNBOOK.md`](ROLLOUT_RUNBOOK.md)를 따른다.
